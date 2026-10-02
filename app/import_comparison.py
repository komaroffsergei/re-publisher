"""Import an exact, disjoint MAX comparison cohort without starting the collector.

The private JSONL input contains selected Telegram peer/message IDs and text hashes.
This command fetches those messages again through the authorised user session,
checks that each message is still in MAX and its text has not changed, then saves
only the requested messages. It never advances the realtime sync cursor.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import create_engine, create_session_factory, session_scope
from app.folders import resolve_folder_chats
from app.models import TelegramPost
from app.sync import save_message, upsert_chat
from app.telegram_client import create_telegram_client


def load_cohort(path: Path, expected_count: int) -> dict[int, dict[int, dict]]:
    rows = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    if len(rows) != expected_count:
        raise ValueError(f"expected {expected_count} rows, got {len(rows)}")
    by_chat: dict[int, dict[int, dict]] = defaultdict(dict)
    for row in rows:
        peer = int(row["chat_peer_id"])
        message = int(row["message_id"])
        if message in by_chat[peer]:
            raise ValueError(f"duplicate Telegram message {peer}/{message}")
        if row.get("partition_hint") != "blind_test_candidate":
            raise ValueError("comparison import accepts only blind test candidates")
        by_chat[peer][message] = row
    return by_chat


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", type=Path)
    parser.add_argument("--expect", type=int, default=100)
    parser.add_argument("--apply", action="store_true", help="save verified posts; default is read-only preflight")
    parser.add_argument("--download-media", action="store_true")
    args = parser.parse_args()
    if args.expect < 1:
        raise ValueError("expected count must be positive")
    selected = load_cohort(args.cohort, args.expect)
    settings = get_settings().model_copy(update={"download_media": args.download_media, "collect_comments": False})
    client = create_telegram_client(settings)
    engine = create_engine(settings)
    factory = create_session_factory(engine=engine)
    checked: list[tuple[object, object]] = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorised")
        chats = {chat.peer_id: chat for chat in await resolve_folder_chats(client, settings.folder_name)}
        absent_chats = set(selected) - set(chats)
        if absent_chats:
            raise RuntimeError(f"selected chats no longer in MAX: {len(absent_chats)}")
        for peer, message_rows in selected.items():
            chat = chats[peer]
            messages = await client.get_messages(chat.entity, ids=list(message_rows))
            if not isinstance(messages, list):
                messages = [messages]
            by_id = {int(message.id): message for message in messages if message is not None}
            for message_id in message_rows:
                message = by_id.get(message_id)
                if message is None:
                    raise RuntimeError(f"selected message no longer available: {peer}/{message_id}")
                expected_hash = message_rows[message_id]["text_sha256"]
                actual_hash = hashlib.sha256((message.raw_text or "").encode("utf-8")).hexdigest()
                if expected_hash != actual_hash:
                    raise RuntimeError(f"selected message changed after cohort freeze: {peer}/{message_id}")
                checked.append((chat, message))
        async with factory() as session:
            existing: set[tuple[int, int]] = set()
            for chat, message in checked:
                row = (await session.execute(select(TelegramPost.id, TelegramPost.text).where(
                    TelegramPost.chat_peer_id == chat.peer_id,
                    TelegramPost.message_id == message.id,
                ))).first()
                if row is not None:
                    expected_hash = selected[chat.peer_id][message.id]["text_sha256"]
                    if hashlib.sha256((row[1] or "").encode("utf-8")).hexdigest() != expected_hash:
                        raise RuntimeError(f"existing message text differs: {chat.peer_id}/{message.id}")
                    existing.add((chat.peer_id, message.id))
        if existing and not args.apply:
            raise RuntimeError(f"comparison cohort contains {len(existing)} already-imported messages")
        if not args.apply:
            print(json.dumps({"verified": len(checked), "chats": len(selected), "new": len(checked), "applied": False}))
            return
        inserted = 0
        for chat, message in checked:
            if (chat.peer_id, message.id) in existing:
                continue
            async with session_scope(factory) as session:
                await upsert_chat(session, settings.folder_name, chat)
                _, fresh, _ = await save_message(
                    client, settings, session, chat, message,
                    collect_comments=False, update_state=False,
                )
                if not fresh:
                    raise RuntimeError(f"message became imported during this run: {chat.peer_id}/{message.id}")
                inserted += 1
        print(json.dumps({"verified": len(checked), "chats": len(selected), "inserted": inserted,
                          "already_imported": len(existing), "total_available": inserted + len(existing),
                          "media_downloaded": args.download_media, "applied": True}))
    finally:
        await client.disconnect()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
