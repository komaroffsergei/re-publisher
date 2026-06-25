from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.content.common import run_async, session_factory, settings_or_exit
from app.content.link_materials import (
    LINK_SUMMARY_PENDING_STATUS,
    append_link_materials_section,
    build_enriched_numbered_digest_body,
    format_link_summary_context,
    is_article_like_link,
    link_summary_gate,
    load_link_materials,
    restore_missing_markdown_links,
    telegram_link_segments,
    telegram_text_with_markdown_links,
)
from app.content.max_publisher import MAX_TEXT_LIMIT, max_text_size, trim_max_text
from app.content.pipeline_activity import try_acquire_pipeline_work_lock
from app.content.pipeline_logic import READY_DRAFT_STATUS, status_from_state
from app.content.pipeline_entries import sync_pipeline_entry_stage
from app.content.prompt_versions import (
    PIPELINE_REWRITE_PROMPT,
    create_prompt_version as create_named_prompt_version,
    ensure_active_prompt_version as ensure_named_active_prompt_version,
    prompt_config_from_version as named_prompt_config_from_version,
)
from app.content.rewriter import draft_from_local_llm, draft_from_yandex, load_templates
from app.content.state import mark_state
from app.content.yandex_gpt import YandexGPTError
from app.main import safe_echo
from app.models import (
    ContentItem,
    PipelineEntry,
    PublicationDraft,
    PublicationTarget,
    PublishedPost,
    RewriteAttempt,
    RewritePromptVersion,
    Showcase,
    TelegramChat,
    TelegramPost,
)

app = typer.Typer(no_args_is_help=True)
PROMPT_NAME = PIPELINE_REWRITE_PROMPT
REWRITE_RUNNING_STATUS = "rewrite_running"
MAX_TEXT_TRIMMED_RISK = "max_text_trimmed"
NON_BLOCKING_RISK_FLAGS = {
    "missing_source_url",
    "missing_source_summary",
    "json_salvaged",
    "structure_preserved_fallback",
    MAX_TEXT_TRIMMED_RISK,
}
BLOCKING_RISK_FLAGS = {"media_only_or_empty", "thin_source"}


@app.callback()
def main() -> None:
    """YandexGPT rewrite worker for publication pipeline."""


def should_block_link_summary_gate(gate, *, allow_pending_link_summaries: bool = False) -> bool:
    return not gate.ok and not (
        allow_pending_link_summaries and gate.status == LINK_SUMMARY_PENDING_STATUS
    )


def prompt_config_from_version(version: RewritePromptVersion) -> dict[str, Any]:
    return named_prompt_config_from_version(version)


def blocking_risk_flags(risk_flags: list[str]) -> list[str]:
    return [flag for flag in risk_flags if flag in BLOCKING_RISK_FLAGS or flag not in NON_BLOCKING_RISK_FLAGS]


def telegram_post_source_url(post: TelegramPost | None, chat: TelegramChat | None) -> str | None:
    if not post or not post.message_id:
        return None
    username = str(chat.username or "").strip().lstrip("@") if chat and chat.username else ""
    if username:
        return f"https://t.me/{username}/{post.message_id}"
    peer_id = int(post.chat_peer_id or 0)
    if peer_id < -1000000000000:
        return f"https://t.me/c/{abs(peer_id) - 1000000000000}/{post.message_id}"
    return None


def append_source_post_link(body: str, source_url: str | None) -> str:
    text = (body or "").strip()
    url = (source_url or "").strip()
    if not text or not url or url in text:
        return text
    return f"{text}\n\nИсточник: [оригинальный пост]({url})"


def source_post_link_line(source_url: str | None) -> str:
    url = (source_url or "").strip()
    return f"Источник: [оригинальный пост]({url})" if url else ""


def composed_max_text_size(title: str | None, body: str | None) -> int:
    title_text = (title or "").strip()
    body_text = (body or "").strip()
    text = f"**{title_text}**\n\n{body_text}" if title_text else body_text
    return max_text_size(text)


def fit_body_to_max_text_limit(title: str | None, body: str | None, source_url: str | None) -> tuple[str, bool]:
    title_text = (title or "").strip()
    body_text = (body or "").strip()
    if composed_max_text_size(title_text, body_text) <= MAX_TEXT_LIMIT:
        return body_text, False

    prefix = f"**{title_text}**\n\n" if title_text else ""
    available_body_size = max(0, MAX_TEXT_LIMIT - max_text_size(prefix))
    source_line = source_post_link_line(source_url)
    if not source_line:
        return trim_max_text(body_text, available_body_size), True

    body_without_source = body_text
    if source_line in body_without_source:
        body_without_source = body_without_source.replace(source_line, "").strip()
    source_with_separator = f"\n\n{source_line}"
    source_size = max_text_size(source_with_separator)
    if source_size >= available_body_size:
        return trim_max_text(source_line, available_body_size), True

    main_limit = available_body_size - source_size
    main = trim_max_text(body_without_source, main_limit).strip()
    fitted = f"{main}{source_with_separator}" if main else source_line
    while fitted and composed_max_text_size(title_text, fitted) > MAX_TEXT_LIMIT:
        fitted = fitted[:-1].rstrip()
    return fitted, True


async def ensure_active_prompt_version(session, *, created_by: str = "system") -> RewritePromptVersion:
    return await ensure_named_active_prompt_version(session, name=PROMPT_NAME, created_by=created_by)


async def create_prompt_version(
    session,
    *,
    system_prompt: str,
    common_user_prompt: str,
    label_prompts: dict[str, Any],
    created_by: str = "web",
) -> RewritePromptVersion:
    return await create_named_prompt_version(
        session,
        name=PROMPT_NAME,
        system_prompt=system_prompt,
        common_user_prompt=common_user_prompt,
        label_prompts=label_prompts,
        created_by=created_by,
    )


async def ready_backlog_count(session) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(PipelineEntry)
        .outerjoin(PublishedPost, PublishedPost.id == PipelineEntry.published_post_id)
        .where(
            PipelineEntry.publication_allowed.is_(True),
            PipelineEntry.status == READY_DRAFT_STATUS,
            PipelineEntry.latest_draft_id.is_not(None),
            PipelineEntry.published_post_id.is_(None),
        )
    )
    return int(result.scalar_one() or 0)


async def update_entry_after_draft(session, entry: PipelineEntry, draft: PublicationDraft | None, *, error: str | None = None) -> None:
    published = None
    if draft:
        published = (
            await session.execute(
                select(PublishedPost).where(PublishedPost.draft_id == draft.id, PublishedPost.status == "published").limit(1)
            )
        ).scalar_one_or_none()
    status = status_from_state(
        publication_allowed=entry.publication_allowed,
        is_eligible=entry.is_eligible,
        draft_status=draft.status if draft else None,
        has_published_post=published is not None,
        has_error=bool(error),
    )
    now = datetime.now(timezone.utc)
    await session.execute(
        update(PipelineEntry)
        .where(PipelineEntry.id == entry.id)
        .values(
            latest_draft_id=draft.id if draft else entry.latest_draft_id,
            published_post_id=published.id if published else entry.published_post_id,
            scheduled_publish_at=None if draft and published is None else entry.scheduled_publish_at,
            status=status,
            last_error=error,
            updated_at=now,
            last_operation_at=now,
        )
    )
    await sync_pipeline_entry_stage(session, entry.source_post_id)


async def default_showcase(session, settings) -> Showcase | None:
    showcase = (
        await session.execute(select(Showcase).where(Showcase.slug == settings.pipeline_showcase_slug).limit(1))
    ).scalar_one_or_none()
    if showcase:
        return showcase
    return (
        await session.execute(select(Showcase).where(Showcase.slug == "ai_education").limit(1))
    ).scalar_one_or_none()


async def rewrite_context(session, entry: PipelineEntry, settings) -> tuple[ContentItem, PublicationTarget, Showcase, PublicationDraft | None] | None:
    result = await session.execute(
        select(ContentItem, PublicationTarget, Showcase, PublicationDraft)
        .join(PublicationTarget, PublicationTarget.content_item_id == ContentItem.id)
        .join(Showcase, Showcase.id == PublicationTarget.showcase_id)
        .outerjoin(PublicationDraft, PublicationDraft.id == entry.latest_draft_id)
        .where(ContentItem.id == entry.content_item_id)
        .order_by(PublicationTarget.id)
        .limit(1)
    )
    row = result.first()
    if row:
        return row

    item = (await session.execute(select(ContentItem).where(ContentItem.id == entry.content_item_id))).scalar_one_or_none()
    showcase = await default_showcase(session, settings)
    if not item or not showcase:
        return None

    now = datetime.now(timezone.utc)
    values = {
        "content_item_id": item.id,
        "showcase_id": showcase.id,
        "route_reason": "manual rewrite target",
        "route_score": float(entry.confidence or 0),
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    stmt = insert(PublicationTarget.__table__).values(**values)
    target_id = (
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_publication_targets_item_showcase",
                set_={
                    "route_reason": stmt.excluded.route_reason,
                    "route_score": stmt.excluded.route_score,
                    "status": stmt.excluded.status,
                    "updated_at": stmt.excluded.updated_at,
                },
            ).returning(PublicationTarget.id)
        )
    ).scalar_one()
    target = (await session.execute(select(PublicationTarget).where(PublicationTarget.id == target_id))).scalar_one()
    existing_draft = None
    if entry.latest_draft_id:
        existing_draft = (
            await session.execute(select(PublicationDraft).where(PublicationDraft.id == entry.latest_draft_id))
        ).scalar_one_or_none()
    return item, target, showcase, existing_draft


async def rewrite_entry(
    session,
    entry: PipelineEntry,
    *,
    prompt_version: RewritePromptVersion,
    refresh: bool = False,
    backend: str | None = None,
    allow_pending_link_summaries: bool = False,
) -> bool:
    settings = settings_or_exit()
    rewrite_backend = backend or settings.rewrite_backend
    templates = load_templates()
    row = await rewrite_context(session, entry, settings)
    if not row:
        await update_entry_after_draft(session, entry, None, error="rewrite_context_missing")
        return False
    item, target, showcase, existing_draft = row
    post = (await session.execute(select(TelegramPost).where(TelegramPost.id == entry.source_post_id))).scalar_one_or_none()
    chat = (
        await session.execute(select(TelegramChat).where(TelegramChat.peer_id == post.chat_peer_id))
        if post
        else None
    )
    source_chat = chat.scalar_one_or_none() if chat else None
    source_post_url = telegram_post_source_url(post, source_chat)
    source_link_segments = telegram_link_segments(post.text, post.raw) if post else []
    source_post_text = telegram_text_with_markdown_links(post.text, post.raw) if post else item.main_text
    link_materials = await load_link_materials(session, entry.source_post_id)
    gate = link_summary_gate(link_materials)
    inline_link_count = len([segment for segment in source_link_segments if segment.url])
    article_summary_count = len([material for material in link_materials if material.snapshot and material.snapshot.summary_short])
    article_link_count = len([material for material in link_materials if is_article_like_link(material.link)])
    events = [
        "rewrite requested",
        f"inline links detected: {inline_link_count}",
        f"article link summaries ready: {article_summary_count}/{article_link_count}",
    ]
    request_meta = {
        "prompt_version": prompt_version.version,
        "refresh": refresh,
        "backend": rewrite_backend,
        "inline_links": inline_link_count,
        "article_link_summaries": article_summary_count,
        "article_links": article_link_count,
        "events": events,
    }
    if source_post_url:
        request_meta["source_post_url"] = source_post_url
    if should_block_link_summary_gate(gate, allow_pending_link_summaries=allow_pending_link_summaries):
        events.append(gate.reason or "link summary gate blocked rewrite")
        now = datetime.now(timezone.utc)
        await session.execute(
            insert(RewriteAttempt.__table__).values(
                pipeline_entry_id=entry.id,
                source_post_id=entry.source_post_id,
                prompt_version_id=prompt_version.id,
                status="blocked",
                error=gate.reason,
                request_meta=request_meta,
                started_at=now,
                finished_at=now,
                created_at=now,
            )
        )
        await session.execute(
            update(PipelineEntry)
            .where(PipelineEntry.id == entry.id)
            .values(
                status=gate.status,
                last_error=gate.reason,
                updated_at=now,
                last_operation_at=now,
            )
        )
        await mark_state(session, entry.source_post_id, rewrite_status=gate.status or "link_summary_pending", last_error=gate.reason)
        return False
    if not gate.ok:
        events.append(f"{gate.reason}; manual rewrite continues without embedded link summaries")
        request_meta["link_summary_gate_bypassed"] = True
        request_meta["link_summary_gate_status"] = gate.status
        request_meta["link_summary_gate_reason"] = gate.reason
    source_summary_context = format_link_summary_context(link_materials) or None
    numbered_body, numbered_meta = (
        build_enriched_numbered_digest_body(post.text, post.raw, link_materials) if post else (None, {})
    )
    if numbered_body:
        request_meta["numbered_digest"] = numbered_meta
    prompt_config = prompt_config_from_version(prompt_version)
    template_name = showcase.default_rewrite_template or "education_guide"
    template = templates.get(template_name) or {}
    request_meta["template"] = template_name
    attempt_values = {
        "pipeline_entry_id": entry.id,
        "source_post_id": entry.source_post_id,
        "prompt_version_id": prompt_version.id,
        "status": "running",
        "request_meta": request_meta,
        "started_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
    }
    attempt_stmt = insert(RewriteAttempt.__table__).values(**attempt_values).returning(RewriteAttempt.id)
    attempt_id = (await session.execute(attempt_stmt)).scalar_one()
    await session.execute(
        update(PipelineEntry)
        .where(PipelineEntry.id == entry.id)
        .values(
            status=REWRITE_RUNNING_STATUS,
            last_error=None,
            updated_at=datetime.now(timezone.utc),
            last_operation_at=datetime.now(timezone.utc),
        )
    )
    await mark_state(session, entry.source_post_id, rewrite_status="running", last_error=None)
    await session.flush()
    await session.commit()
    try:
        restored_inline_links: list[str] = []
        if numbered_body:
            title = (item.translated_title or item.title or "Материал").strip() or "Материал"
            draft = {
                "title": title[:240],
                "body": numbered_body,
                "source_url": item.source_url,
                "source_domain": item.source_domain,
                "image_asset_id": item.primary_image_asset_id,
                "claims": [],
                "risk_flags": ["structure_preserved_fallback"],
                "similarity_to_original": None,
                "rewrite_model": "deterministic_numbered_digest",
            }
            restored_inline_links = [segment.url for segment in source_link_segments if segment.url]
            events.append("model skipped for numbered digest")
            events.append(
                "numbered digest structure preserved: "
                f"{numbered_meta.get('numbered_sections', 0)} sections, "
                f"{numbered_meta.get('enriched_sections', 0)} enriched"
            )
        else:
            events.append("model request started")
            if rewrite_backend == "yandexgpt":
                draft = await draft_from_yandex(
                    item,
                    showcase,
                    template,
                    entry.genre_primary,
                    settings,
                    prompt_config,
                    source_summary_override=source_summary_context,
                    post_text_override=source_post_text,
                )
            elif rewrite_backend == "local_llm":
                draft = await draft_from_local_llm(
                    item,
                    showcase,
                    template,
                    entry.genre_primary,
                    settings,
                    prompt_config,
                    source_summary_override=source_summary_context,
                    post_text_override=source_post_text,
                )
            else:
                raise YandexGPTError(f"Unsupported REWRITE_BACKEND: {rewrite_backend}")
            draft_body, restored_inline_links = restore_missing_markdown_links(draft["body"], source_link_segments)
            if restored_inline_links:
                events.append(f"restored inline links: {len(restored_inline_links)}")
            draft["body"] = append_link_materials_section(draft_body, link_materials)
        draft["body"] = append_source_post_link(draft["body"], source_post_url)
        if source_post_url:
            events.append("source post link appended")
        fitted_body, was_trimmed = fit_body_to_max_text_limit(draft["title"], draft["body"], source_post_url)
        if was_trimmed:
            draft["body"] = fitted_body
            risk_flags = list(draft.get("risk_flags") or [])
            if MAX_TEXT_TRIMMED_RISK not in risk_flags:
                risk_flags.append(MAX_TEXT_TRIMMED_RISK)
            draft["risk_flags"] = risk_flags
            events.append(f"trimmed to MAX text limit: {composed_max_text_size(draft['title'], draft['body'])}/{MAX_TEXT_LIMIT}")
        status = READY_DRAFT_STATUS if not blocking_risk_flags(draft["risk_flags"]) else "needs_review"
        draft_model = draft.get("rewrite_model") or rewrite_backend
        draft_values = {
            "publication_target_id": target.id,
            "source_post_id": item.source_post_id,
            "rewrite_model": draft_model,
            "rewrite_template": f"{entry.genre_primary or template_name}/prompt_v{prompt_version.version}/{draft_model}",
            "title": draft["title"],
            "body": draft["body"] or "Текст требует ручной проверки.",
            "source_url": draft["source_url"],
            "source_domain": draft["source_domain"],
            "image_asset_id": draft["image_asset_id"],
            "tags": [entry.genre_primary] if entry.genre_primary else [],
            "claims": draft.get("claims") or [],
            "risk_flags": draft["risk_flags"],
            "similarity_to_original": draft["similarity_to_original"],
            "validation_errors": [],
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if existing_draft:
            await session.execute(update(PublicationDraft).where(PublicationDraft.id == existing_draft.id).values(**draft_values))
            draft_id = existing_draft.id
        else:
            draft_values["created_at"] = datetime.now(timezone.utc)
            draft_id = (await session.execute(insert(PublicationDraft.__table__).values(**draft_values).returning(PublicationDraft.id))).scalar_one()
        saved_draft = (await session.execute(select(PublicationDraft).where(PublicationDraft.id == draft_id))).scalar_one()
        await session.execute(
            update(RewriteAttempt)
            .where(RewriteAttempt.id == attempt_id)
            .values(
                publication_draft_id=draft_id,
                rewrite_model=draft_values["rewrite_model"],
                status="done",
                response_raw={
                    "title": draft["title"],
                    "claims": draft.get("claims") or [],
                    "restored_inline_links": restored_inline_links,
                    "events": [*events, "draft saved"],
                },
                risk_flags=draft["risk_flags"],
                validation_errors=[],
                finished_at=datetime.now(timezone.utc),
            )
        )
        await update_entry_after_draft(session, entry, saved_draft)
        await mark_state(session, entry.source_post_id, rewrite_status=status)
        return True
    except asyncio.CancelledError:
        error = "rewrite_cancelled"
        events.append(error)
        await session.execute(
            update(RewriteAttempt)
            .where(RewriteAttempt.id == attempt_id)
            .values(status="cancelled", error=error, response_raw={"events": events}, finished_at=datetime.now(timezone.utc))
        )
        await session.execute(
            update(PipelineEntry)
            .where(PipelineEntry.id == entry.id)
            .values(
                status=existing_draft.status if existing_draft else "rewrite_pending",
                last_error=error,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await mark_state(session, entry.source_post_id, rewrite_status="cancelled", last_error=error)
        await session.commit()
        raise
    except Exception as exc:
        error = f"rewrite_{rewrite_backend}: {str(exc)[:800]}"
        events.append(error)
        await session.execute(
            update(RewriteAttempt)
            .where(RewriteAttempt.id == attempt_id)
            .values(status="failed", error=error, response_raw={"events": events}, finished_at=datetime.now(timezone.utc))
        )
        await update_entry_after_draft(session, entry, existing_draft, error=error)
        await mark_state(session, entry.source_post_id, rewrite_status="failed", last_error=error)
        return False


async def rewrite_ready_pipeline(
    limit: int | None = None,
    *,
    refresh: bool = False,
    ignore_backlog_cap: bool = False,
    backend: str | None = None,
) -> dict[str, Any]:
    settings = settings_or_exit()
    actual_limit = 1
    if not settings.enable_rewrite:
        return {"rewritten": 0, "skipped": "rewrite_disabled"}
    factory = session_factory(settings)
    rewritten = 0
    async with factory() as session:
        prompt_version = await ensure_active_prompt_version(session)
        backlog = await ready_backlog_count(session)
        if backlog >= settings.pipeline_ready_backlog_limit and not ignore_backlog_cap:
            return {"rewritten": 0, "ready_backlog": backlog, "stopped": "ready_backlog_cap"}
        stmt = (
            select(PipelineEntry)
            .where(
                PipelineEntry.is_eligible.is_(True),
                PipelineEntry.publication_allowed.is_(True),
                PipelineEntry.published_post_id.is_(None),
            )
            .order_by(PipelineEntry.scheduled_publish_at.nulls_last(), PipelineEntry.id)
            .limit(actual_limit)
        )
        if refresh:
            stmt = stmt.where(PipelineEntry.latest_draft_id.is_not(None))
        else:
            stmt = stmt.where(PipelineEntry.latest_draft_id.is_(None))
        entries = list((await session.execute(stmt)).scalars())
        for entry in entries:
            backlog = await ready_backlog_count(session)
            if backlog >= settings.pipeline_ready_backlog_limit and not ignore_backlog_cap:
                break
            ok = await rewrite_entry(session, entry, prompt_version=prompt_version, refresh=refresh, backend=backend)
            await session.commit()
            if ok:
                rewritten += 1
    return {"rewritten": rewritten}


async def rewrite_one(
    entry_id: int,
    *,
    ignore_backlog_cap: bool = True,
    backend: str | None = None,
    allow_pending_link_summaries: bool = False,
) -> dict[str, Any]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        prompt_version = await ensure_active_prompt_version(session)
        entry = (await session.execute(select(PipelineEntry).where(PipelineEntry.id == entry_id))).scalar_one_or_none()
        if not entry:
            return {"rewritten": 0, "error": "entry_not_found"}
        if not ignore_backlog_cap and await ready_backlog_count(session) >= settings.pipeline_ready_backlog_limit:
            return {"rewritten": 0, "stopped": "ready_backlog_cap"}
        ok = await rewrite_entry(
            session,
            entry,
            prompt_version=prompt_version,
            refresh=True,
            backend=backend,
            allow_pending_link_summaries=allow_pending_link_summaries,
        )
        await session.commit()
        return {"rewritten": 1 if ok else 0}


async def rewrite_one_by_one(limit: int, *, backend: str = "local_llm", refresh: bool = False) -> dict[str, Any]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    report_rows: list[dict[str, Any]] = []
    started = time.monotonic()
    async with factory() as session:
        prompt_version = await ensure_active_prompt_version(session)
        stmt = (
            select(PipelineEntry)
            .where(
                PipelineEntry.is_eligible.is_(True),
                PipelineEntry.publication_allowed.is_(True),
                PipelineEntry.published_post_id.is_(None),
            )
            .order_by(PipelineEntry.scheduled_publish_at.nulls_last(), PipelineEntry.id)
            .limit(limit)
        )
        if refresh:
            stmt = stmt.where(PipelineEntry.latest_draft_id.is_not(None))
        else:
            stmt = stmt.where(PipelineEntry.latest_draft_id.is_(None))
        entries = list((await session.execute(stmt)).scalars())
        for index, entry in enumerate(entries, start=1):
            before = time.monotonic()
            ok = await rewrite_entry(session, entry, prompt_version=prompt_version, refresh=refresh, backend=backend)
            await session.commit()
            saved = (
                await session.execute(
                    select(PipelineEntry, PublicationDraft)
                    .outerjoin(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
                    .where(PipelineEntry.id == entry.id)
                )
            ).first()
            saved_entry = saved[0] if saved else entry
            draft = saved[1] if saved else None
            elapsed = round(time.monotonic() - before, 2)
            row = {
                "index": index,
                "entry_id": entry.id,
                "source_post_id": entry.source_post_id,
                "ok": ok,
                "status": saved_entry.status,
                "draft_status": draft.status if draft else None,
                "rewrite_model": draft.rewrite_model if draft else None,
                "seconds": elapsed,
                "body_chars": len(draft.body) if draft and draft.body else 0,
                "risk_flags": list(draft.risk_flags or []) if draft else [],
                "error": saved_entry.last_error,
            }
            report_rows.append(row)
            safe_echo(
                " ".join(
                    [
                        f"processed={index}/{len(entries)}",
                        f"entry_id={entry.id}",
                        f"status={row['status']}",
                        f"seconds={elapsed}",
                        f"chars={row['body_chars']}",
                        f"error={row['error'] or ''}",
                    ]
                )
            )
            sys.stdout.flush()
    total_seconds = round(time.monotonic() - started, 2)
    report_path = write_rewrite_report(report_rows, backend=backend, total_seconds=total_seconds)
    return {
        "processed": len(report_rows),
        "ready": sum(1 for row in report_rows if row["status"] == READY_DRAFT_STATUS),
        "needs_review": sum(1 for row in report_rows if row["status"] == "needs_review"),
        "failed": sum(1 for row in report_rows if row["status"] == "rewrite_failed"),
        "total_seconds": total_seconds,
        "report": str(report_path),
    }


def write_rewrite_report(rows: list[dict[str, Any]], *, backend: str, total_seconds: float) -> Path:
    target = Path("reports") / "local_llm_rewrite_50.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    ready = sum(1 for row in rows if row["status"] == READY_DRAFT_STATUS)
    needs_review = sum(1 for row in rows if row["status"] == "needs_review")
    failed = sum(1 for row in rows if row["status"] == "rewrite_failed")
    avg_seconds = round(total_seconds / len(rows), 2) if rows else 0
    lines = [
        "# Local LLM rewrite smoke",
        "",
        f"- backend: `{backend}`",
        f"- processed: `{len(rows)}`",
        f"- ready: `{ready}`",
        f"- needs_review: `{needs_review}`",
        f"- failed: `{failed}`",
        f"- total_seconds: `{total_seconds}`",
        f"- avg_seconds_per_post: `{avg_seconds}`",
        "",
        "| # | entry_id | source_post_id | status | draft_status | model | seconds | chars | risks | error |",
        "|---:|---:|---:|---|---|---|---:|---:|---|---|",
    ]
    for row in rows:
        risks = ", ".join(row["risk_flags"])[:140]
        error_text = (row["error"] or "").replace("|", "\\|")[:160]
        formatted = {**row, "risks": risks.replace("|", "\\|"), "error": error_text}
        lines.append(
            "| {index} | {entry_id} | {source_post_id} | {status} | {draft_status} | {rewrite_model} | {seconds} | {body_chars} | {risks} | {error} |".format(
                **formatted,
            )
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


@app.command("run-once")
def run_once_command(
    limit: int | None = typer.Option(None, "--limit", min=1),
    refresh: bool = typer.Option(False, "--refresh"),
    ignore_backlog_cap: bool = typer.Option(False, "--ignore-backlog-cap"),
) -> None:
    """Rewrite pipeline entries until limit or ready backlog cap is reached."""

    safe_echo(run_async(rewrite_ready_pipeline(limit, refresh=refresh, ignore_backlog_cap=ignore_backlog_cap)))


@app.command("rewrite-one-by-one")
def rewrite_one_by_one_command(
    limit: int = typer.Option(50, "--limit", min=1),
    backend: str = typer.Option("local_llm", "--backend"),
    refresh: bool = typer.Option(False, "--refresh"),
) -> None:
    """Rewrite selected pipeline entries sequentially and write a smoke report."""

    safe_echo(run_async(rewrite_one_by_one(limit, backend=backend, refresh=refresh)))


@app.command("run-loop")
def run_loop_command(
    interval_seconds: int = typer.Option(300, "--interval-seconds", min=30),
    limit: int | None = typer.Option(None, "--limit", min=1),
    max_cycles: int | None = typer.Option(None, "--max-cycles", min=1),
) -> None:
    """Run the capped YandexGPT rewrite worker on a fixed local schedule."""

    cycle = 0
    while True:
        cycle += 1
        async def _cycle() -> dict[str, Any]:
            settings = settings_or_exit()
            lock = await try_acquire_pipeline_work_lock(settings, owner="rewrite_run_loop")
            if lock is None:
                return {"rewritten": 0, "busy": True}
            try:
                return await rewrite_ready_pipeline(1, ignore_backlog_cap=False)
            finally:
                await lock.release()

        result = run_async(_cycle())
        safe_echo(
            " ".join(
                [
                    f"ts={datetime.now(timezone.utc).isoformat()}",
                    f"cycle={cycle}",
                    f"result={result}",
                ]
            )
        )
        sys.stdout.flush()
        if max_cycles is not None and cycle >= max_cycles:
            break
        time.sleep(interval_seconds)


@app.command("rewrite-entry")
def rewrite_entry_command(
    entry_id: int = typer.Argument(...),
    backend: str | None = typer.Option(None, "--backend"),
) -> None:
    """Force rewrite one pipeline entry, intended for the web Repeat Rewrite action."""

    safe_echo(run_async(rewrite_one(entry_id, backend=backend)))


if __name__ == "__main__":
    app()
