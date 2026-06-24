from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import typer
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.state import mark_state
from app.content.text_utils import clean_text
from app.content.url_utils import domain_from_url, safe_join_redirect, validate_fetch_url, youtube_thumbnail_url
from app.main import safe_echo
from app.models import LinkSnapshot, PostLink, TelegramChat, TelegramPost

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Link enrichment commands."""


@dataclass
class FetchResult:
    final_url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    body: bytes | None = None
    error: str | None = None


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


async def fetch_url(url: str, settings: Settings) -> FetchResult:
    try:
        current_url = validate_fetch_url(url)
    except Exception as exc:
        return FetchResult(error=str(exc))

    try:
        import httpx
    except Exception as exc:
        return FetchResult(error=f"httpx unavailable: {exc}")

    headers = {"User-Agent": settings.link_fetch_user_agent}
    async with httpx.AsyncClient(timeout=settings.link_fetch_timeout_seconds, headers=headers, follow_redirects=False) as client:
        for redirect_count in range(settings.link_fetch_max_redirects + 1):
            try:
                response = await client.get(current_url)
            except Exception as exc:
                return FetchResult(final_url=current_url, error=str(exc))

            if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                if redirect_count >= settings.link_fetch_max_redirects:
                    return FetchResult(final_url=current_url, status_code=response.status_code, error="redirect limit exceeded")
                redirected = safe_join_redirect(current_url, response.headers["location"])
                try:
                    current_url = validate_fetch_url(redirected)
                except Exception as exc:
                    return FetchResult(final_url=redirected, status_code=response.status_code, error=str(exc))
                continue

            body = response.content[: settings.link_fetch_max_bytes + 1]
            if len(body) > settings.link_fetch_max_bytes:
                return FetchResult(final_url=str(response.url), status_code=response.status_code, content_type=response.headers.get("content-type"), error="response too large")
            return FetchResult(
                final_url=str(response.url),
                status_code=response.status_code,
                content_type=response.headers.get("content-type"),
                body=body,
            )
    return FetchResult(final_url=current_url, error="unhandled fetch state")


async def fetch_youtube_oembed(url: str, settings: Settings) -> dict[str, Any]:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=settings.link_fetch_timeout_seconds, headers={"User-Agent": settings.link_fetch_user_agent}) as client:
            response = await client.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"})
            if response.status_code == 200:
                return response.json()
    except Exception:
        return {}
    return {}


async def youtube_snapshot_values(link: PostLink, settings: Settings) -> dict[str, Any] | None:
    source_url = link.canonical_url or link.original_url
    thumbnail = youtube_thumbnail_url(source_url)
    if not thumbnail:
        return None
    metadata = await fetch_youtube_oembed(source_url, settings)
    now = datetime.now(timezone.utc)
    title = clean_text(metadata.get("title")) if metadata else None
    author = clean_text(metadata.get("author_name")) if metadata else None
    return {
        "link_id": link.id,
        "canonical_url": link.canonical_url,
        "final_url": source_url,
        "domain": domain_from_url(source_url),
        "http_status": 200 if metadata else None,
        "content_type": "text/html",
        "fetched_at": now,
        "title": title,
        "description": "YouTube video metadata extracted locally from URL/oEmbed." if title else None,
        "site_name": "YouTube",
        "author": author,
        "extracted_text": title,
        "extracted_text_hash": hashlib.sha256(title.encode("utf-8")).hexdigest() if title else None,
        "extraction_method": "youtube_oembed",
        "extraction_quality_score": 0.45 if title else 0.25,
        "summary_short": title,
        "summary_model": "youtube_oembed",
        "summary_generated_at": now if title else None,
        "image_url": thumbnail,
        "raw_metadata": metadata or {"thumbnail_url": thumbnail},
        "error": None if title else "youtube_oembed_unavailable",
        "created_at": now,
        "updated_at": now,
    }


def telegram_message_id(url: str | None) -> int | None:
    parsed = urlparse(url or "")
    if (parsed.hostname or "").lower() not in {"t.me", "telegram.me"}:
        return None
    parts = [part for part in (parsed.path or "").split("/") if part]
    if len(parts) < 2:
        return None
    try:
        return int(parts[-1])
    except ValueError:
        return None


def first_line(text: str | None) -> str | None:
    cleaned = clean_text(text)
    if not cleaned:
        return None
    return cleaned.splitlines()[0][:180]


async def telegram_snapshot_values(session, link: PostLink) -> dict[str, Any] | None:
    message_id = telegram_message_id(link.canonical_url or link.original_url)
    if message_id is None:
        return None
    source_post = (await session.execute(select(TelegramPost).where(TelegramPost.id == link.post_id))).scalar_one_or_none()
    candidates: list[tuple[TelegramPost, str]] = []
    if source_post:
        same_chat = (
            await session.execute(
                select(TelegramPost).where(
                    TelegramPost.chat_peer_id == source_post.chat_peer_id,
                    TelegramPost.message_id == message_id,
                    TelegramPost.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if same_chat:
            candidates.append((same_chat, "source_chat"))
    parsed = urlparse(link.canonical_url or link.original_url)
    slug = (parsed.path or "").strip("/").split("/", 1)[0]
    if slug:
        by_username = (
            await session.execute(
                select(TelegramPost)
                .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                .where(
                    TelegramChat.username == slug,
                    TelegramPost.message_id == message_id,
                    TelegramPost.is_deleted.is_(False),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if by_username:
            candidates.insert(0, (by_username, "username"))
    if not candidates:
        return None
    post, resolution = candidates[0]
    text = clean_text(post.text)
    if not text:
        return None
    now = datetime.now(timezone.utc)
    return {
        "link_id": link.id,
        "canonical_url": link.canonical_url,
        "final_url": link.canonical_url or link.original_url,
        "domain": domain_from_url(link.canonical_url or link.original_url),
        "http_status": 200,
        "content_type": "text/plain",
        "fetched_at": now,
        "title": first_line(text),
        "description": None,
        "site_name": "Telegram",
        "author": None,
        "published_at": post.date,
        "extracted_text": text,
        "extracted_text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "extraction_method": "telegram_local",
        "extraction_quality_score": 0.9,
        "image_url": None,
        "raw_metadata": {"telegram_post_id": post.id, "telegram_resolution": resolution},
        "error": None,
        "created_at": now,
        "updated_at": now,
    }


def meta_content(soup: Any, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return clean_text(tag.get("content"))
    return None


def extract_html(body: bytes, final_url: str | None) -> dict[str, Any]:
    html = body.decode("utf-8", errors="replace")
    title = description = extracted_text = image_url = site_name = author = None
    published_at = None
    method = "beautifulsoup"
    try:
        import trafilatura

        extracted_text = clean_text(trafilatura.extract(html, url=final_url, include_comments=False, include_tables=False))
        metadata = trafilatura.extract_metadata(html, default_url=final_url)
        if metadata:
            title = clean_text(getattr(metadata, "title", None))
            description = clean_text(getattr(metadata, "description", None))
            site_name = clean_text(getattr(metadata, "sitename", None))
            author = clean_text(getattr(metadata, "author", None))
            published_at = parse_datetime(getattr(metadata, "date", None))
        method = "trafilatura" if extracted_text else "beautifulsoup"
    except Exception:
        extracted_text = None

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        if not title and soup.title:
            title = clean_text(soup.title.get_text(" ", strip=True))
        description = description or meta_content(soup, "og:description", "description", "twitter:description")
        title = title or meta_content(soup, "og:title", "twitter:title")
        site_name = site_name or meta_content(soup, "og:site_name")
        author = author or meta_content(soup, "author", "article:author")
        image_url = meta_content(soup, "og:image", "twitter:image")
        published_at = published_at or parse_datetime(meta_content(soup, "article:published_time", "date", "pubdate"))
        if not extracted_text:
            extracted_text = clean_text(" ".join(paragraph.get_text(" ", strip=True) for paragraph in soup.find_all("p")))
    except Exception:
        pass

    text_len = len(extracted_text or "")
    quality = min(1.0, text_len / 2500) + (0.15 if title else 0) + (0.1 if description else 0)
    return {
        "title": title or None,
        "description": description or None,
        "site_name": site_name or None,
        "author": author or None,
        "published_at": published_at,
        "extracted_text": extracted_text or None,
        "extracted_text_hash": hashlib.sha256((extracted_text or "").encode("utf-8")).hexdigest() if extracted_text else None,
        "extraction_method": method,
        "extraction_quality_score": round(min(1.0, quality), 4),
        "image_url": image_url,
        "raw_metadata": {"final_url": final_url},
    }


def snapshot_values(link: PostLink, result: FetchResult) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "link_id": link.id,
        "canonical_url": link.canonical_url,
        "final_url": result.final_url,
        "domain": domain_from_url(result.final_url or link.canonical_url),
        "http_status": result.status_code,
        "content_type": result.content_type,
        "fetched_at": now,
        "error": result.error,
        "created_at": now,
        "updated_at": now,
    }
    if result.body and not result.error:
        content_type = (result.content_type or "").lower()
        if "text/html" in content_type or "application/xhtml" in content_type or content_type == "":
            values.update(extract_html(result.body, result.final_url))
        elif "pdf" in content_type:
            values.update({"extraction_method": "unsupported_pdf", "error": "PDF extraction is unsupported in MVP"})
        else:
            values.update({"extraction_method": "unsupported_content_type"})
    return values


async def enrich_pending(limit: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        result = await session.execute(
            select(PostLink)
            .outerjoin(LinkSnapshot, PostLink.id == LinkSnapshot.link_id)
            .where(or_(LinkSnapshot.id.is_(None), PostLink.extraction_status.in_(["pending", "failed"])))
            .order_by(PostLink.id)
            .limit(limit)
        )
        links = list(result.scalars())
        table = LinkSnapshot.__table__
        for link in links:
            values = await telegram_snapshot_values(session, link) if link.url_type == "telegram" else None
            if values is None:
                values = await youtube_snapshot_values(link, settings) if link.url_type == "youtube" else None
            result = None
            if values is None:
                result = await fetch_url(link.canonical_url or link.original_url, settings)
                values = snapshot_values(link, result)
            stmt = insert(table).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_link_snapshots_link_id",
                    set_={key: stmt.excluded[key] for key in values if key != "link_id"},
                )
            )
            await session.execute(
                update(PostLink)
                .where(PostLink.id == link.id)
                .values(
                    final_url=values.get("final_url") or (result.final_url if result else None),
                    domain=values.get("domain") or domain_from_url((result.final_url if result else None) or link.canonical_url),
                    extraction_status="failed" if values.get("error") else "done",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await mark_state(session, link.post_id, enrichment_status="failed" if values.get("error") else "done")
            count += 1
        await session.commit()
    return count


async def enrich_post_links(post_id: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        result = await session.execute(
            select(PostLink)
            .outerjoin(LinkSnapshot, PostLink.id == LinkSnapshot.link_id)
            .where(
                PostLink.post_id == post_id,
                or_(LinkSnapshot.id.is_(None), PostLink.extraction_status.in_(["pending", "failed"])),
            )
            .order_by(PostLink.position_index.nulls_last(), PostLink.id)
        )
        links = list(result.scalars())
        table = LinkSnapshot.__table__
        for link in links:
            values = await telegram_snapshot_values(session, link) if link.url_type == "telegram" else None
            if values is None:
                values = await youtube_snapshot_values(link, settings) if link.url_type == "youtube" else None
            fetch_result = None
            if values is None:
                fetch_result = await fetch_url(link.canonical_url or link.original_url, settings)
                values = snapshot_values(link, fetch_result)
            stmt = insert(table).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_link_snapshots_link_id",
                    set_={key: stmt.excluded[key] for key in values if key != "link_id"},
                )
            )
            await session.execute(
                update(PostLink)
                .where(PostLink.id == link.id)
                .values(
                    final_url=values.get("final_url") or (fetch_result.final_url if fetch_result else None),
                    domain=values.get("domain")
                    or domain_from_url((fetch_result.final_url if fetch_result else None) or link.canonical_url),
                    extraction_status="failed" if values.get("error") else "done",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await mark_state(session, link.post_id, enrichment_status="failed" if values.get("error") else "done")
            count += 1
        await session.commit()
    return count


@app.command("enrich-pending")
def enrich_pending_command(limit: int = limit_option(100)) -> None:
    """Fetch and extract metadata/text for pending links."""

    count = run_async(enrich_pending(limit))
    safe_echo(f"enriched={count}")


if __name__ == "__main__":
    app()
