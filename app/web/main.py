from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import typer
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select, text, update

from app.config import Settings, get_settings
from app.content.link_enricher import enrich_post_links
from app.content.link_materials import (
    LINK_SUMMARY_FAILED_STATUS,
    LINK_SUMMARY_PENDING_STATUS,
    is_article_like_link,
    link_display_url,
    link_title,
    load_link_materials,
    load_media_assets_for_post,
    markdown_link_segments,
    strip_link_materials_section,
    telegram_link_segments,
)
from app.content.local_summary import summarize_post_links
from app.content.material_builder import build_post_material
from app.content.media_assets import download_link_images_for_post, register_telegram_media_for_post
from app.content.pipeline_activity import (
    active_pipeline_work_snapshot,
    reset_stale_pipeline_activity,
    try_acquire_pipeline_work_lock,
    PipelineWorkLock,
)
from app.content.pipeline_entries import PIPELINE_STAGE_LABELS, PIPELINE_STAGES, sync_pipeline_entry_stage
from app.content.pipeline_logic import READY_DRAFT_STATUS
from app.content.pipeline_manager import classify_post
from app.content.pipeline_rewriter import rewrite_one, rewrite_ready_pipeline, telegram_post_source_url
from app.content.processor import process_post
from app.content.prompt_versions import (
    LINK_SUMMARY_PROMPT,
    PIPELINE_REWRITE_PROMPT,
    create_prompt_version,
    ensure_active_prompt_version,
)
from app.content.topic_audit import latest_topic_audit_summary, report_root
from app.content.yandex_genre_classifier import usage_total_tokens
from app.content.search import build_search_statement
from app.content.url_extractor import extract_post_links
from app.db import create_session_factory
from app.logging_setup import setup_logging
from app.models import (
    ContentItem,
    ContentPipelineState,
    CodexGenreClassification,
    CodexGenreModelComparison,
    CodexTrainingRun,
    LabelingQueue,
    LinkSnapshot,
    MediaAsset,
    ModelVersion,
    PipelineEntry,
    PostClassification,
    PostLabel,
    PostProcessed,
    PostLink,
    PublicationDraft,
    PublicationTarget,
    PublishedPost,
    RewriteAttempt,
    RewritePromptVersion,
    SearchDocument,
    Showcase,
    TelegramChat,
    TelegramPost,
    YandexGenreClassification,
)

web_cli = typer.Typer(no_args_is_help=True)
security = HTTPBasic(auto_error=False)
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
CONTENT_ACTIVE_STATUS_VALUES = {"running", "processing"}
CONTENT_PIPELINE_FIELDS = (
    ("processing_status", "process_post", "подготовка текста"),
    ("link_status", "extract_links", "извлечение ссылок"),
    ("enrichment_status", "enrich_links", "скачивание ссылок"),
    ("summary_status", "summarize_links", "summary ссылок"),
    ("material_status", "build_material", "сборка материала"),
    ("classification_status", "classify_post", "классификация"),
    ("rewrite_status", "rewrite", "рерайт"),
    ("publication_status", "publish", "публикация"),
)
PIPELINE_PHASE_LABELS = {
    "starting": "старт",
    "process_post": "подготовка текста",
    "extract_links": "извлечение ссылок",
    "enrich_links": "скачивание ссылок",
    "telegram_media": "медиа Telegram",
    "link_images": "изображения ссылок",
    "download_media": "скачивание медиа",
    "summarize_links": "summary ссылок",
    "build_material": "сборка материала",
    "classify_post": "классификация",
    "refresh_pipeline_entry": "обновление карточки",
    "enriched_rewrite": "обогащенный рерайт",
    "refresh_after_rewrite": "обновление после рерайта",
}
PIPELINE_PHASE_ORDER = list(PIPELINE_PHASE_LABELS)


@web_cli.callback()
def web_main() -> None:
    """Web app commands."""


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(title="Re Publisher")
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    register_routes(app)

    @app.on_event("startup")
    async def cleanup_pipeline_activity_on_startup() -> None:
        async with create_session_factory(settings)() as session:
            await reset_stale_pipeline_activity(session)
            await session.commit()

    return app


def require_auth(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    settings: Settings = request.app.state.settings
    if not settings.web_basic_auth_user and not settings.web_basic_auth_password:
        return
    if credentials is None:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    user_ok = secrets.compare_digest(credentials.username, settings.web_basic_auth_user or "")
    password_ok = secrets.compare_digest(credentials.password, settings.web_basic_auth_password or "")
    if not (user_ok and password_ok):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def session_factory(request: Request):
    return create_session_factory(request.app.state.settings)


def serialize_model(obj: Any) -> dict[str, Any]:
    return {column.name: getattr(obj, column.name) for column in obj.__table__.columns}


def latest_yandex_genre_artifact(settings: Settings, limit: int = 20) -> dict[str, Any] | None:
    root = Path(settings.artifacts_dir) / "yandex_genre"
    if not root.exists():
        return None
    candidates = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    latest = candidates[0]
    results_path = latest / "results.jsonl"
    rows: list[dict[str, Any]] = []
    if results_path.exists():
        with results_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(rows) >= limit:
                    break
    return {"run_id": latest.name, "path": latest, "rows": rows}


async def enrich_artifact_rows(session, artifact: dict[str, Any] | None) -> None:
    if not artifact or not artifact.get("rows"):
        return
    post_ids = [int(row["source_post_id"]) for row in artifact["rows"] if row.get("source_post_id") is not None]
    if not post_ids:
        return
    result = await session.execute(
        select(ContentItem, TelegramPost, PublicationDraft)
        .join(TelegramPost, TelegramPost.id == ContentItem.source_post_id)
        .outerjoin(PublicationDraft, PublicationDraft.source_post_id == ContentItem.source_post_id)
        .where(ContentItem.source_post_id.in_(post_ids))
        .order_by(ContentItem.id, PublicationDraft.id.desc())
    )
    context: dict[int, dict[str, Any]] = {}
    for item, post, draft in result.all():
        context.setdefault(item.source_post_id, {"item": item, "post": post, "draft": draft})
    for row in artifact["rows"]:
        row_context = context.get(int(row.get("source_post_id") or 0), {})
        item = row_context.get("item")
        post = row_context.get("post")
        draft = row_context.get("draft")
        row["content_item_id"] = item.id if item else row.get("content_item_id")
        row["original_text"] = post.text if post else ""
        row["draft_status"] = draft.status if draft else "нет"
        row["draft_title"] = draft.title if draft else ""


async def processed_rows(request: Request, limit: int = 50):
    async with session_factory(request)() as session:
        result = await session.execute(
            select(ContentItem, PostClassification, PublicationDraft)
            .outerjoin(PostClassification, PostClassification.content_item_id == ContentItem.id)
            .outerjoin(PublicationDraft, PublicationDraft.source_post_id == ContentItem.source_post_id)
            .order_by(ContentItem.id.desc())
            .limit(limit)
        )
        return result.all()


async def draft_rows(request: Request, status: str | None = None, limit: int = 100):
    async with session_factory(request)() as session:
        stmt = (
            select(PublicationDraft, PublicationTarget, Showcase)
            .join(PublicationTarget, PublicationTarget.id == PublicationDraft.publication_target_id)
            .join(Showcase, Showcase.id == PublicationTarget.showcase_id)
            .order_by(PublicationDraft.id.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(PublicationDraft.status == status)
        result = await session.execute(stmt)
        return result.all()


def rewrite_attempt_events(attempt: RewriteAttempt | None) -> list[str]:
    if not attempt:
        return []
    if attempt.status == "reset":
        response_events = attempt.response_raw.get("events") if isinstance(attempt.response_raw, dict) else None
        if isinstance(response_events, list) and response_events:
            return [str(event) for event in response_events if event]
        return ["link summary queue reset"]
    events: list[str] = []
    for payload in [attempt.request_meta, attempt.response_raw]:
        if not isinstance(payload, dict):
            continue
        raw_events = payload.get("events")
        if isinstance(raw_events, list):
            events.extend(str(event) for event in raw_events if event)
    if attempt.error:
        events.append(attempt.error)
    deduped: list[str] = []
    for event in events:
        if event not in deduped:
            deduped.append(event)
    return deduped


def rewrite_progress(entry: PipelineEntry, latest_attempt: RewriteAttempt | None) -> int:
    if latest_attempt and latest_attempt.status == "running":
        return 55
    if entry.status == "rewrite_running":
        return 35
    if latest_attempt and latest_attempt.status in {"done", "failed", "blocked"}:
        return 100
    if entry.latest_draft_id:
        return 100
    if entry.status in {"link_summary_pending", "link_summary_failed", "rewrite_failed"}:
        return 100
    return 0


def rewrite_status_payload(
    entry: PipelineEntry,
    draft: PublicationDraft | None,
    attempts: list[tuple[RewriteAttempt, RewritePromptVersion | None]],
) -> dict[str, Any]:
    latest_attempt = attempts[0][0] if attempts else None
    events = rewrite_attempt_events(latest_attempt)
    if not events:
        events = [entry.last_error] if entry.last_error else [f"current status: {entry.status}"]
    return {
        "entry_id": entry.id,
        "status": entry.status,
        "last_error": entry.last_error,
        "draft_id": draft.id if draft else None,
        "draft_status": draft.status if draft else None,
        "updated_at": entry.updated_at,
        "progress": rewrite_progress(entry, latest_attempt),
        "is_running": bool((latest_attempt and latest_attempt.status == "running") or entry.status == "rewrite_running"),
        "latest_attempt": serialize_rewrite_attempt(latest_attempt, attempts[0][1] if attempts else None),
        "events": events,
        "attempts": [serialize_rewrite_attempt(attempt, prompt) for attempt, prompt in attempts[:10]],
    }


def serialize_rewrite_attempt(attempt: RewriteAttempt | None, prompt: RewritePromptVersion | None = None) -> dict[str, Any] | None:
    if not attempt:
        return None
    return {
        "id": attempt.id,
        "status": attempt.status,
        "error": attempt.error,
        "prompt_version": prompt.version if prompt else None,
        "rewrite_model": attempt.rewrite_model,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "created_at": attempt.created_at,
        "request_meta": attempt.request_meta or {},
        "response_raw": attempt.response_raw or {},
        "events": rewrite_attempt_events(attempt),
    }


def now_moscow_iso() -> str:
    return datetime.now(ZoneInfo("Europe/Moscow")).isoformat()


def rewrite_worker_state(app: FastAPI) -> dict[str, Any]:
    state = getattr(app.state, "rewrite_worker", None)
    if state is None:
        state = {
            "task": None,
            "running": False,
            "stop_requested": False,
            "phase": "idle",
            "started_at": None,
            "stopped_at": None,
            "last_heartbeat": None,
            "last_result": None,
            "last_error": None,
            "cycles": 0,
            "limit": 10,
            "interval_seconds": 30,
        }
        app.state.rewrite_worker = state
    return state


def public_rewrite_worker_state(app: FastAPI) -> dict[str, Any]:
    state = rewrite_worker_state(app)
    task = state.get("task")
    task_alive = bool(task and not task.done())
    return {
        "running": bool(state.get("running") and task_alive),
        "stop_requested": bool(state.get("stop_requested")),
        "phase": state.get("phase") or "idle",
        "started_at": state.get("started_at"),
        "stopped_at": state.get("stopped_at"),
        "last_heartbeat": state.get("last_heartbeat"),
        "last_result": state.get("last_result"),
        "last_error": state.get("last_error"),
        "cycles": int(state.get("cycles") or 0),
        "limit": int(state.get("limit") or 10),
        "interval_seconds": int(state.get("interval_seconds") or 30),
    }


def pipeline_runs_state(app: FastAPI) -> dict[int, dict[str, Any]]:
    state = getattr(app.state, "pipeline_runs", None)
    if state is None:
        state = {}
        app.state.pipeline_runs = state
    return state


def pipeline_run_state(app: FastAPI, entry_id: int) -> dict[str, Any]:
    runs = pipeline_runs_state(app)
    state = runs.get(entry_id)
    if state is None:
        state = {
            "task": None,
            "running": False,
            "phase": "idle",
            "stages": {},
            "events": [],
            "error": None,
            "started_at": None,
            "finished_at": None,
            "result": None,
        }
        runs[entry_id] = state
    return state


def public_pipeline_run_state(app: FastAPI, entry_id: int) -> dict[str, Any]:
    state = pipeline_run_state(app, entry_id)
    task = state.get("task")
    task_alive = bool(task and not task.done())
    return {
        "entry_id": entry_id,
        "running": bool(state.get("running") and task_alive),
        "phase": state.get("phase") or "idle",
        "stages": state.get("stages") or {},
        "events": list(state.get("events") or []),
        "error": state.get("error"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "result": state.get("result"),
    }


def parse_entry_ids(raw: str | None, *, limit: int = 500) -> list[int]:
    if not raw:
        return []
    entry_ids: list[int] = []
    seen: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            entry_id = int(chunk)
        except ValueError:
            continue
        if entry_id <= 0 or entry_id in seen:
            continue
        seen.add(entry_id)
        entry_ids.append(entry_id)
        if len(entry_ids) >= limit:
            break
    return entry_ids


def active_pipeline_entry_ids(app: FastAPI) -> set[int]:
    active_ids: set[int] = set()
    runs = getattr(app.state, "pipeline_runs", {}) or {}
    for entry_id, state in runs.items():
        task = state.get("task")
        if state.get("running") and task and not task.done():
            active_ids.add(int(entry_id))
    return active_ids


def pipeline_run_payload_if_known(app: FastAPI, entry_id: int) -> dict[str, Any] | None:
    state = (getattr(app.state, "pipeline_runs", {}) or {}).get(entry_id)
    if state is None:
        return None
    task = state.get("task")
    task_alive = bool(task and not task.done())
    return {
        "entry_id": entry_id,
        "running": bool(state.get("running") and task_alive),
        "phase": state.get("phase") or "idle",
        "stages": state.get("stages") or {},
        "events": list(state.get("events") or []),
        "error": state.get("error"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "result": state.get("result"),
    }


def pipeline_progress_from_stages(stages: dict[str, Any]) -> int:
    if not stages:
        return 5
    tracked = [name for name in PIPELINE_PHASE_ORDER if name in stages]
    total = max(len(PIPELINE_PHASE_ORDER), 1)
    done = sum(1 for name in tracked if (stages.get(name) or {}).get("status") == "done")
    has_running = any((stages.get(name) or {}).get("status") == "running" for name in tracked)
    progress = int((done / total) * 100)
    if has_running:
        progress += max(4, int(100 / total / 2))
    return max(5, min(progress, 99))


def derived_stage_progress(stage: str | None) -> int:
    return {
        "received": 0,
        "sorted": 20,
        "enriched": 45,
        "rewritten": 70,
        "ready": 90,
        "published": 100,
    }.get(stage or "", 0)


def content_state_activity(state: ContentPipelineState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    for index, (field_name, phase, phase_label) in enumerate(CONTENT_PIPELINE_FIELDS):
        status = getattr(state, field_name, None)
        if status in CONTENT_ACTIVE_STATUS_VALUES:
            return {
                "active_kind": "collector",
                "phase": phase,
                "label": f"В работе: {phase}",
                "progress": int(((index + 0.5) / len(CONTENT_PIPELINE_FIELDS)) * 100),
                "phase_label": phase_label,
            }
    return None


def entry_error_text(entry: PipelineEntry, state: ContentPipelineState | None = None) -> str | None:
    failed_entry_statuses = {
        "rewrite_failed",
        "link_summary_failed",
        "publish_failed",
        "publish_failed_media",
        "publish_failed_media_verification",
        "missing_media",
        "blocked",
    }
    if entry.status in failed_entry_statuses or not entry.publication_allowed:
        return str(entry.last_error or entry.blocked_reason or "")
    if state and state.last_error:
        for field_name, _phase, _label in CONTENT_PIPELINE_FIELDS:
            if getattr(state, field_name, None) == "failed":
                return str(state.last_error)
    return None


def active_sql_condition(active_ids: set[int]):
    conditions: list[Any] = [
        PipelineEntry.status == "rewrite_running",
        select(RewriteAttempt.id)
        .where(RewriteAttempt.pipeline_entry_id == PipelineEntry.id, RewriteAttempt.status == "running")
        .exists(),
    ]
    for field_name, _phase, _label in CONTENT_PIPELINE_FIELDS:
        conditions.append(getattr(ContentPipelineState, field_name).in_(CONTENT_ACTIVE_STATUS_VALUES))
    if active_ids:
        conditions.append(PipelineEntry.id.in_(active_ids))
    return or_(*conditions)


def iso_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def board_state_for_entries(app: FastAPI, session, entry_ids: list[int]) -> dict[int, dict[str, Any]]:
    entry_ids = parse_entry_ids(",".join(str(entry_id) for entry_id in entry_ids))
    if not entry_ids:
        return {}
    rows = list(
        (
            await session.execute(
                select(PipelineEntry, ContentPipelineState)
                .outerjoin(ContentPipelineState, ContentPipelineState.post_id == PipelineEntry.source_post_id)
                .where(PipelineEntry.id.in_(entry_ids))
            )
        ).all()
    )
    entries = {entry.id: (entry, state) for entry, state in rows}
    attempts: dict[int, RewriteAttempt] = {}
    attempt_rows = list(
        (
            await session.execute(
                select(RewriteAttempt)
                .where(RewriteAttempt.pipeline_entry_id.in_(entry_ids), RewriteAttempt.status == "running")
                .order_by(RewriteAttempt.pipeline_entry_id, RewriteAttempt.id.desc())
            )
        ).scalars()
    )
    for attempt in attempt_rows:
        attempts.setdefault(attempt.pipeline_entry_id, attempt)

    payloads: dict[int, dict[str, Any]] = {}
    for entry_id in entry_ids:
        pair = entries.get(entry_id)
        if pair is None:
            continue
        entry, content_state = pair
        active = False
        active_kind: str | None = None
        phase: str | None = None
        label: str | None = None
        progress: int | None = derived_stage_progress(entry.stage)
        last_error = entry_error_text(entry, content_state)

        run_state = pipeline_run_payload_if_known(app, entry_id)
        if run_state and run_state.get("running"):
            active = True
            active_kind = "targeted_pipeline"
            phase = str(run_state.get("phase") or "pipeline")
            label = f"В работе: {phase}"
            progress = pipeline_progress_from_stages(run_state.get("stages") or {})
            last_error = run_state.get("error") or last_error
        else:
            if run_state and run_state.get("error"):
                last_error = str(run_state["error"])
            running_attempt = attempts.get(entry_id)
            if running_attempt or entry.status == "rewrite_running":
                active = True
                active_kind = "rewrite"
                phase = "rewrite"
                label = "В работе: rewrite"
                progress = rewrite_progress(entry, running_attempt)
                last_error = (running_attempt.error if running_attempt and running_attempt.error else None) or last_error
            else:
                content_activity = content_state_activity(content_state)
                if content_activity:
                    active = True
                    active_kind = content_activity["active_kind"]
                    phase = content_activity["phase"]
                    label = content_activity["label"]
                    progress = content_activity["progress"]

        payloads[entry_id] = {
            "entry_id": entry_id,
            "active": active,
            "active_kind": active_kind,
            "phase": phase,
            "label": label,
            "progress": progress,
            "last_error": last_error,
            "moved": False,
            "stage": entry.stage,
            "status": entry.status,
            "last_operation_at": iso_datetime(entry.last_operation_at),
            "updated_at": iso_datetime(entry.updated_at),
        }
    return payloads


async def active_pipeline_snapshot_for_app(app: FastAPI, session) -> dict[str, Any] | None:
    for entry_id, state in (getattr(app.state, "pipeline_runs", {}) or {}).items():
        task = state.get("task")
        if state.get("running") and task and not task.done():
            return {
                "entry_id": int(entry_id),
                "phase": state.get("phase") or "pipeline",
                "label": state.get("phase") or "pipeline",
                "updated_at": state.get("started_at"),
            }
    return await active_pipeline_work_snapshot(session)


async def pipeline_entry_post_id(app: FastAPI, entry_id: int) -> tuple[int | None, int | None]:
    async with create_session_factory(app.state.settings)() as session:
        row = (
            await session.execute(
                select(PipelineEntry.source_post_id, PipelineEntry.content_item_id).where(PipelineEntry.id == entry_id)
            )
        ).first()
    return tuple(row) if row else (None, None)


async def sync_entry_stage_for_post(app: FastAPI, post_id: int) -> dict[str, Any]:
    async with create_session_factory(app.state.settings)() as session:
        entry = await sync_pipeline_entry_stage(session, post_id)
        await session.commit()
        return {"entry_id": entry.id, "stage": entry.stage, "status": entry.status, "content_item_id": entry.content_item_id} if entry else {}


async def run_pipeline_stage(state: dict[str, Any], stage_name: str, coro) -> Any:
    stages = state.setdefault("stages", {})
    stages[stage_name] = {"status": "running", "started_at": now_moscow_iso(), "finished_at": None, "result": None, "error": None}
    state["phase"] = stage_name
    state.setdefault("events", []).append(f"{stage_name} started")
    try:
        result = await coro
    except Exception as exc:
        stages[stage_name].update({"status": "failed", "finished_at": now_moscow_iso(), "error": str(exc)[:800]})
        state.setdefault("events", []).append(f"{stage_name} failed: {str(exc)[:240]}")
        raise
    stages[stage_name].update({"status": "done", "finished_at": now_moscow_iso(), "result": result})
    state.setdefault("events", []).append(f"{stage_name} done: {result}")
    return result


async def pipeline_entry_loop(app: FastAPI, entry_id: int, lock: PipelineWorkLock | None = None) -> None:
    state = pipeline_run_state(app, entry_id)
    state.update(
        {
            "running": True,
            "phase": "starting",
            "stages": {},
            "events": ["pipeline requested"],
            "error": None,
            "started_at": now_moscow_iso(),
            "finished_at": None,
            "result": None,
        }
    )
    if lock is None:
        lock = await try_acquire_pipeline_work_lock(app.state.settings, owner="web_pipeline", entry_id=entry_id)
        if lock is None:
            async with create_session_factory(app.state.settings)() as session:
                active = await active_pipeline_snapshot_for_app(app, session)
            state.update(
                {
                    "running": False,
                    "phase": "busy",
                    "error": None,
                    "finished_at": now_moscow_iso(),
                    "result": {"ok": False, "busy": True, "active": active},
                }
            )
            return
    try:
        post_id, content_item_id = await pipeline_entry_post_id(app, entry_id)
        if post_id is None:
            raise HTTPException(404, "entry_not_found")
        await run_pipeline_stage(state, "process_post", process_post(post_id))
        await run_pipeline_stage(state, "extract_links", extract_post_links(post_id))
        await run_pipeline_stage(state, "enrich_links", enrich_post_links(post_id))
        telegram_media = await run_pipeline_stage(state, "telegram_media", register_telegram_media_for_post(post_id))
        link_images = await run_pipeline_stage(state, "link_images", download_link_images_for_post(post_id))
        state["stages"]["download_media"] = {
            "status": "done",
            "started_at": state["stages"]["telegram_media"]["started_at"],
            "finished_at": now_moscow_iso(),
            "result": {"telegram_media": telegram_media, "link_images": link_images},
            "error": None,
        }
        await run_pipeline_stage(state, "summarize_links", summarize_post_links(post_id))
        await run_pipeline_stage(state, "build_material", build_post_material(post_id, refresh=True))
        await run_pipeline_stage(state, "classify_post", classify_post(post_id))
        await run_pipeline_stage(state, "refresh_pipeline_entry", sync_entry_stage_for_post(app, post_id))
        await run_pipeline_stage(
            state,
            "enriched_rewrite",
            rewrite_one(entry_id, ignore_backlog_cap=True, allow_pending_link_summaries=False),
        )
        await run_pipeline_stage(state, "refresh_after_rewrite", sync_entry_stage_for_post(app, post_id))
        state["phase"] = "done"
        state["result"] = {"ok": True}
    except asyncio.CancelledError:
        state["phase"] = "cancelled"
        state["error"] = "pipeline_cancelled"
        raise
    except Exception as exc:
        state["phase"] = "failed"
        state["error"] = str(exc)[:800]
        state["result"] = {"ok": False, "error": state["error"]}
    finally:
        if lock is not None:
            await lock.release()
        state["running"] = False
        state["finished_at"] = now_moscow_iso()


async def rewrite_worker_loop(app: FastAPI, *, limit: int, interval_seconds: int) -> None:
    state = rewrite_worker_state(app)
    state.update(
        {
            "running": True,
            "stop_requested": False,
            "phase": "starting",
            "started_at": now_moscow_iso(),
            "stopped_at": None,
            "last_heartbeat": now_moscow_iso(),
            "last_error": None,
            "last_result": None,
            "cycles": 0,
            "limit": limit,
            "interval_seconds": interval_seconds,
        }
    )
    try:
        while not state.get("stop_requested"):
            state["phase"] = "running_cycle"
            state["last_heartbeat"] = now_moscow_iso()
            lock = await try_acquire_pipeline_work_lock(app.state.settings, owner="web_rewrite_worker")
            if lock is None:
                async with create_session_factory(app.state.settings)() as session:
                    active = await active_pipeline_snapshot_for_app(app, session)
                result = {"rewritten": 0, "busy": True, "active": active}
                state["phase"] = "busy"
            else:
                try:
                    result = await rewrite_ready_pipeline(limit=1, ignore_backlog_cap=True)
                finally:
                    await lock.release()
            state["cycles"] = int(state.get("cycles") or 0) + 1
            state["last_result"] = result
            state["last_error"] = None
            state["last_heartbeat"] = now_moscow_iso()
            if state.get("stop_requested"):
                break
            state["phase"] = "sleeping"
            for _ in range(interval_seconds):
                if state.get("stop_requested"):
                    break
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        state["phase"] = "stopped"
        raise
    except Exception as exc:
        state["last_error"] = str(exc)[:800]
        state["phase"] = "failed"
    finally:
        stopped_by_request = bool(state.get("stop_requested"))
        state["running"] = False
        if state.get("phase") not in {"failed"}:
            state["phase"] = "stopped" if stopped_by_request else "idle"
        state["stop_requested"] = False
        state["stopped_at"] = now_moscow_iso()
        state["last_heartbeat"] = now_moscow_iso()


async def scalar_count(session, stmt) -> int:
    return int((await session.execute(stmt)).scalar_one() or 0)


async def rewrite_progress_data(session) -> dict[str, Any]:
    scope_filter = (
        PipelineEntry.is_eligible.is_(True),
        PipelineEntry.publication_allowed.is_(True),
    )
    done_statuses = {READY_DRAFT_STATUS, "needs_review", "published"}
    blocked_statuses = {"link_summary_pending", "link_summary_failed"}
    failed_statuses = {"rewrite_failed"}

    total_entries = await scalar_count(session, select(func.count()).select_from(PipelineEntry))
    scope_total = await scalar_count(session, select(func.count()).select_from(PipelineEntry).where(*scope_filter))
    done = await scalar_count(
        session,
        select(func.count())
        .select_from(PipelineEntry)
        .where(*scope_filter, PipelineEntry.status.in_(done_statuses)),
    )
    drafts = await scalar_count(
        session,
        select(func.count())
        .select_from(PipelineEntry)
        .where(*scope_filter, PipelineEntry.latest_draft_id.is_not(None)),
    )
    running = await scalar_count(
        session,
        select(func.count())
        .select_from(PipelineEntry)
        .where(*scope_filter, PipelineEntry.status == "rewrite_running"),
    )
    blocked_pending = await scalar_count(
        session,
        select(func.count())
        .select_from(PipelineEntry)
        .where(*scope_filter, PipelineEntry.status == "link_summary_pending"),
    )
    blocked_failed = await scalar_count(
        session,
        select(func.count())
        .select_from(PipelineEntry)
        .where(*scope_filter, PipelineEntry.status == "link_summary_failed"),
    )
    failed = await scalar_count(
        session,
        select(func.count())
        .select_from(PipelineEntry)
        .where(*scope_filter, PipelineEntry.status.in_(failed_statuses)),
    )
    waiting = max(scope_total - done - running - blocked_pending - blocked_failed - failed, 0)

    article_filter = PostLink.url_type.in_(["article", "arxiv", "telegram"])
    article_links = await scalar_count(session, select(func.count()).select_from(PostLink).where(article_filter))
    article_summaries = await scalar_count(
        session,
        select(func.count())
        .select_from(PostLink)
        .join(LinkSnapshot, LinkSnapshot.link_id == PostLink.id)
        .where(article_filter, LinkSnapshot.summary_short.is_not(None)),
    )
    article_failed = await scalar_count(
        session,
        select(func.count())
        .select_from(PostLink)
        .outerjoin(LinkSnapshot, LinkSnapshot.link_id == PostLink.id)
        .where(article_filter, or_(PostLink.extraction_status == "failed", LinkSnapshot.error.is_not(None))),
    )
    article_pending = max(article_links - article_summaries - article_failed, 0)

    status_rows = list(
        (
            await session.execute(
                select(PipelineEntry.status, func.count())
                .group_by(PipelineEntry.status)
                .order_by(func.count().desc(), PipelineEntry.status)
            )
        ).all()
    )
    attempt_rows = list(
        (
            await session.execute(
                select(RewriteAttempt.status, func.count())
                .group_by(RewriteAttempt.status)
                .order_by(func.count().desc(), RewriteAttempt.status)
            )
        ).all()
    )
    recent_rows = list(
        (
            await session.execute(
                select(RewriteAttempt, PipelineEntry, ContentItem)
                .join(PipelineEntry, PipelineEntry.id == RewriteAttempt.pipeline_entry_id)
                .join(ContentItem, ContentItem.id == PipelineEntry.content_item_id)
                .order_by(RewriteAttempt.id.desc())
                .limit(30)
            )
        ).all()
    )

    progress = round((done / scope_total) * 100, 1) if scope_total else 0
    summary_progress = round((article_summaries / article_links) * 100, 1) if article_links else 0
    return {
        "updated_at": datetime.now(ZoneInfo("Europe/Moscow")),
        "progress": progress,
        "summary_progress": summary_progress,
        "metrics": {
            "total_entries": total_entries,
            "scope_total": scope_total,
            "done": done,
            "drafts": drafts,
            "waiting": waiting,
            "running": running,
            "blocked_pending": blocked_pending,
            "blocked_failed": blocked_failed,
            "failed": failed,
            "article_links": article_links,
            "article_summaries": article_summaries,
            "article_pending": article_pending,
            "article_failed": article_failed,
        },
        "status_rows": [{"status": status or "нет", "count": int(count or 0)} for status, count in status_rows],
        "attempt_rows": [{"status": status or "нет", "count": int(count or 0)} for status, count in attempt_rows],
        "recent_attempts": [
            {
                "attempt": serialize_rewrite_attempt(attempt),
                "entry_id": entry.id,
                "entry_status": entry.status,
                "entry_error": entry.last_error,
                "title": item.translated_title or item.title or "Без заголовка",
                "source_post_id": entry.source_post_id,
            }
            for attempt, entry, item in recent_rows
        ],
    }


async def reset_link_summary_queue(session) -> dict[str, Any]:
    blocked_statuses = (LINK_SUMMARY_PENDING_STATUS, LINK_SUMMARY_FAILED_STATUS)
    rows = list(
        (
            await session.execute(
                select(PipelineEntry.id, PipelineEntry.source_post_id, PublicationDraft.status)
                .outerjoin(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
                .where(PipelineEntry.status.in_(blocked_statuses))
                .order_by(PipelineEntry.id)
            )
        ).all()
    )
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    reset_to_draft = 0
    reset_to_pending = 0
    post_ids: list[int] = []
    entry_ids: list[int] = []
    for entry_id, source_post_id, draft_status in rows:
        next_status = draft_status or "rewrite_pending"
        if draft_status:
            reset_to_draft += 1
        else:
            reset_to_pending += 1
        post_ids.append(source_post_id)
        entry_ids.append(entry_id)
        await session.execute(
            update(PipelineEntry)
            .where(PipelineEntry.id == entry_id)
            .values(status=next_status, last_error=None, updated_at=now)
        )
    attempts_reset = 0
    if entry_ids:
        attempt_result = await session.execute(
            update(RewriteAttempt)
            .where(
                RewriteAttempt.pipeline_entry_id.in_(entry_ids),
                RewriteAttempt.status == "blocked",
                RewriteAttempt.error.like("article link summaries%"),
            )
            .values(
                status="reset",
                error=None,
                response_raw={"events": ["link summary queue reset"]},
                finished_at=now,
            )
        )
        attempts_reset = int(attempt_result.rowcount or 0)
    if post_ids:
        await session.execute(
            update(ContentPipelineState)
            .where(ContentPipelineState.post_id.in_(post_ids))
            .values(rewrite_status="pending", last_error=None, retry_count=0, updated_at=now)
        )
    await session.commit()
    return {
        "ok": True,
        "reset": len(rows),
        "reset_to_draft": reset_to_draft,
        "reset_to_pending": reset_to_pending,
        "attempts_reset": attempts_reset,
    }


def register_routes(app: FastAPI) -> None:
    auth = Depends(require_auth)

    @app.get("/health")
    async def health(request: Request):
        started = datetime.now(ZoneInfo("UTC"))
        checks: dict[str, Any] = {"web": "ok"}
        try:
            async with session_factory(request)() as session:
                await session.execute(text("select 1"))
            checks["db"] = "ok"
        except Exception as exc:
            checks["db"] = "failed"
            checks["error"] = str(exc)[:300]
            raise HTTPException(status_code=503, detail=checks) from exc
        checks["checked_at"] = started.isoformat()
        return checks

    @app.get("/", response_class=HTMLResponse, dependencies=[auth])
    async def index() -> RedirectResponse:
        return RedirectResponse("/processed")

    @app.get("/processed", response_class=HTMLResponse, dependencies=[auth])
    async def processed_page(request: Request, limit: int = 50) -> HTMLResponse:
        return templates.TemplateResponse(request, "processed.html", {"rows": await processed_rows(request, limit)})

    @app.get("/yandex-genres", response_class=HTMLResponse, dependencies=[auth])
    async def yandex_genres_page(request: Request, run_id: str | None = None, genre: str | None = None, limit: int = 100) -> HTMLResponse:
        async with session_factory(request)() as session:
            run_result = await session.execute(
                select(YandexGenreClassification.run_id, func.count(), func.max(YandexGenreClassification.created_at))
                .group_by(YandexGenreClassification.run_id)
                .order_by(func.max(YandexGenreClassification.created_at).desc())
            )
            runs = list(run_result.all())
            genre_stmt = select(YandexGenreClassification.genre_primary, func.count()).group_by(YandexGenreClassification.genre_primary)
            if run_id:
                genre_stmt = genre_stmt.where(YandexGenreClassification.run_id == run_id)
            genre_result = await session.execute(genre_stmt.order_by(YandexGenreClassification.genre_primary))
            genres = list(genre_result.all())
            stmt = (
                select(YandexGenreClassification, ContentItem, TelegramPost, PublicationDraft)
                .outerjoin(ContentItem, ContentItem.id == YandexGenreClassification.content_item_id)
                .outerjoin(TelegramPost, TelegramPost.id == YandexGenreClassification.source_post_id)
                .outerjoin(PublicationDraft, PublicationDraft.source_post_id == YandexGenreClassification.source_post_id)
            )
            if run_id:
                stmt = stmt.where(YandexGenreClassification.run_id == run_id)
            if genre:
                stmt = stmt.where(YandexGenreClassification.genre_primary == genre)
            stmt = stmt.order_by(YandexGenreClassification.created_at.desc(), YandexGenreClassification.id.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = list(result.all())
        db_results = [row[0] for row in rows]
        stats = {
            "rows": len(db_results),
            "total_tokens": sum(usage_total_tokens(row.usage) for row in db_results),
            "needs_review": sum(1 for row in db_results if row.needs_review),
        }
        if db_results:
            stats.update(
                {
                    "avg_difficulty": sum(row.difficulty_score for row in db_results) / len(db_results),
                    "avg_promo": sum(row.promo_score for row in db_results) / len(db_results),
                    "avg_opinion": sum(row.opinion_score for row in db_results) / len(db_results),
                    "avg_event": sum(row.event_score for row in db_results) / len(db_results),
                }
            )
        artifact = latest_yandex_genre_artifact(request.app.state.settings)
        if artifact and genre:
            artifact["rows"] = [row for row in artifact.get("rows", []) if row.get("genre_primary") == genre]
        async with session_factory(request)() as session:
            await enrich_artifact_rows(session, artifact)
        return templates.TemplateResponse(
            request,
            "yandex_genres.html",
            {
                "runs": runs,
                "genres": genres,
                "rows": rows,
                "stats": stats,
                "selected_run_id": run_id,
                "selected_genre": genre,
                "limit": limit,
                "artifact": artifact,
                "usage_total_tokens": usage_total_tokens,
            },
        )

    @app.get("/codex-training", response_class=HTMLResponse, dependencies=[auth])
    async def codex_training_page(
        request: Request,
        run_id: str | None = None,
        q: str | None = None,
        genre: str | None = None,
        match: str | None = None,
        split: str | None = None,
        model_version: str | None = None,
        limit: int = 100,
        page: int = 1,
    ) -> HTMLResponse:
        limit = max(1, min(limit, 500))
        page = max(1, page)
        search_query = (q or "").strip()
        async with session_factory(request)() as session:
            run_rows = await session.execute(select(CodexTrainingRun).order_by(CodexTrainingRun.updated_at.desc()))
            runs = list(run_rows.scalars())
            if not run_id and runs:
                run_id = runs[0].run_id

            genre_stmt = select(CodexGenreClassification.genre_primary, func.count()).group_by(CodexGenreClassification.genre_primary)
            model_stmt = select(CodexGenreModelComparison.model_version, func.count()).group_by(CodexGenreModelComparison.model_version)
            if run_id:
                genre_stmt = genre_stmt.where(CodexGenreClassification.run_id == run_id)
                model_stmt = model_stmt.where(CodexGenreModelComparison.run_id == run_id)
            genres = list((await session.execute(genre_stmt.order_by(CodexGenreClassification.genre_primary))).all())
            model_versions = list((await session.execute(model_stmt.order_by(CodexGenreModelComparison.model_version.desc()))).all())

            stmt = (
                select(CodexGenreModelComparison, ContentItem, TelegramPost)
                .outerjoin(ContentItem, ContentItem.id == CodexGenreModelComparison.content_item_id)
                .outerjoin(TelegramPost, TelegramPost.id == CodexGenreModelComparison.source_post_id)
            )
            count_stmt = (
                select(func.count(CodexGenreModelComparison.id))
                .select_from(CodexGenreModelComparison)
                .outerjoin(ContentItem, ContentItem.id == CodexGenreModelComparison.content_item_id)
                .outerjoin(TelegramPost, TelegramPost.id == CodexGenreModelComparison.source_post_id)
            )
            filters = []
            if run_id:
                filters.append(CodexGenreModelComparison.run_id == run_id)
            if genre:
                filters.append(CodexGenreModelComparison.teacher_genre == genre)
            if split:
                filters.append(CodexGenreModelComparison.split == split)
            if model_version:
                filters.append(CodexGenreModelComparison.model_version == model_version)
            if match == "ok":
                filters.append(CodexGenreModelComparison.match_percent >= 90)
            elif match == "bad":
                filters.append(CodexGenreModelComparison.match_percent < 90)
            if search_query:
                pattern = f"%{search_query}%"
                filters.append(
                    or_(
                        ContentItem.title.ilike(pattern),
                        ContentItem.translated_title.ilike(pattern),
                        ContentItem.main_text.ilike(pattern),
                        ContentItem.source_summary.ilike(pattern),
                        ContentItem.translated_summary.ilike(pattern),
                        TelegramPost.text.ilike(pattern),
                    )
                )
            for condition in filters:
                stmt = stmt.where(condition)
                count_stmt = count_stmt.where(condition)
            filtered_total = int((await session.execute(count_stmt)).scalar_one() or 0)
            total_pages = max(1, (filtered_total + limit - 1) // limit)
            page = min(page, total_pages)
            offset = (page - 1) * limit
            stmt = (
                stmt.order_by(CodexGenreModelComparison.created_at.desc(), CodexGenreModelComparison.id.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = list((await session.execute(stmt)).all())

            current_run = next((item for item in runs if item.run_id == run_id), None)
            all_stats_stmt = select(
                func.count(CodexGenreModelComparison.id),
                func.avg(CodexGenreModelComparison.match_percent),
                func.max(CodexGenreModelComparison.match_percent),
                func.min(CodexGenreModelComparison.match_percent),
            )
            if run_id:
                all_stats_stmt = all_stats_stmt.where(CodexGenreModelComparison.run_id == run_id)
            total, avg_match, best_match, worst_match = (await session.execute(all_stats_stmt)).one()
            current_model_version = model_version or (current_run.latest_model_version if current_run else None)
            current_total = current_avg = current_best = current_worst = None
            if current_model_version:
                current_stats_stmt = select(
                    func.count(CodexGenreModelComparison.id),
                    func.avg(CodexGenreModelComparison.match_percent),
                    func.max(CodexGenreModelComparison.match_percent),
                    func.min(CodexGenreModelComparison.match_percent),
                ).where(CodexGenreModelComparison.model_version == current_model_version)
                if run_id:
                    current_stats_stmt = current_stats_stmt.where(CodexGenreModelComparison.run_id == run_id)
                current_total, current_avg, current_best, current_worst = (await session.execute(current_stats_stmt)).one()
            label_count_stmt = select(func.count()).select_from(CodexGenreClassification)
            if run_id:
                label_count_stmt = label_count_stmt.where(CodexGenreClassification.run_id == run_id)
            label_count = (await session.execute(label_count_stmt)).scalar_one()
        stats = {
            "labels": label_count,
            "comparisons": total or 0,
            "filtered_comparisons": filtered_total,
            "current_comparisons": current_total or 0,
            "avg_match": float((current_avg if current_avg is not None else avg_match) or 0),
            "best_match": float((current_best if current_best is not None else best_match) or 0),
            "worst_match": float((current_worst if current_worst is not None else worst_match) or 0),
            "history_worst_match": float(worst_match or 0),
            "current_model_version": current_model_version,
            "latest_match": float(current_run.latest_match_percent or 0) if current_run and current_run.latest_match_percent is not None else 0,
        }
        series_history: list[dict[str, Any]] = []
        series_id = None
        if current_run:
            run_metrics = current_run.metrics or {}
            snapshot = run_metrics.get("series_snapshot") or {}
            series_id = snapshot.get("series_id") or run_metrics.get("series_id")
        if series_id:
            app_settings: Settings = request.app.state.settings
            stats_path = Path(app_settings.artifacts_dir) / "codex_training_series" / series_id / "series_stats.jsonl"
            if stats_path.exists():
                for line in stats_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        series_history.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                series_history = list(reversed(series_history[-20:]))
        return templates.TemplateResponse(
            request,
            "codex_training.html",
            {
                "runs": runs,
                "current_run": current_run,
                "rows": rows,
                "stats": stats,
                "genres": genres,
                "model_versions": model_versions,
                "selected_run_id": run_id,
                "search_query": search_query,
                "selected_genre": genre,
                "selected_match": match,
                "selected_split": split,
                "selected_model_version": model_version,
                "limit": limit,
                "page": page,
                "total_pages": total_pages,
                "has_prev_page": page > 1,
                "has_next_page": page < total_pages,
                "prev_page": max(1, page - 1),
                "next_page": min(total_pages, page + 1),
                "series_history": series_history,
            },
        )

    @app.get("/topic-audit", response_class=HTMLResponse, dependencies=[auth])
    async def topic_audit_page(request: Request) -> HTMLResponse:
        settings: Settings = request.app.state.settings
        summary = latest_topic_audit_summary(report_root(settings))
        return templates.TemplateResponse(
            request,
            "topic_audit.html",
            {
                "summary": summary,
                "topics": summary.get("topics", []) if summary else [],
                "totals": summary.get("totals", {}) if summary else {},
                "breakdowns": summary.get("breakdowns", {}) if summary else {},
                "artifacts": summary.get("artifacts", {}) if summary else {},
            },
        )

    @app.get("/processed/{item_id}", response_class=HTMLResponse, dependencies=[auth])
    async def processed_detail(request: Request, item_id: int) -> HTMLResponse:
        async with session_factory(request)() as session:
            result = await session.execute(
                select(ContentItem, TelegramPost, TelegramChat, PostClassification, PublicationDraft)
                .join(TelegramPost, TelegramPost.id == ContentItem.source_post_id)
                .outerjoin(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                .outerjoin(PostClassification, PostClassification.content_item_id == ContentItem.id)
                .outerjoin(PublicationDraft, PublicationDraft.source_post_id == ContentItem.source_post_id)
                .where(ContentItem.id == item_id)
            )
            row = result.first()
            if row is None:
                raise HTTPException(404)
            item = row[0]
            link_rows = await session.execute(
                select(PostLink, LinkSnapshot)
                .outerjoin(LinkSnapshot, LinkSnapshot.link_id == PostLink.id)
                .where(PostLink.post_id == item.source_post_id)
                .order_by(PostLink.id)
            )
            draft_rows = await session.execute(select(PublicationDraft).where(PublicationDraft.source_post_id == item.source_post_id).order_by(PublicationDraft.id))
            asset_ids = {item.primary_image_asset_id}
            drafts = list(draft_rows.scalars())
            asset_ids.update(draft.image_asset_id for draft in drafts)
            asset_ids.discard(None)
            assets = []
            if asset_ids:
                assets = list((await session.execute(select(MediaAsset).where(MediaAsset.id.in_(asset_ids)))).scalars())
            return templates.TemplateResponse(
                request,
                "processed_detail.html",
                {"row": row, "links": list(link_rows.all()), "drafts": drafts, "assets": assets},
            )

    @app.get("/drafts", response_class=HTMLResponse, dependencies=[auth])
    async def drafts_page(request: Request, status: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(request, "drafts.html", {"rows": await draft_rows(request, status), "status": status})

    @app.post("/api/drafts/{draft_id}/approve", dependencies=[auth])
    async def approve_draft(request: Request, draft_id: int):
        async with session_factory(request)() as session:
            await session.execute(update(PublicationDraft).where(PublicationDraft.id == draft_id).values(status="approved"))
            await session.commit()
        return RedirectResponse("/drafts", status_code=303)

    @app.post("/api/drafts/{draft_id}/reject", dependencies=[auth])
    async def reject_draft(request: Request, draft_id: int):
        async with session_factory(request)() as session:
            await session.execute(update(PublicationDraft).where(PublicationDraft.id == draft_id).values(status="rejected"))
            await session.commit()
        return RedirectResponse("/drafts", status_code=303)

    @app.post("/api/drafts/{draft_id}/edit", dependencies=[auth])
    async def edit_draft(request: Request, draft_id: int, title: str = Form(""), body: str = Form(""), tags: str = Form("")):
        async with session_factory(request)() as session:
            await session.execute(
                update(PublicationDraft)
                .where(PublicationDraft.id == draft_id)
                .values(title=title, body=body, tags=[tag.strip() for tag in tags.split(",") if tag.strip()], status="needs_review")
            )
            await session.commit()
        return RedirectResponse("/drafts", status_code=303)

    @app.get("/labels", response_class=HTMLResponse, dependencies=[auth])
    async def labels_page(request: Request, limit: int = 100) -> HTMLResponse:
        async with session_factory(request)() as session:
            result = await session.execute(
                select(LabelingQueue, PostProcessed, ContentItem)
                .join(PostProcessed, PostProcessed.post_id == LabelingQueue.post_id)
                .outerjoin(ContentItem, ContentItem.source_post_id == LabelingQueue.post_id)
                .where(LabelingQueue.status == "pending")
                .order_by(LabelingQueue.id)
                .limit(limit)
            )
            return templates.TemplateResponse(request, "labels.html", {"rows": result.all()})

    @app.post("/api/labels/{post_id}", dependencies=[auth])
    async def save_label(request: Request, post_id: int, label: str = Form(...), reviewer: str = Form("web")):
        async with session_factory(request)() as session:
            await session.execute(
                PostLabel.__table__.insert().values(
                    post_id=post_id,
                    label=label,
                    label_set_version="v1",
                    confidence=1.0,
                    source="human",
                    status="accepted",
                    created_by=reviewer,
                    reviewed_by=reviewer,
                    raw={},
                )
            )
            await session.execute(update(LabelingQueue).where(LabelingQueue.post_id == post_id, LabelingQueue.status == "pending").values(status="reviewed", human_label=label, reviewer=reviewer))
            await session.commit()
        return RedirectResponse("/labels", status_code=303)

    @app.get("/models", response_class=HTMLResponse, dependencies=[auth])
    async def models_page(request: Request) -> HTMLResponse:
        async with session_factory(request)() as session:
            result = await session.execute(select(ModelVersion).order_by(ModelVersion.id.desc()).limit(50))
            return templates.TemplateResponse(request, "models.html", {"models": list(result.scalars())})

    @app.post("/api/models/{model_id}/promote", dependencies=[auth])
    async def promote_model(request: Request, model_id: int):
        async with session_factory(request)() as session:
            model = (await session.execute(select(ModelVersion).where(ModelVersion.id == model_id))).scalar_one_or_none()
            if not model:
                raise HTTPException(404)
            await session.execute(update(ModelVersion).where(ModelVersion.model_name == model.model_name, ModelVersion.status == "active").values(status="archived"))
            await session.execute(update(ModelVersion).where(ModelVersion.id == model_id).values(status="active"))
            await session.commit()
        return RedirectResponse("/models", status_code=303)

    @app.get("/pipeline", response_class=HTMLResponse, dependencies=[auth])
    async def pipeline_page(
        request: Request,
        q: str | None = None,
    ) -> HTMLResponse:
        search_query = (q or "").strip()
        settings: Settings = request.app.state.settings
        query_params = request.query_params
        sort_options = {
            "last_operation": "последняя операция",
            "received": "дата получения",
            "post_date": "дата поста",
            "title": "заголовок",
            "status": "статус",
            "genre": "жанр",
            "draft": "дата рерайта",
            "published_at": "дата публикации",
            "id": "id",
        }

        def column_limit(stage: str) -> int:
            raw_limit = query_params.get(f"{stage}_limit", "50")
            try:
                value = int(raw_limit)
            except ValueError:
                value = 50
            return max(1, min(value, 200))

        def column_filters(stage: str) -> dict[str, Any]:
            selected_sort = query_params.get(f"{stage}_sort", "last_operation")
            selected_order = query_params.get(f"{stage}_order", "desc")
            return {
                "sort": selected_sort if selected_sort in sort_options else "last_operation",
                "order": selected_order if selected_order in {"asc", "desc"} else "desc",
                "limit": column_limit(stage),
                "genre": query_params.get(f"{stage}_genre", ""),
                "eligible": query_params.get(f"{stage}_eligible", ""),
                "active": query_params.get(f"{stage}_active", ""),
            }

        def row_statement():
            return (
                select(PipelineEntry, ContentItem, TelegramPost, TelegramChat, PostClassification, PublicationDraft, PublishedPost)
                .select_from(PipelineEntry)
                .outerjoin(ContentItem, ContentItem.id == PipelineEntry.content_item_id)
                .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                .outerjoin(PostClassification, PostClassification.id == PipelineEntry.classification_id)
                .outerjoin(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
                .outerjoin(PublishedPost, PublishedPost.id == PipelineEntry.published_post_id)
                .outerjoin(ContentPipelineState, ContentPipelineState.post_id == PipelineEntry.source_post_id)
            )

        def count_statement():
            return (
                select(func.count(PipelineEntry.id))
                .select_from(PipelineEntry)
                .outerjoin(ContentItem, ContentItem.id == PipelineEntry.content_item_id)
                .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                .outerjoin(PostClassification, PostClassification.id == PipelineEntry.classification_id)
                .outerjoin(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
                .outerjoin(PublishedPost, PublishedPost.id == PipelineEntry.published_post_id)
                .outerjoin(ContentPipelineState, ContentPipelineState.post_id == PipelineEntry.source_post_id)
            )

        def search_condition(pattern: str):
            return or_(
                ContentItem.title.ilike(pattern),
                ContentItem.translated_title.ilike(pattern),
                ContentItem.main_text.ilike(pattern),
                ContentItem.source_summary.ilike(pattern),
                ContentItem.translated_summary.ilike(pattern),
                TelegramPost.text.ilike(pattern),
                TelegramChat.title.ilike(pattern),
                PublicationDraft.title.ilike(pattern),
                PublicationDraft.body.ilike(pattern),
            )

        sort_columns = {
            "last_operation": PipelineEntry.last_operation_at,
            "received": TelegramPost.created_at,
            "post_date": TelegramPost.date,
            "title": func.coalesce(ContentItem.translated_title, ContentItem.title, TelegramPost.text),
            "status": PipelineEntry.status,
            "genre": PipelineEntry.genre_primary,
            "draft": PublicationDraft.created_at,
            "published_at": PublishedPost.published_at,
            "id": PipelineEntry.id,
        }
        async with session_factory(request)() as session:
            genre_rows = list(
                (
                    await session.execute(
                        select(PipelineEntry.genre_primary, func.count(PipelineEntry.id))
                        .select_from(PipelineEntry)
                        .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                        .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                        .where(TelegramChat.folder_name == settings.folder_name, PipelineEntry.genre_primary.is_not(None))
                        .group_by(PipelineEntry.genre_primary)
                        .order_by(PipelineEntry.genre_primary)
                    )
                ).all()
            )
            stage_count_rows = list(
                (
                    await session.execute(
                        select(PipelineEntry.stage, func.count(PipelineEntry.id))
                        .select_from(PipelineEntry)
                        .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                        .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                        .where(TelegramChat.folder_name == settings.folder_name)
                        .group_by(PipelineEntry.stage)
                    )
                ).all()
            )
            stage_counts = {stage: 0 for stage in PIPELINE_STAGES}
            stage_counts.update({stage_name: int(count or 0) for stage_name, count in stage_count_rows})
            stats_count_stmt = (
                select(func.count(PipelineEntry.id))
                .select_from(PipelineEntry)
                .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                .where(TelegramChat.folder_name == settings.folder_name)
            )

            async def count_where(*conditions: Any) -> int:
                return int((await session.execute(stats_count_stmt.where(*conditions))).scalar_one() or 0)

            stats = {
                "total": await count_where(),
                "eligible": await count_where(PipelineEntry.is_eligible.is_(True)),
                "ready": await count_where(PipelineEntry.stage == "ready"),
                "blocked": await count_where(PipelineEntry.publication_allowed.is_(False)),
                "published": await count_where(PipelineEntry.stage == "published"),
                "rewrite_pending": await count_where(PipelineEntry.status == "rewrite_pending"),
                "stage_counts": stage_counts,
            }
            columns = []
            visible_entry_ids: list[int] = []
            active_ids = active_pipeline_entry_ids(request.app)
            active_condition = active_sql_condition(active_ids)
            for stage in PIPELINE_STAGES:
                filters = column_filters(stage)
                conditions = [TelegramChat.folder_name == settings.folder_name, PipelineEntry.stage == stage]
                if filters["genre"]:
                    conditions.append(PipelineEntry.genre_primary == filters["genre"])
                if filters["eligible"] == "yes":
                    conditions.append(PipelineEntry.is_eligible.is_(True))
                elif filters["eligible"] == "no":
                    conditions.append(PipelineEntry.is_eligible.is_(False))
                if search_query:
                    conditions.append(search_condition(f"%{search_query}%"))
                if filters["active"] == "yes":
                    conditions.append(active_condition)
                total = int((await session.execute(count_statement().where(*conditions))).scalar_one() or 0)
                sort_column = sort_columns.get(filters["sort"], PipelineEntry.last_operation_at)
                order_expr = sort_column.asc().nulls_last() if filters["order"] == "asc" else sort_column.desc().nulls_last()
                active_order = case((active_condition, 1), else_=0).desc()
                rows = list(
                    (
                        await session.execute(
                            row_statement()
                            .where(*conditions)
                            .order_by(active_order, order_expr, PipelineEntry.id.desc())
                            .limit(filters["limit"])
                        )
                    ).all()
                )
                visible_entry_ids.extend(row[0].id for row in rows)
                columns.append(
                    {
                        "stage": stage,
                        "label": PIPELINE_STAGE_LABELS.get(stage, stage),
                        "count": total,
                        "rows": rows,
                        "filters": filters,
                    }
                )
            active_states = await board_state_for_entries(request.app, session, visible_entry_ids)
        return templates.TemplateResponse(
            request,
            "pipeline.html",
            {
                "columns": columns,
                "stats": stats,
                "genre_rows": genre_rows,
                "search_query": search_query,
                "stage_labels": PIPELINE_STAGE_LABELS,
                "stages": PIPELINE_STAGES,
                "sort_options": sort_options,
                "ready_status": READY_DRAFT_STATUS,
                "active_states": active_states,
            },
        )

    @app.get("/pipeline/{entry_id}", response_class=HTMLResponse, dependencies=[auth])
    async def pipeline_detail(request: Request, entry_id: int) -> HTMLResponse:
        async with session_factory(request)() as session:
            row = (
                await session.execute(
                    select(PipelineEntry, ContentItem, TelegramPost, TelegramChat, PostClassification, PublicationDraft, PublishedPost)
                    .outerjoin(ContentItem, ContentItem.id == PipelineEntry.content_item_id)
                    .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                    .outerjoin(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                    .outerjoin(PostClassification, PostClassification.id == PipelineEntry.classification_id)
                    .outerjoin(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
                    .outerjoin(PublishedPost, PublishedPost.id == PipelineEntry.published_post_id)
                    .where(PipelineEntry.id == entry_id)
                )
            ).first()
            if row is None:
                raise HTTPException(404)
            entry, _item, post, _chat, _classification, _draft, _published = row
            link_materials = await load_link_materials(session, entry.source_post_id)
            media_assets = await load_media_assets_for_post(session, entry.source_post_id, link_materials)
            post_segments = telegram_link_segments(post.text, post.raw)
            link_rows = []
            for material in link_materials:
                snapshot = material.snapshot
                link_rows.append(
                    {
                        "id": material.link.id,
                        "title": link_title(material),
                        "url": link_display_url(material),
                        "original_url": material.link.original_url,
                        "domain": material.link.domain or (snapshot.domain if snapshot else None),
                        "url_type": material.link.url_type,
                        "position_index": material.link.position_index,
                        "is_primary": material.link.is_primary,
                        "is_article_like": is_article_like_link(material.link),
                        "extraction_status": material.link.extraction_status,
                        "content_type": snapshot.content_type if snapshot else None,
                        "http_status": snapshot.http_status if snapshot else None,
                        "fetched_at": snapshot.fetched_at if snapshot else None,
                        "extracted_text": snapshot.extracted_text if snapshot else None,
                        "summary": snapshot.summary_short if snapshot else None,
                        "summary_model": snapshot.summary_model if snapshot else None,
                        "error": snapshot.error if snapshot else None,
                        "image_asset_id": snapshot.image_asset_id if snapshot else None,
                    }
                )
            attempts = list(
                (
                    await session.execute(
                        select(RewriteAttempt, RewritePromptVersion)
                        .outerjoin(RewritePromptVersion, RewritePromptVersion.id == RewriteAttempt.prompt_version_id)
                        .where(RewriteAttempt.pipeline_entry_id == entry_id)
                        .order_by(RewriteAttempt.id.desc())
                    )
                ).all()
            )
            _entry, _item, _post, _chat, _classification, draft, _published = row
            draft_display_body = strip_link_materials_section(draft.body if draft else None)
            draft_segments = markdown_link_segments(draft_display_body)
            rewrite_status = rewrite_status_payload(_entry, draft, attempts)
            source_post_url = telegram_post_source_url(post, row[3])
        return templates.TemplateResponse(
            request,
            "pipeline_detail.html",
            {
                "row": row,
                "attempts": attempts,
                "post_segments": post_segments,
                "draft_segments": draft_segments,
                "draft_display_body": draft_display_body,
                "link_rows": link_rows,
                "media_assets": media_assets,
                "rewrite_status": rewrite_status,
                "source_post_url": source_post_url,
            },
        )

    @app.get("/rewrite-progress", response_class=HTMLResponse, dependencies=[auth])
    async def rewrite_progress_page(request: Request) -> HTMLResponse:
        async with session_factory(request)() as session:
            progress = await rewrite_progress_data(session)
        progress["worker"] = public_rewrite_worker_state(request.app)
        return templates.TemplateResponse(request, "rewrite_progress.html", {"progress": progress})

    @app.get("/api/rewrite-progress", dependencies=[auth])
    async def rewrite_progress_api(request: Request):
        async with session_factory(request)() as session:
            progress = await rewrite_progress_data(session)
        progress["worker"] = public_rewrite_worker_state(request.app)
        return progress

    @app.post("/api/rewrite-progress/reset-link-summary-queue", dependencies=[auth])
    async def rewrite_progress_reset_link_summary_queue(request: Request):
        async with session_factory(request)() as session:
            result = await reset_link_summary_queue(session)
            progress = await rewrite_progress_data(session)
        progress["worker"] = public_rewrite_worker_state(request.app)
        return {**result, "progress": progress}

    @app.get("/api/rewrite-worker", dependencies=[auth])
    async def rewrite_worker_status(request: Request):
        return public_rewrite_worker_state(request.app)

    @app.post("/api/rewrite-worker/start", dependencies=[auth])
    async def rewrite_worker_start(
        request: Request,
        limit: int = Form(10),
        interval_seconds: int = Form(30),
    ):
        state = rewrite_worker_state(request.app)
        task = state.get("task")
        if task and not task.done():
            return public_rewrite_worker_state(request.app)
        actual_limit = max(1, min(int(limit or 10), 100))
        actual_interval = max(5, min(int(interval_seconds or 30), 3600))
        state["task"] = asyncio.create_task(
            rewrite_worker_loop(request.app, limit=actual_limit, interval_seconds=actual_interval)
        )
        await asyncio.sleep(0)
        return public_rewrite_worker_state(request.app)

    @app.post("/api/rewrite-worker/stop", dependencies=[auth])
    async def rewrite_worker_stop(request: Request):
        state = rewrite_worker_state(request.app)
        state["stop_requested"] = True
        task = state.get("task")
        if task and not task.done():
            state["phase"] = "stopping"
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=10)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                state["last_error"] = "worker stop timeout; cancellation is still pending"
                state["phase"] = "stopping"
        else:
            state["running"] = False
            state["stop_requested"] = False
            state["phase"] = "stopped"
            state["stopped_at"] = now_moscow_iso()
        return public_rewrite_worker_state(request.app)

    @app.get("/api/pipeline/{entry_id}/rewrite-status", dependencies=[auth])
    async def pipeline_rewrite_status(request: Request, entry_id: int):
        async with session_factory(request)() as session:
            row = (
                await session.execute(
                    select(PipelineEntry, PublicationDraft)
                    .outerjoin(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
                    .where(PipelineEntry.id == entry_id)
                )
            ).first()
            if row is None:
                raise HTTPException(404)
            entry, draft = row
            attempts = list(
                (
                    await session.execute(
                        select(RewriteAttempt, RewritePromptVersion)
                        .outerjoin(RewritePromptVersion, RewritePromptVersion.id == RewriteAttempt.prompt_version_id)
                        .where(RewriteAttempt.pipeline_entry_id == entry_id)
                        .order_by(RewriteAttempt.id.desc())
                        .limit(10)
                    )
                ).all()
            )
        return rewrite_status_payload(entry, draft, attempts)

    @app.get("/api/pipeline/board-state", dependencies=[auth])
    async def pipeline_board_state(request: Request, entry_ids: str = ""):
        ids = parse_entry_ids(entry_ids, limit=500)
        async with session_factory(request)() as session:
            entries = await board_state_for_entries(request.app, session, ids)
        return {
            "updated_at": now_moscow_iso(),
            "entries": {str(entry_id): state for entry_id, state in entries.items()},
        }

    @app.get("/api/pipeline/{entry_id}/pipeline-status", dependencies=[auth])
    async def pipeline_status(request: Request, entry_id: int):
        return public_pipeline_run_state(request.app, entry_id)

    @app.post("/api/pipeline/{entry_id}/run-pipeline", dependencies=[auth])
    async def pipeline_run(request: Request, entry_id: int):
        state = pipeline_run_state(request.app, entry_id)
        task = state.get("task")
        if task and not task.done():
            return public_pipeline_run_state(request.app, entry_id)
        lock = await try_acquire_pipeline_work_lock(
            request.app.state.settings,
            owner="manual_pipeline",
            entry_id=entry_id,
        )
        if lock is None:
            async with session_factory(request)() as session:
                active = await active_pipeline_snapshot_for_app(request.app, session)
            state.update(
                {
                    "running": False,
                    "phase": "busy",
                    "stages": {},
                    "events": ["pipeline busy"],
                    "error": None,
                    "started_at": None,
                    "finished_at": now_moscow_iso(),
                    "result": {"ok": False, "busy": True, "active": active},
                }
            )
            return {**public_pipeline_run_state(request.app, entry_id), "busy": True, "active": active}
        state["task"] = asyncio.create_task(pipeline_entry_loop(request.app, entry_id, lock))
        await asyncio.sleep(0)
        return public_pipeline_run_state(request.app, entry_id)

    @app.post("/api/pipeline/{entry_id}/block", dependencies=[auth])
    async def pipeline_block(request: Request, entry_id: int, reason: str = Form("")):
        async with session_factory(request)() as session:
            await session.execute(
                update(PipelineEntry)
                .where(PipelineEntry.id == entry_id)
                .values(publication_allowed=False, blocked_reason=reason or "manual_block", status="blocked", updated_at=datetime.now(ZoneInfo("Europe/Moscow")))
            )
            await session.commit()
        return RedirectResponse(request.headers.get("referer") or "/pipeline", status_code=303)

    @app.post("/api/pipeline/{entry_id}/allow", dependencies=[auth])
    async def pipeline_allow(request: Request, entry_id: int):
        async with session_factory(request)() as session:
            entry = (await session.execute(select(PipelineEntry).where(PipelineEntry.id == entry_id))).scalar_one_or_none()
            if not entry:
                raise HTTPException(404)
            draft = None
            if entry.latest_draft_id:
                draft = (await session.execute(select(PublicationDraft).where(PublicationDraft.id == entry.latest_draft_id))).scalar_one_or_none()
            status_value = draft.status if draft else ("rewrite_pending" if entry.is_eligible else "ineligible")
            await session.execute(
                update(PipelineEntry)
                .where(PipelineEntry.id == entry_id)
                .values(publication_allowed=True, blocked_reason=None, status=status_value, updated_at=datetime.now(ZoneInfo("Europe/Moscow")))
            )
            await session.commit()
        return RedirectResponse(request.headers.get("referer") or "/pipeline", status_code=303)

    @app.post("/api/pipeline/{entry_id}/schedule", dependencies=[auth])
    async def pipeline_schedule(request: Request, entry_id: int, scheduled_publish_at: str = Form("")):
        value = None
        raw = scheduled_publish_at.strip()
        if raw:
            value = datetime.fromisoformat(raw)
            if value.tzinfo is None:
                value = value.replace(tzinfo=ZoneInfo("Europe/Moscow"))
        async with session_factory(request)() as session:
            await session.execute(update(PipelineEntry).where(PipelineEntry.id == entry_id).values(scheduled_publish_at=value, updated_at=datetime.now(ZoneInfo("Europe/Moscow"))))
            await session.commit()
        return RedirectResponse(request.headers.get("referer") or "/pipeline", status_code=303)

    @app.post("/api/pipeline/{entry_id}/rewrite", dependencies=[auth])
    async def pipeline_rewrite(request: Request, entry_id: int):
        result = await rewrite_one(entry_id, ignore_backlog_cap=True, allow_pending_link_summaries=True)
        if request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", ""):
            return result
        return RedirectResponse(request.headers.get("referer") or f"/pipeline/{entry_id}", status_code=303)

    @app.get("/pipeline-prompts", response_class=HTMLResponse, dependencies=[auth])
    async def pipeline_prompts(request: Request) -> HTMLResponse:
        async with session_factory(request)() as session:
            prompts = []
            for prompt_name, title, description in [
                (
                    PIPELINE_REWRITE_PROMPT,
                    "Рерайт",
                    "Активная версия используется для повторных и фоновых рерайтов.",
                ),
                (
                    LINK_SUMMARY_PROMPT,
                    "Summary ссылок",
                    "Активная версия используется для summary скачанных article-like ссылок.",
                ),
            ]:
                active = await ensure_active_prompt_version(session, name=prompt_name)
                versions = list(
                    (
                        await session.execute(
                            select(RewritePromptVersion)
                            .where(RewritePromptVersion.name == active.name)
                            .order_by(RewritePromptVersion.version.desc())
                            .limit(20)
                        )
                    ).scalars()
                )
                prompts.append(
                    {
                        "name": prompt_name,
                        "title": title,
                        "description": description,
                        "active": active,
                        "versions": versions,
                        "labels_json": json.dumps(active.label_prompts or {}, ensure_ascii=False, indent=2),
                    }
                )
        return templates.TemplateResponse(request, "pipeline_prompts.html", {"prompts": prompts})

    @app.post("/pipeline-prompts", dependencies=[auth])
    async def pipeline_prompts_save(
        request: Request,
        prompt_name: str = Form(...),
        system_prompt: str = Form(...),
        common_user_prompt: str = Form(...),
        label_prompts_json: str = Form("{}"),
    ):
        if prompt_name not in {PIPELINE_REWRITE_PROMPT, LINK_SUMMARY_PROMPT}:
            raise HTTPException(400, "Unknown prompt name.")
        try:
            labels = json.loads(label_prompts_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"Invalid label prompts JSON: {exc}") from exc
        if not isinstance(labels, dict):
            raise HTTPException(400, "Label prompts must be a JSON object.")
        if prompt_name == LINK_SUMMARY_PROMPT:
            labels = {}
        async with session_factory(request)() as session:
            await create_prompt_version(
                session,
                name=prompt_name,
                system_prompt=system_prompt,
                common_user_prompt=common_user_prompt,
                label_prompts=labels,
                created_by="web",
            )
        return RedirectResponse("/pipeline-prompts", status_code=303)

    @app.get("/search", response_class=HTMLResponse, dependencies=[auth])
    async def search_page(request: Request, q: str | None = None, label: str | None = None):
        async with session_factory(request)() as session:
            result = await session.execute(build_search_statement(q=q, label=label).limit(50))
            return templates.TemplateResponse(request, "search.html", {"rows": list(result.scalars()), "q": q or "", "label": label or ""})

    @app.get("/media/{asset_id}", dependencies=[auth])
    async def media_file(request: Request, asset_id: int):
        settings: Settings = request.app.state.settings
        async with session_factory(request)() as session:
            asset = (await session.execute(select(MediaAsset).where(MediaAsset.id == asset_id))).scalar_one_or_none()
            if not asset or not asset.local_path:
                raise HTTPException(404)
            path = Path(asset.local_path).resolve()
            media_root = Path(settings.media_dir).resolve()
            if media_root not in path.parents and path != media_root:
                raise HTTPException(403)
            if not path.exists():
                raise HTTPException(404)
            return FileResponse(path, media_type=asset.mime_type)

    @app.get("/api/processed", dependencies=[auth])
    async def api_processed(request: Request):
        return [
            {"item": serialize_model(item), "classification": serialize_model(classification) if classification else None, "draft": serialize_model(draft) if draft else None}
            for item, classification, draft in await processed_rows(request)
        ]

    @app.get("/api/processed/{item_id}", dependencies=[auth])
    async def api_processed_detail(request: Request, item_id: int):
        async with session_factory(request)() as session:
            item = (await session.execute(select(ContentItem).where(ContentItem.id == item_id))).scalar_one_or_none()
            if not item:
                raise HTTPException(404)
            return serialize_model(item)

    @app.get("/api/drafts", dependencies=[auth])
    async def api_drafts(request: Request, status: str | None = None):
        return [{"draft": serialize_model(draft), "showcase": serialize_model(showcase)} for draft, _, showcase in await draft_rows(request, status)]

    @app.get("/api/labels/queue", dependencies=[auth])
    async def api_labels_queue(request: Request):
        async with session_factory(request)() as session:
            result = await session.execute(select(LabelingQueue).where(LabelingQueue.status == "pending").limit(100))
            return [serialize_model(row) for row in result.scalars()]

    @app.get("/api/models", dependencies=[auth])
    async def api_models(request: Request):
        async with session_factory(request)() as session:
            result = await session.execute(select(ModelVersion).order_by(ModelVersion.id.desc()).limit(50))
            return [serialize_model(row) for row in result.scalars()]

    @app.get("/api/search", dependencies=[auth])
    async def api_search(request: Request, q: str | None = None, label: str | None = None):
        async with session_factory(request)() as session:
            result = await session.execute(build_search_statement(q=q, label=label).limit(50))
            return [serialize_model(row) for row in result.scalars()]

    @app.get("/api/pipeline/status", dependencies=[auth])
    async def api_pipeline_status(request: Request):
        async with session_factory(request)() as session:
            total_posts = (await session.execute(select(func.count()).select_from(TelegramPost))).scalar_one()
            total_items = (await session.execute(select(func.count()).select_from(ContentItem))).scalar_one()
            total_drafts = (await session.execute(select(func.count()).select_from(PublicationDraft))).scalar_one()
            total_published = (await session.execute(select(func.count()).select_from(PublishedPost))).scalar_one()
            return {"telegram_posts": total_posts, "content_items": total_items, "drafts": total_drafts, "published": total_published}

    @app.get("/api/topic-audit/latest", dependencies=[auth])
    async def api_topic_audit_latest(request: Request):
        settings: Settings = request.app.state.settings
        summary = latest_topic_audit_summary(report_root(settings))
        if summary is None:
            return {"ok": False, "error": "topic audit report not found"}
        return {"ok": True, "summary": summary}

    @app.post("/api/pipeline/retry/{post_id}", dependencies=[auth])
    async def api_pipeline_retry(request: Request, post_id: int):
        async with session_factory(request)() as session:
            await session.execute(update(ContentPipelineState).where(ContentPipelineState.post_id == post_id).values(last_error=None, retry_count=0))
            await session.commit()
        return {"ok": True}


app = create_app()


@web_cli.command("run")
def run(host: str = typer.Option("0.0.0.0", "--host"), port: int = typer.Option(8080, "--port")) -> None:
    """Run the FastAPI web app with uvicorn."""

    import uvicorn

    uvicorn.run("app.web.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    web_cli()
