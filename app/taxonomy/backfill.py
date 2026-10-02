"""Idempotently sort existing textless MAX posts without running either model."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.models import PipelineEntry, TelegramChat, TelegramPost
from app.taxonomy.jobs import sort_textless_post


async def run() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine=engine)
    last_id = 0
    media = empty = 0
    try:
        while True:
            async with factory() as session:
                rows = (await session.execute(
                    select(TelegramPost.id, TelegramPost.media_type, TelegramPost.media_path)
                    .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                    .join(PipelineEntry, PipelineEntry.source_post_id == TelegramPost.id)
                    .where(
                        TelegramPost.id > last_id,
                        TelegramPost.is_deleted.is_(False),
                        TelegramChat.folder_name == settings.folder_name,
                        func.length(func.btrim(func.coalesce(TelegramPost.text, ""))) == 0,
                    )
                    .order_by(TelegramPost.id)
                    .limit(100)
                )).all()
                if not rows:
                    break
                for post_id, media_type, media_path in rows:
                    await sort_textless_post(session, post_id)
                    if media_type or media_path:
                        media += 1
                    else:
                        empty += 1
                last_id = rows[-1][0]
                await session.commit()
        print(f"sorted textless posts: media_only={media} empty={empty}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
