from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ContentPipelineState


async def mark_state(
    session: AsyncSession,
    post_id: int,
    **statuses: str | None,
) -> None:
    values = {"post_id": post_id, "updated_at": func.now()}
    values.update({key: value for key, value in statuses.items() if value is not None})
    table = ContentPipelineState.__table__
    stmt = insert(table).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[table.c.post_id],
            set_={key: stmt.excluded[key] for key in values if key != "post_id"} | {"updated_at": func.now()},
        )
    )

