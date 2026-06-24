from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.state import mark_state
from app.main import safe_echo
from app.models import MediaAsset, PublicationDraft, PublicationTarget, PublishedPost, Showcase

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Publishing commands."""


async def send_telegram(settings, showcase: Showcase, draft: PublicationDraft, media: MediaAsset | None) -> tuple[str | None, str | None]:
    import httpx

    chat_id = showcase.target_chat_id or showcase.target_username
    if not chat_id:
        raise RuntimeError("showcase target chat is not configured")
    base = f"https://api.telegram.org/bot{settings.bot_token}"
    async with httpx.AsyncClient(timeout=30) as client:
        if media and media.local_path and Path(media.local_path).exists() and len(draft.body) <= 1024:
            with Path(media.local_path).open("rb") as handle:
                response = await client.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": chat_id, "caption": draft.body},
                    files={"photo": handle},
                )
        else:
            response = await client.post(f"{base}/sendMessage", data={"chat_id": chat_id, "text": draft.body})
        response.raise_for_status()
        payload = response.json()
    message = payload.get("result", {})
    message_id = str(message.get("message_id")) if message.get("message_id") is not None else None
    username = (showcase.target_username or "").lstrip("@")
    published_url = f"https://t.me/{username}/{message_id}" if username and message_id else None
    return message_id, published_url


async def publish_approved(limit: int) -> int:
    settings = settings_or_exit()
    if not settings.auto_publish or not settings.bot_token:
        return 0
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        result = await session.execute(
            select(PublicationDraft, PublicationTarget, Showcase, MediaAsset)
            .join(PublicationTarget, PublicationTarget.id == PublicationDraft.publication_target_id)
            .join(Showcase, Showcase.id == PublicationTarget.showcase_id)
            .outerjoin(MediaAsset, MediaAsset.id == PublicationDraft.image_asset_id)
            .outerjoin(PublishedPost, (PublishedPost.draft_id == PublicationDraft.id) & (PublishedPost.showcase_id == Showcase.id))
            .where(PublicationDraft.status == "approved", PublishedPost.id.is_(None))
            .order_by(PublicationDraft.id)
            .limit(limit)
        )
        rows = list(result.all())
        table = PublishedPost.__table__
        for draft, target, showcase, media in rows:
            status = "published"
            error = None
            message_id = published_url = None
            try:
                message_id, published_url = await send_telegram(settings, showcase, draft, media)
            except Exception as exc:
                status = "failed"
                error = str(exc)
            values = {
                "draft_id": draft.id,
                "showcase_id": showcase.id,
                "target_type": showcase.target_type,
                "target_chat_id": showcase.target_chat_id or showcase.target_username,
                "target_message_id": message_id,
                "published_url": published_url,
                "status": status,
                "error": error,
                "published_at": datetime.now(timezone.utc) if status == "published" else None,
                "created_at": datetime.now(timezone.utc),
            }
            stmt = insert(table).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_published_posts_draft_showcase",
                    set_={key: stmt.excluded[key] for key in values if key != "created_at"},
                )
            )
            await mark_state(session, draft.source_post_id, publication_status=status)
            count += 1
        await session.commit()
    return count


@app.command("publish-approved")
def publish_approved_command(limit: int = limit_option(50)) -> None:
    """Publish approved drafts only when AUTO_PUBLISH=true and BOT_TOKEN is configured."""

    count = run_async(publish_approved(limit))
    safe_echo(f"published_attempts={count}")


if __name__ == "__main__":
    app()
