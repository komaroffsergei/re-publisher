from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.config import Settings
from app.db import create_engine
from app.models import ContentPipelineState, PipelineEntry, RewriteAttempt

PIPELINE_ADVISORY_LOCK_KEY = 2_026_062_301
ACTIVE_STATUS_VALUES = {"running", "processing"}
PIPELINE_STATUS_FIELDS = (
    "processing_status",
    "link_status",
    "enrichment_status",
    "summary_status",
    "material_status",
    "classification_status",
    "rewrite_status",
    "publication_status",
)


@dataclass
class PipelineWorkLock:
    engine: AsyncEngine
    connection: AsyncConnection
    owner: str
    entry_id: int | None = None

    async def release(self) -> None:
        try:
            await self.connection.execute(
                text("select pg_advisory_unlock(:lock_key)"),
                {"lock_key": PIPELINE_ADVISORY_LOCK_KEY},
            )
        finally:
            await self.connection.close()
            await self.engine.dispose()


async def try_acquire_pipeline_work_lock(
    settings: Settings,
    *,
    owner: str,
    entry_id: int | None = None,
) -> PipelineWorkLock | None:
    engine = create_engine(settings)
    connection = await engine.connect()
    try:
        acquired = bool(
            await connection.scalar(
                text("select pg_try_advisory_lock(:lock_key)"),
                {"lock_key": PIPELINE_ADVISORY_LOCK_KEY},
            )
        )
    except Exception:
        await connection.close()
        await engine.dispose()
        raise
    if not acquired:
        await connection.close()
        await engine.dispose()
        return None
    return PipelineWorkLock(engine=engine, connection=connection, owner=owner, entry_id=entry_id)


def active_state_condition():
    return or_(
        *[
            getattr(ContentPipelineState, field_name).in_(ACTIVE_STATUS_VALUES)
            for field_name in PIPELINE_STATUS_FIELDS
        ]
    )


def active_state_phase(state: ContentPipelineState) -> tuple[str, str] | None:
    phases = {
        "processing_status": ("process_post", "подготовка текста"),
        "link_status": ("extract_links", "извлечение ссылок"),
        "enrichment_status": ("enrich_links", "обогащение ссылок"),
        "summary_status": ("summarize_links", "summary ссылок"),
        "material_status": ("build_material", "сборка материала"),
        "classification_status": ("classify_post", "классификация"),
        "rewrite_status": ("rewrite", "рерайт"),
        "publication_status": ("publish", "публикация"),
    }
    for field_name in PIPELINE_STATUS_FIELDS:
        if getattr(state, field_name, None) in ACTIVE_STATUS_VALUES:
            return phases[field_name]
    return None


async def active_pipeline_work_snapshot(session: AsyncSession) -> dict[str, Any] | None:
    state_row = (
        await session.execute(
            select(PipelineEntry, ContentPipelineState)
            .join(ContentPipelineState, ContentPipelineState.post_id == PipelineEntry.source_post_id)
            .where(active_state_condition())
            .order_by(ContentPipelineState.updated_at.desc(), PipelineEntry.id.desc())
            .limit(1)
        )
    ).first()
    if state_row:
        entry, state = state_row
        phase = active_state_phase(state)
        return {
            "entry_id": entry.id,
            "source_post_id": entry.source_post_id,
            "phase": phase[0] if phase else "pipeline",
            "label": phase[1] if phase else "pipeline",
            "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        }
    attempt_row = (
        await session.execute(
            select(PipelineEntry, RewriteAttempt)
            .join(RewriteAttempt, RewriteAttempt.pipeline_entry_id == PipelineEntry.id)
            .where(RewriteAttempt.status == "running")
            .order_by(RewriteAttempt.started_at.desc().nulls_last(), RewriteAttempt.id.desc())
            .limit(1)
        )
    ).first()
    if attempt_row:
        entry, attempt = attempt_row
        return {
            "entry_id": entry.id,
            "source_post_id": entry.source_post_id,
            "phase": "rewrite",
            "label": "рерайт",
            "updated_at": attempt.started_at.isoformat() if attempt.started_at else None,
        }
    return None


async def reset_stale_pipeline_activity(
    session: AsyncSession,
    *,
    stale_after_minutes: int = 30,
) -> dict[str, int]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    active_states = list(
        (
            await session.execute(
                select(ContentPipelineState)
                .where(active_state_condition())
                .order_by(ContentPipelineState.updated_at.desc(), ContentPipelineState.post_id.desc())
            )
        ).scalars()
    )
    keep_post_id = (
        active_states[0].post_id
        if active_states and active_states[0].updated_at and active_states[0].updated_at >= cutoff
        else None
    )
    reset_post_ids: list[int] = []
    for state in active_states:
        stale = not state.updated_at or state.updated_at < cutoff
        duplicated = keep_post_id is not None and state.post_id != keep_post_id
        if stale or duplicated:
            values = {
                field_name: "pending"
                for field_name in PIPELINE_STATUS_FIELDS
                if getattr(state, field_name, None) in ACTIVE_STATUS_VALUES
            }
            if values:
                values["updated_at"] = datetime.now(timezone.utc)
                await session.execute(
                    update(ContentPipelineState)
                    .where(ContentPipelineState.post_id == state.post_id)
                    .values(**values)
                )
                reset_post_ids.append(state.post_id)
    if reset_post_ids:
        await session.execute(
            update(PipelineEntry)
            .where(PipelineEntry.source_post_id.in_(reset_post_ids), PipelineEntry.status == "rewrite_running")
            .values(status="rewrite_pending", updated_at=datetime.now(timezone.utc))
        )
        await session.execute(
            update(RewriteAttempt)
            .where(
                RewriteAttempt.source_post_id.in_(reset_post_ids),
                RewriteAttempt.status == "running",
            )
            .values(
                status="cancelled",
                error="stale activity reset",
                finished_at=datetime.now(timezone.utc),
            )
        )
    return {
        "active_found": len(active_states),
        "reset": len(set(reset_post_ids)),
        "kept": 1 if keep_post_id is not None else 0,
    }
