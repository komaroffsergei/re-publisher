from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import typer
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.text_utils import clean_text
from app.main import safe_echo
from app.models import ContentItem, PostClassification, PublicationDraft, SearchDocument, TelegramChat, TelegramPost

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Search indexing commands."""


async def upsert_document(session, values: dict[str, Any]) -> None:
    body = "\n".join(clean_text(values.get(key)) for key in ["title", "body", "source_text"])
    values = values | {
        "tsv": func.to_tsvector("russian", body),
        "updated_at": datetime.now(timezone.utc),
        "created_at": values.get("created_at") or datetime.now(timezone.utc),
    }
    table = SearchDocument.__table__
    stmt = insert(table).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_search_documents_entity",
            set_={key: stmt.excluded[key] for key in values if key not in {"entity_type", "entity_id", "created_at"}},
        )
    )


async def reindex(limit: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        content_rows = await session.execute(
            select(ContentItem, PostClassification)
            .outerjoin(PostClassification, PostClassification.content_item_id == ContentItem.id)
            .order_by(ContentItem.id.desc())
            .limit(limit)
        )
        for item, classification in content_rows.all():
            await upsert_document(
                session,
                {
                    "entity_type": "content_item",
                    "entity_id": item.id,
                    "title": item.translated_title or item.title,
                    "body": item.translated_summary or item.source_summary,
                    "source_text": item.main_text,
                    "labels": [classification.label_primary] if classification and classification.label_primary else [],
                    "source_domain": item.source_domain,
                    "language": item.source_lang,
                },
            )
            count += 1

        draft_rows = await session.execute(select(PublicationDraft).order_by(PublicationDraft.id.desc()).limit(limit))
        for draft in draft_rows.scalars():
            await upsert_document(
                session,
                {
                    "entity_type": "publication_draft",
                    "entity_id": draft.id,
                    "title": draft.title,
                    "body": draft.body,
                    "labels": draft.tags or [],
                    "source_domain": draft.source_domain,
                },
            )
            count += 1

        post_rows = await session.execute(
            select(TelegramPost, TelegramChat)
            .outerjoin(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
            .order_by(TelegramPost.id.desc())
            .limit(limit)
        )
        for post, chat in post_rows.all():
            await upsert_document(
                session,
                {
                    "entity_type": "telegram_post",
                    "entity_id": post.id,
                    "title": chat.title if chat else None,
                    "body": post.text,
                    "source_text": post.text,
                    "labels": [],
                    "source_chat": chat.title if chat else str(post.chat_peer_id),
                },
            )
            count += 1
        await session.commit()
    return count


def build_search_statement(
    q: str | None = None,
    label: str | None = None,
    source_domain: str | None = None,
    source_chat: str | None = None,
    language: str | None = None,
):
    stmt = select(SearchDocument)
    if q:
        stmt = stmt.where(SearchDocument.tsv.op("@@")(func.plainto_tsquery("russian", q)))
    if label:
        stmt = stmt.where(SearchDocument.labels.contains([label]))
    if source_domain:
        stmt = stmt.where(SearchDocument.source_domain == source_domain)
    if source_chat:
        stmt = stmt.where(SearchDocument.source_chat == source_chat)
    if language:
        stmt = stmt.where(SearchDocument.language == language)
    return stmt.order_by(SearchDocument.updated_at.desc())


async def search_documents(**filters):
    settings = settings_or_exit()
    page = max(1, int(filters.pop("page", 1) or 1))
    page_size = min(100, max(1, int(filters.pop("page_size", 25) or 25)))
    factory = session_factory(settings)
    async with factory() as session:
        result = await session.execute(build_search_statement(**filters).offset((page - 1) * page_size).limit(page_size))
        return list(result.scalars())


@app.command("reindex")
def reindex_command(limit: int = limit_option(1000)) -> None:
    """Rebuild PostgreSQL full-text search documents."""

    count = run_async(reindex(limit))
    safe_echo(f"search_documents={count}")


if __name__ == "__main__":
    app()
