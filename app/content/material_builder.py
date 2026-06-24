from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import typer
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.text_utils import clean_text, detect_language, first_nonempty
from app.main import safe_echo
from app.models import ContentItem, ContentPipelineState, LinkSnapshot, MediaAsset, PostLink, PostProcessed, TelegramPost

app = typer.Typer(no_args_is_help=True)
CONTENT_INSERT_CHUNK_SIZE = 1500
STATE_INSERT_CHUNK_SIZE = 5000
MEDIA_PREFETCH_CHUNK_SIZE = 10000


@app.callback()
def main() -> None:
    """Material builder commands."""


def first_line(text: str | None) -> str | None:
    cleaned = clean_text(text)
    if not cleaned:
        return None
    return cleaned.splitlines()[0][:180]


def content_hash(*values: str | None) -> str | None:
    joined = "\n".join(clean_text(value) for value in values if clean_text(value))
    if not joined:
        return None
    return hashlib.sha256(joined.lower().encode("utf-8")).hexdigest()


def chunks(values: list, size: int) -> list[list]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def quality_score(processed: PostProcessed, snapshot: LinkSnapshot | None) -> float:
    score = 0.15 if processed.clean_text else 0.0
    score += min((processed.word_count or 0) / 120, 0.35)
    if snapshot and snapshot.title:
        score += 0.15
    if snapshot and snapshot.summary_short:
        score += 0.2
    if snapshot and snapshot.extraction_quality_score:
        score += min(float(snapshot.extraction_quality_score), 1.0) * 0.15
    return round(min(score, 1.0), 4)


async def build_new(limit: int, refresh: bool = False) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        stmt = (
            select(PostProcessed, TelegramPost, PostLink, LinkSnapshot)
            .join(TelegramPost, TelegramPost.id == PostProcessed.post_id)
            .outerjoin(ContentItem, ContentItem.source_post_id == PostProcessed.post_id)
            .outerjoin(PostLink, and_(PostLink.post_id == PostProcessed.post_id, PostLink.is_primary.is_(True)))
            .outerjoin(LinkSnapshot, LinkSnapshot.link_id == PostLink.id)
            .where(TelegramPost.is_deleted.is_(False))
            .order_by(PostProcessed.post_id)
            .limit(limit)
        )
        if not refresh:
            stmt = stmt.where(ContentItem.id.is_(None))
        result = await session.execute(stmt)
        rows = list(result.all())
        if not rows:
            return 0

        post_ids = [post.id for _, post, _, _ in rows]
        media_by_post = {}
        for post_id_chunk in chunks(post_ids, MEDIA_PREFETCH_CHUNK_SIZE):
            media_result = await session.execute(
                select(MediaAsset.source_post_id, func.min(MediaAsset.id))
                .where(MediaAsset.source_post_id.in_(post_id_chunk))
                .group_by(MediaAsset.source_post_id)
            )
            media_by_post.update({post_id: asset_id for post_id, asset_id in media_result.all()})

        table = ContentItem.__table__
        content_rows = []
        state_rows = []
        now = datetime.now(timezone.utc)
        for processed, post, link, snapshot in rows:
            image_asset_id = media_by_post.get(post.id) or (snapshot.image_asset_id if snapshot else None)
            title = first_nonempty([snapshot.title if snapshot else None, first_line(processed.clean_text)])
            summary = snapshot.summary_short if snapshot else None
            content_rows.append(
                {
                    "source_post_id": post.id,
                    "primary_link_id": link.id if link else None,
                    "primary_snapshot_id": snapshot.id if snapshot else None,
                    "title": title,
                    "main_text": processed.clean_text,
                    "source_summary": summary,
                    "source_url": (link.final_url or link.canonical_url) if link else None,
                    "source_domain": link.domain if link else None,
                    "source_lang": processed.language or detect_language(processed.clean_text),
                    "target_lang": settings.translation_default_target_lang,
                    "primary_image_asset_id": image_asset_id,
                    "content_hash": content_hash(processed.normalized_text, title, summary),
                    "quality_score": quality_score(processed, snapshot),
                    "status": "ready",
                    "created_at": now,
                    "updated_at": now,
                }
            )
            state_rows.append({"post_id": post.id, "material_status": "done", "updated_at": now})
            count += 1

        for content_chunk in chunks(content_rows, CONTENT_INSERT_CHUNK_SIZE):
            content_stmt = insert(table).values(content_chunk)
            await session.execute(
                content_stmt.on_conflict_do_update(
                    constraint="uq_content_items_source_post_id",
                    set_={key: content_stmt.excluded[key] for key in content_chunk[0] if key not in {"source_post_id", "created_at"}},
                )
            )

        state_table = ContentPipelineState.__table__
        for state_chunk in chunks(state_rows, STATE_INSERT_CHUNK_SIZE):
            state_stmt = insert(state_table).values(state_chunk)
            await session.execute(
                state_stmt.on_conflict_do_update(
                    index_elements=[state_table.c.post_id],
                    set_={
                        "material_status": state_stmt.excluded.material_status,
                        "updated_at": state_stmt.excluded.updated_at,
                    },
                )
            )
        await session.commit()
    return count


async def build_post_material(post_id: int, refresh: bool = True) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        row = (
            await session.execute(
                select(PostProcessed, TelegramPost, PostLink, LinkSnapshot)
                .join(TelegramPost, TelegramPost.id == PostProcessed.post_id)
                .outerjoin(ContentItem, ContentItem.source_post_id == PostProcessed.post_id)
                .outerjoin(PostLink, and_(PostLink.post_id == PostProcessed.post_id, PostLink.is_primary.is_(True)))
                .outerjoin(LinkSnapshot, LinkSnapshot.link_id == PostLink.id)
                .where(PostProcessed.post_id == post_id, TelegramPost.is_deleted.is_(False))
            )
        ).first()
        if not row:
            return 0
        processed, post, link, snapshot = row
        existing = (
            await session.execute(select(ContentItem).where(ContentItem.source_post_id == post_id))
        ).scalar_one_or_none()
        if existing and not refresh:
            return 0
        media_asset_id = (
            await session.execute(
                select(func.min(MediaAsset.id)).where(MediaAsset.source_post_id == post_id)
            )
        ).scalar_one_or_none()
        image_asset_id = media_asset_id or (snapshot.image_asset_id if snapshot else None)
        title = first_nonempty([snapshot.title if snapshot else None, first_line(processed.clean_text)])
        summary = snapshot.summary_short if snapshot else None
        now = datetime.now(timezone.utc)
        values = {
            "source_post_id": post.id,
            "primary_link_id": link.id if link else None,
            "primary_snapshot_id": snapshot.id if snapshot else None,
            "title": title,
            "main_text": processed.clean_text,
            "source_summary": summary,
            "source_url": (link.final_url or link.canonical_url) if link else None,
            "source_domain": link.domain if link else None,
            "source_lang": processed.language or detect_language(processed.clean_text),
            "target_lang": settings.translation_default_target_lang,
            "primary_image_asset_id": image_asset_id,
            "content_hash": content_hash(processed.normalized_text, title, summary),
            "quality_score": quality_score(processed, snapshot),
            "status": "ready",
            "created_at": now,
            "updated_at": now,
        }
        table = ContentItem.__table__
        stmt = insert(table).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_content_items_source_post_id",
                set_={key: stmt.excluded[key] for key in values if key not in {"source_post_id", "created_at"}},
            )
        )
        state_table = ContentPipelineState.__table__
        state_stmt = insert(state_table).values(post_id=post.id, material_status="done", updated_at=now)
        await session.execute(
            state_stmt.on_conflict_do_update(
                index_elements=[state_table.c.post_id],
                set_={"material_status": state_stmt.excluded.material_status, "updated_at": state_stmt.excluded.updated_at},
            )
        )
        await session.commit()
        return 1


@app.command("build-new")
def build_new_command(
    limit: int = limit_option(),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh existing content_items as well as creating new ones."),
) -> None:
    """Build content_items from processed posts and enriched links."""

    count = run_async(build_new(limit, refresh=refresh))
    safe_echo(f"content_items={count}")


if __name__ == "__main__":
    app()
