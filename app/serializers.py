from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from telethon import utils


def make_json_serializable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): make_json_serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_serializable(item) for item in value]
    if is_dataclass(value):
        return make_json_serializable(asdict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return make_json_serializable(to_dict())
        except Exception:
            return str(value)
    return str(value)


def peer_id(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(utils.get_peer_id(value))
    except Exception:
        pass
    raw_id = getattr(value, "id", None)
    if raw_id is not None:
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sender_peer_id(message: Any) -> int | None:
    from_id = getattr(message, "from_id", None)
    resolved = peer_id(from_id)
    if resolved is not None:
        return resolved
    sender_id = getattr(message, "sender_id", None)
    return peer_id(sender_id)


def media_type(message: Any) -> str | None:
    media = getattr(message, "media", None)
    if media is None:
        return None
    return media.__class__.__name__


def replies_count(message: Any) -> int | None:
    replies = getattr(message, "replies", None)
    count = getattr(replies, "replies", None)
    if count is None:
        return None
    try:
        return int(count)
    except (TypeError, ValueError):
        return None


def raw_message(message: Any) -> dict:
    to_dict = getattr(message, "to_dict", None)
    if callable(to_dict):
        raw = to_dict()
    else:
        raw = getattr(message, "__dict__", {})
    serialized = make_json_serializable(raw)
    return serialized if isinstance(serialized, dict) else {"value": serialized}


def message_to_post_dict(client: Any, chat_entity: Any, message: Any, media_path: str | None = None) -> dict[str, Any]:
    return {
        "chat_peer_id": peer_id(chat_entity),
        "message_id": int(getattr(message, "id")),
        "sender_peer_id": sender_peer_id(message),
        "date": getattr(message, "date", None),
        "edit_date": getattr(message, "edit_date", None),
        "text": getattr(message, "raw_text", None) or getattr(message, "message", None),
        "grouped_id": getattr(message, "grouped_id", None),
        "views": getattr(message, "views", None),
        "forwards": getattr(message, "forwards", None),
        "replies_count": replies_count(message),
        "media_type": media_type(message),
        "media_path": media_path,
        "raw": raw_message(message),
        "is_deleted": False,
    }


def message_to_comment_dict(
    client: Any,
    post: Any,
    discussion_entity: Any,
    comment_message: Any,
    media_path: str | None = None,
) -> dict[str, Any]:
    reply_to = getattr(comment_message, "reply_to", None)
    parent_comment_message_id = getattr(reply_to, "reply_to_msg_id", None)
    post_chat_peer_id = post.get("chat_peer_id") if isinstance(post, dict) else getattr(post, "chat_peer_id")
    post_message_id = post.get("message_id") if isinstance(post, dict) else getattr(post, "message_id")
    return {
        "post_chat_peer_id": post_chat_peer_id,
        "post_message_id": post_message_id,
        "discussion_peer_id": peer_id(discussion_entity),
        "comment_message_id": int(getattr(comment_message, "id")),
        "parent_comment_message_id": parent_comment_message_id,
        "sender_peer_id": sender_peer_id(comment_message),
        "date": getattr(comment_message, "date", None),
        "edit_date": getattr(comment_message, "edit_date", None),
        "text": getattr(comment_message, "raw_text", None) or getattr(comment_message, "message", None),
        "media_type": media_type(comment_message),
        "media_path": media_path,
        "raw": raw_message(comment_message),
        "is_deleted": False,
    }
