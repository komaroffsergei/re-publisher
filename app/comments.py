from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from app.config import Settings
from app.serializers import message_to_comment_dict, peer_id

logger = logging.getLogger(__name__)


async def iter_comment_messages(client: Any, channel_entity: Any, message: Any) -> AsyncIterator[tuple[Any, Any]]:
    try:
        async for reply in client.iter_messages(channel_entity, reply_to=message.id, reverse=True):
            yield channel_entity, reply
        return
    except Exception as exc:
        logger.warning(
            "high_level_comments_failed",
            extra={"extra": {"message_id": getattr(message, "id", None), "error": str(exc)}},
        )

    result = await client(GetDiscussionMessageRequest(peer=channel_entity, msg_id=message.id))
    discussion_entity = None
    chats = list(getattr(result, "chats", None) or [])
    for chat in reversed(chats):
        if peer_id(chat) != peer_id(channel_entity):
            discussion_entity = chat
            break
    if discussion_entity is None and chats:
        discussion_entity = chats[-1]
    if discussion_entity is None:
        return

    root_message = None
    for candidate in list(getattr(result, "messages", None) or []):
        if getattr(candidate, "id", None) is not None:
            root_message = candidate
            break
    if root_message is None:
        return

    async for reply in client.iter_messages(discussion_entity, reply_to=root_message.id, reverse=True):
        yield discussion_entity, reply


async def collect_comments_for_post(
    client: Any,
    settings: Settings,
    session: AsyncSession,
    post_data: dict[str, Any],
    channel_entity: Any,
    message: Any,
    download_media,
    upsert_comment,
) -> int:
    if not settings.collect_comments:
        return 0
    replies = getattr(getattr(message, "replies", None), "replies", 0) or 0
    if replies <= 0:
        return 0

    saved = 0
    try:
        async for discussion_entity, comment_message in iter_comment_messages(client, channel_entity, message):
            media_path = await download_media(settings, comment_message, peer_id(discussion_entity), comment_message.id)
            comment_data = message_to_comment_dict(client, post_data, discussion_entity, comment_message, media_path)
            await upsert_comment(session, comment_data)
            saved += 1
    except Exception as exc:
        logger.warning(
            "comments_collection_failed",
            extra={
                "extra": {
                    "chat_peer_id": post_data.get("chat_peer_id"),
                    "message_id": post_data.get("message_id"),
                    "error": str(exc),
                }
            },
        )
    return saved
