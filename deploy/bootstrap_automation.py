"""Однократное включение явно согласованных карточек, до старта collector."""

import asyncio
from sqlalchemy import select, func
from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.models import PipelineEntry

SELECTED = (
    9339,
    9367,
    9389,
    9397,
    9413,
    9414,
    9418,
    9419,
    9420,
    9421,
    9433,
    9438,
    9457,
)
MANUAL = 9368


async def main():
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine=engine)() as session:
            async with session.begin():
                received = await session.scalar(
                    select(func.count())
                    .select_from(PipelineEntry)
                    .where(PipelineEntry.stage == "received")
                )
                assert received == 6021, (
                    "Изменился контрольный архив: сначала проверьте состав"
                )
                entries = list(
                    (
                        await session.execute(
                            select(PipelineEntry)
                            .where(PipelineEntry.id.in_((*SELECTED, MANUAL)))
                            .with_for_update()
                        )
                    ).scalars()
                )
                assert len(entries) == 14
                for entry in entries:
                    if entry.auto_enabled:
                        continue
                    assert entry.stage == (
                        "marking" if entry.id == MANUAL else "filtered"
                    )
                    entry.auto_enabled = True
                    entry.auto_state = "pending"
                    entry.auto_manual_mark = entry.id == MANUAL
                    entry.auto_retry_at = None
        print("BOOTSTRAP_OK selected=13 manual=1 old_received=6021")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
