from __future__ import annotations

from datetime import datetime, timezone

import typer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.state import mark_state
from app.content.text_utils import clean_text, detect_language, emoji_count, has_code_markers, hashtags, mentions, normalize_text, sha256_text, word_count
from app.content.url_utils import domain_from_url, extract_urls, normalize_url
from app.main import safe_echo
from app.models import PostProcessed, TelegramPost

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Post processing commands."""


def processed_values(post: TelegramPost) -> dict:
    text = clean_text(post.text)
    urls = extract_urls(text, post.raw)
    normalized_urls = [normalize_url(item.url) for item in urls]
    domains = sorted({domain for domain in (domain_from_url(url) for url in normalized_urls) if domain})
    return {
        "post_id": post.id,
        "clean_text": text or None,
        "normalized_text": normalize_text(text) or None,
        "text_hash": sha256_text(text),
        "language": detect_language(text),
        "word_count": word_count(text),
        "char_count": len(text),
        "emoji_count": emoji_count(text),
        "url_count": len(urls),
        "domains": domains,
        "hashtags": hashtags(text),
        "mentions": mentions(text),
        "has_code": has_code_markers(text),
        "has_github": any(domain in {"github.com", "gist.github.com"} for domain in domains),
        "has_arxiv": any(domain in {"arxiv.org", "www.arxiv.org"} for domain in domains),
        "has_media": bool(post.media_path or post.media_type),
        "processed_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


async def process_new(limit: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    processed = 0
    async with factory() as session:
        result = await session.execute(
            select(TelegramPost)
            .outerjoin(PostProcessed, TelegramPost.id == PostProcessed.post_id)
            .where(PostProcessed.post_id.is_(None), TelegramPost.is_deleted.is_(False))
            .order_by(TelegramPost.date.nulls_last(), TelegramPost.id)
            .limit(limit)
        )
        posts = list(result.scalars())
        table = PostProcessed.__table__
        for post in posts:
            values = processed_values(post)
            stmt = insert(table).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[table.c.post_id],
                    set_={key: stmt.excluded[key] for key in values if key != "post_id"},
                )
            )
            await mark_state(session, post.id, processing_status="done")
            processed += 1
        await session.commit()
    return processed


async def process_post(post_id: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        post = (
            await session.execute(select(TelegramPost).where(TelegramPost.id == post_id, TelegramPost.is_deleted.is_(False)))
        ).scalar_one_or_none()
        if not post:
            return 0
        values = processed_values(post)
        table = PostProcessed.__table__
        stmt = insert(table).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[table.c.post_id],
                set_={key: stmt.excluded[key] for key in values if key != "post_id"},
            )
        )
        await mark_state(session, post.id, processing_status="done")
        await session.commit()
        return 1


@app.command("process-new")
def process_new_command(limit: int = limit_option()) -> None:
    """Clean and normalize Telegram posts that do not have post_processed rows."""

    count = run_async(process_new(limit))
    safe_echo(f"processed={count}")


if __name__ == "__main__":
    app()
