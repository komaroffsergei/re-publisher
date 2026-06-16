from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from telethon import utils
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import Channel, Chat, User

from app.serializers import make_json_serializable, peer_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FolderChat:
    peer_id: int
    title: str | None
    username: str | None
    chat_type: str
    entity: Any
    raw: dict | None


def extract_filter_title(dialog_filter: Any) -> str:
    title = getattr(dialog_filter, "title", "")
    if isinstance(title, str):
        return title
    text = getattr(title, "text", None)
    if isinstance(text, str):
        return text
    to_dict = getattr(title, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            text = data.get("text")
            if isinstance(text, str):
                return text
        except Exception:
            pass
    return str(title or "")


def filter_peer_candidates(dialog_filter: Any) -> list[Any]:
    include_peers = list(getattr(dialog_filter, "include_peers", None) or [])
    pinned_peers = list(getattr(dialog_filter, "pinned_peers", None) or [])
    exclude_ids = {peer_id(peer) for peer in list(getattr(dialog_filter, "exclude_peers", None) or [])}
    exclude_ids.discard(None)

    seen: set[int] = set()
    result: list[Any] = []
    for peer in include_peers + pinned_peers:
        resolved_id = peer_id(peer)
        if resolved_id is None or resolved_id in exclude_ids or resolved_id in seen:
            continue
        seen.add(resolved_id)
        result.append(peer)
    return result


async def fetch_dialog_filters(client: Any) -> list[Any]:
    result = await client(GetDialogFiltersRequest())
    if isinstance(result, list):
        return result
    return list(getattr(result, "filters", []) or [])


async def find_dialog_filter(client: Any, folder_name: str) -> Any:
    filters = await fetch_dialog_filters(client)
    for dialog_filter in filters:
        if extract_filter_title(dialog_filter) == folder_name:
            return dialog_filter
    available = [extract_filter_title(item) for item in filters if extract_filter_title(item)]
    raise ValueError(f"Telegram folder '{folder_name}' was not found. Available folders: {available}")


def entity_title(entity: Any) -> str | None:
    for attr in ("title", "first_name", "username"):
        value = getattr(entity, attr, None)
        if value:
            last_name = getattr(entity, "last_name", None)
            if attr == "first_name" and last_name:
                return f"{value} {last_name}"
            return str(value)
    return None


def entity_username(entity: Any) -> str | None:
    username = getattr(entity, "username", None)
    return str(username) if username else None


def entity_chat_type(entity: Any) -> str:
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
    if isinstance(entity, Channel):
        if getattr(entity, "broadcast", False):
            return "channel"
        return "group"
    if isinstance(entity, Chat):
        return "group"
    if getattr(entity, "bot", False):
        return "bot"
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
        return "group"
    return entity.__class__.__name__.lower()


def entity_raw(entity: Any) -> dict | None:
    raw = make_json_serializable(entity)
    return raw if isinstance(raw, dict) else {"value": raw}


def folder_chat_from_entity(entity: Any) -> FolderChat:
    resolved_peer_id = peer_id(entity)
    if resolved_peer_id is None:
        raise ValueError(f"Could not resolve peer id for {entity!r}")
    return FolderChat(
        peer_id=resolved_peer_id,
        title=entity_title(entity),
        username=entity_username(entity),
        chat_type=entity_chat_type(entity),
        entity=entity,
        raw=entity_raw(entity),
    )


def filter_has_any_flag(dialog_filter: Any) -> bool:
    return any(
        bool(getattr(dialog_filter, flag, False))
        for flag in ("contacts", "non_contacts", "groups", "broadcasts", "bots")
    )


def is_muted(dialog: Any) -> bool:
    notify_settings = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    mute_until = getattr(notify_settings, "mute_until", None)
    if mute_until is None:
        return False
    if isinstance(mute_until, datetime):
        return mute_until > datetime.now(timezone.utc)
    try:
        return int(mute_until) > int(datetime.now(timezone.utc).timestamp())
    except (TypeError, ValueError):
        return False


def dialog_matches_flags(dialog_filter: Any, dialog: Any) -> bool:
    entity = getattr(dialog, "entity", None)
    chat_type = entity_chat_type(entity)

    include = False
    if getattr(dialog_filter, "broadcasts", False) and chat_type == "channel":
        include = True
    if getattr(dialog_filter, "groups", False) and chat_type == "group":
        include = True
    if getattr(dialog_filter, "bots", False) and chat_type == "bot":
        include = True
    if getattr(dialog_filter, "contacts", False) and isinstance(entity, User) and getattr(entity, "contact", False):
        include = True
    if getattr(dialog_filter, "non_contacts", False) and isinstance(entity, User) and not getattr(entity, "contact", False):
        include = True
    if not include:
        return False

    if getattr(dialog_filter, "exclude_archived", False) and getattr(dialog, "folder_id", None) == 1:
        return False
    if getattr(dialog_filter, "exclude_muted", False) and is_muted(dialog):
        return False
    if getattr(dialog_filter, "exclude_read", False) and getattr(dialog, "unread_count", 0) == 0:
        return False
    return True


async def resolve_folder_chats(client: Any, folder_name: str) -> list[FolderChat]:
    dialog_filter = await find_dialog_filter(client, folder_name)
    exclude_ids = {peer_id(peer) for peer in list(getattr(dialog_filter, "exclude_peers", None) or [])}
    exclude_ids.discard(None)

    entities: dict[int, Any] = {}
    for peer in filter_peer_candidates(dialog_filter):
        try:
            entity = await client.get_entity(peer)
        except Exception:
            logger.warning("failed_to_resolve_explicit_peer", extra={"extra": {"peer": str(peer)}})
            continue
        resolved_id = peer_id(entity)
        if resolved_id is not None and resolved_id not in exclude_ids:
            entities[resolved_id] = entity

    if filter_has_any_flag(dialog_filter):
        async for dialog in client.iter_dialogs():
            entity = getattr(dialog, "entity", None)
            resolved_id = peer_id(entity)
            if resolved_id is None or resolved_id in exclude_ids:
                continue
            try:
                if dialog_matches_flags(dialog_filter, dialog):
                    entities[resolved_id] = entity
            except Exception as exc:
                logger.warning(
                    "dialog_flag_evaluation_failed",
                    extra={"extra": {"peer_id": resolved_id, "error": str(exc)}},
                )
    else:
        unsupported_flags = [
            flag
            for flag in ("exclude_archived", "exclude_muted", "exclude_read")
            if getattr(dialog_filter, flag, False)
        ]
        if unsupported_flags:
            logger.warning(
                "folder_filter_has_exclude_flags_without_include_flags",
                extra={"extra": {"folder": folder_name, "flags": unsupported_flags}},
            )

    return [folder_chat_from_entity(entity) for _, entity in sorted(entities.items())]
