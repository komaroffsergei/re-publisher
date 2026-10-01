"""Состояние сервисов в БД: проверяем живой цикл, а не наличие session-файла."""

from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import insert
from app.models import ServiceRuntime

UNSET = object()


async def heartbeat(factory, name, error=UNSET, success=False):
    now = datetime.now(timezone.utc)
    values = dict(
        name=name,
        started_at=now,
        heartbeat_at=now,
        error=None if error is UNSET else error,
    )
    if success:
        values["last_success_at"] = now
    statement = insert(ServiceRuntime).values(**values)
    changes = {"heartbeat_at": now}
    if error is not UNSET or success:
        changes["error"] = None if success else error
    if success:
        changes["last_success_at"] = now
    async with factory() as session:
        await session.execute(
            statement.on_conflict_do_update(index_elements=["name"], set_=changes)
        )
        await session.commit()


async def check_health(name):
    from app.config import get_settings
    from app.db import create_engine, create_session_factory

    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine=engine)() as session:
            row = await session.get(ServiceRuntime, name)
            assert row and row.heartbeat_at and not row.error
            assert (datetime.now(timezone.utc) - row.heartbeat_at).total_seconds() < 60
            if name == "collector":
                assert row.last_success_at
                assert (
                    datetime.now(timezone.utc) - row.last_success_at
                ).total_seconds() < 900
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import asyncio, sys

    asyncio.run(check_health(sys.argv[1]))
