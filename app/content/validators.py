from __future__ import annotations

from datetime import datetime, timezone

import typer
from sqlalchemy import select, update

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.state import mark_state
from app.content.text_utils import clean_text
from app.main import safe_echo
from app.models import PublicationDraft, PublicationTarget, Showcase

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Draft validation commands."""


def validate_draft(draft: PublicationDraft, showcase: Showcase) -> list[str]:
    errors: list[str] = []
    body = clean_text(draft.body)
    if not body:
        errors.append("body_empty")
    if len(body) > 4096:
        errors.append("body_too_long")
    if showcase.target_type == "telegram" and not draft.source_url and "missing_source_url" not in (draft.risk_flags or []):
        errors.append("source_url_missing")
    if draft.similarity_to_original is not None and float(draft.similarity_to_original) > 0.92:
        errors.append("too_similar_to_original")
    if "unsupported_claim" in (draft.risk_flags or []):
        errors.append("unsupported_claim")
    return errors


async def validate_pending(limit: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        result = await session.execute(
            select(PublicationDraft, PublicationTarget, Showcase)
            .join(PublicationTarget, PublicationTarget.id == PublicationDraft.publication_target_id)
            .join(Showcase, Showcase.id == PublicationTarget.showcase_id)
            .where(PublicationDraft.status.in_(["draft", "needs_review", "approved"]))
            .order_by(PublicationDraft.id)
            .limit(limit)
        )
        rows = list(result.all())
        for draft, target, showcase in rows:
            errors = validate_draft(draft, showcase)
            if "body_empty" in errors:
                status = "rejected"
            elif settings.auto_approve_drafts and not errors and not draft.risk_flags:
                status = "approved"
            elif draft.status == "approved" and not errors:
                status = "approved"
            else:
                status = "needs_review"
            await session.execute(
                update(PublicationDraft)
                .where(PublicationDraft.id == draft.id)
                .values(status=status, validation_errors=errors, updated_at=datetime.now(timezone.utc))
            )
            await mark_state(session, draft.source_post_id, rewrite_status=status)
            count += 1
        await session.commit()
    return count


@app.command("validate-drafts")
def validate_drafts_command(limit: int = limit_option()) -> None:
    """Validate publication drafts and set review/approved/rejected status."""

    count = run_async(validate_pending(limit))
    safe_echo(f"validated={count}")


if __name__ == "__main__":
    app()
