from __future__ import annotations

from datetime import datetime, timezone

import typer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.state import mark_state
from app.content.url_utils import detect_url_type, domain_from_url, extract_urls, normalize_url
from app.main import safe_echo
from app.models import PostLink, TelegramPost

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """URL extraction commands."""


def link_values(post: TelegramPost) -> list[dict]:
    extracted = extract_urls(post.text, post.raw)
    values: list[dict] = []
    for index, item in enumerate(extracted):
        canonical = normalize_url(item.url)
        values.append(
            {
                "post_id": post.id,
                "original_url": item.url,
                "canonical_url": canonical,
                "domain": domain_from_url(canonical),
                "url_type": detect_url_type(canonical),
                "position_index": index,
                "is_primary": False,
                "extraction_status": "pending",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
    preferred = next((row for row in values if row["url_type"] not in {"telegram", "unknown"}), values[0] if values else None)
    if preferred:
        preferred["is_primary"] = True
    return values


async def extract_new(limit: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    created = 0
    async with factory() as session:
        result = await session.execute(
            select(TelegramPost)
            .outerjoin(PostLink, TelegramPost.id == PostLink.post_id)
            .where(PostLink.id.is_(None), TelegramPost.is_deleted.is_(False))
            .order_by(TelegramPost.date.nulls_last(), TelegramPost.id)
            .limit(limit)
        )
        posts = list(result.scalars())
        table = PostLink.__table__
        for post in posts:
            rows = link_values(post)
            for row in rows:
                stmt = insert(table).values(**row)
                await session.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_post_links_post_original_url",
                        set_={
                            "canonical_url": stmt.excluded.canonical_url,
                            "domain": stmt.excluded.domain,
                            "url_type": stmt.excluded.url_type,
                            "position_index": stmt.excluded.position_index,
                            "is_primary": stmt.excluded.is_primary,
                            "updated_at": stmt.excluded.updated_at,
                        },
                    )
                )
                created += 1
            await mark_state(session, post.id, link_status="done" if rows else "empty")
        await session.commit()
    return created


async def extract_post_links(post_id: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        post = (await session.execute(select(TelegramPost).where(TelegramPost.id == post_id))).scalar_one_or_none()
        if not post or post.is_deleted:
            return 0
        rows = link_values(post)
        table = PostLink.__table__
        for row in rows:
            stmt = insert(table).values(**row)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_post_links_post_original_url",
                    set_={
                        "canonical_url": stmt.excluded.canonical_url,
                        "domain": stmt.excluded.domain,
                        "url_type": stmt.excluded.url_type,
                        "position_index": stmt.excluded.position_index,
                        "is_primary": stmt.excluded.is_primary,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
            )
        await mark_state(session, post.id, link_status="done" if rows else "empty")
        await session.commit()
        return len(rows)


@app.command("extract-new")
def extract_new_command(limit: int = limit_option()) -> None:
    """Extract URLs from Telegram post text and raw Telegram entities."""

    count = run_async(extract_new(limit))
    safe_echo(f"links={count}")


if __name__ == "__main__":
    app()
