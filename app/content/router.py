from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
import yaml
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.state import mark_state
from app.main import safe_echo
from app.models import ContentItem, PostClassification, PublicationTarget, Showcase

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Routing commands."""


def load_rules(path: Path = Path("config/showcase_rules.yaml")) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("rules") or [])


def matches_rule(item: ContentItem, classification: PostClassification, rule: dict[str, Any]) -> bool:
    label = classification.label_primary
    confidence = float(classification.confidence or 0)
    if label in set(rule.get("excluded_labels") or []):
        return False
    if label not in set(rule.get("labels") or []):
        return False
    if confidence < float(rule.get("min_confidence") or 0):
        return False
    if rule.get("require_source_link") and not item.source_url:
        return False
    if rule.get("require_image") and not item.primary_image_asset_id:
        return False
    if float(item.quality_score or 0) < float(rule.get("min_quality_score") or 0):
        return False
    return True


async def route_new(limit: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    rules = load_rules()
    count = 0
    async with factory() as session:
        showcase_rows = await session.execute(select(Showcase))
        showcases = {showcase.slug: showcase for showcase in showcase_rows.scalars()}
        result = await session.execute(
            select(ContentItem, PostClassification)
            .join(PostClassification, PostClassification.content_item_id == ContentItem.id)
            .outerjoin(PublicationTarget, PublicationTarget.content_item_id == ContentItem.id)
            .where(PublicationTarget.id.is_(None))
            .order_by(ContentItem.id)
            .limit(limit)
        )
        rows = list(result.all())
        table = PublicationTarget.__table__
        for item, classification in rows:
            matched = [rule for rule in rules if matches_rule(item, classification, rule)]
            if not matched:
                matched = [{"showcase": "quarantine", "rewrite_template": "news_short"}]
            for rule in matched:
                showcase = showcases.get(rule["showcase"])
                if not showcase:
                    continue
                values = {
                    "content_item_id": item.id,
                    "showcase_id": showcase.id,
                    "route_reason": f"label={classification.label_primary} confidence={float(classification.confidence or 0):.3f}",
                    "route_score": float(classification.confidence or 0),
                    "status": "pending",
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
                stmt = insert(table).values(**values)
                await session.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_publication_targets_item_showcase",
                        set_={key: stmt.excluded[key] for key in values if key not in {"created_at"}},
                    )
                )
                count += 1
            await mark_state(session, item.source_post_id, routing_status="done")
        await session.commit()
    return count


@app.command("route-new")
def route_new_command(limit: int = limit_option()) -> None:
    """Route classified materials to showcases."""

    count = run_async(route_new(limit))
    safe_echo(f"routes={count}")


if __name__ == "__main__":
    app()
