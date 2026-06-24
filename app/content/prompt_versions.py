from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.content.yandex_gpt import load_prompt_config
from app.models import RewritePromptVersion

PIPELINE_REWRITE_PROMPT = "pipeline_rewrite"
LINK_SUMMARY_PROMPT = "link_summary"


def default_prompt_values(name: str) -> dict[str, Any]:
    config = load_prompt_config()
    if name == PIPELINE_REWRITE_PROMPT:
        rewrite = config.get("rewrite") or {}
        return {
            "system_prompt": rewrite.get("system") or "",
            "common_user_prompt": rewrite.get("common_user") or "",
            "label_prompts": rewrite.get("labels") or {},
            "raw_config": {"rewrite": rewrite},
        }
    if name == LINK_SUMMARY_PROMPT:
        summary = config.get("summary") or {}
        return {
            "system_prompt": summary.get("system") or "",
            "common_user_prompt": summary.get("user") or "",
            "label_prompts": {},
            "raw_config": {"summary": summary},
        }
    raise ValueError(f"Unknown prompt name: {name}")


def prompt_config_from_version(version: RewritePromptVersion) -> dict[str, Any]:
    if version.name == LINK_SUMMARY_PROMPT:
        return {
            "summary": {
                "system": version.system_prompt,
                "user": version.common_user_prompt,
            }
        }
    return {
        "rewrite": {
            "system": version.system_prompt,
            "common_user": version.common_user_prompt,
            "labels": version.label_prompts or {},
        }
    }


async def ensure_active_prompt_version(
    session,
    *,
    name: str,
    created_by: str = "system",
) -> RewritePromptVersion:
    active = (
        await session.execute(
            select(RewritePromptVersion)
            .where(RewritePromptVersion.name == name, RewritePromptVersion.is_active.is_(True))
            .order_by(RewritePromptVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active:
        return active

    values = default_prompt_values(name)
    table = RewritePromptVersion.__table__
    stmt = insert(table).values(
        name=name,
        version=1,
        is_active=True,
        created_by=created_by,
        created_at=datetime.now(timezone.utc),
        **values,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_rewrite_prompt_versions_name_version",
            set_={
                "is_active": True,
                "system_prompt": stmt.excluded.system_prompt,
                "common_user_prompt": stmt.excluded.common_user_prompt,
                "label_prompts": stmt.excluded.label_prompts,
                "raw_config": stmt.excluded.raw_config,
            },
        )
    )
    await session.commit()
    return (
        await session.execute(
            select(RewritePromptVersion)
            .where(RewritePromptVersion.name == name, RewritePromptVersion.is_active.is_(True))
            .order_by(RewritePromptVersion.version.desc())
            .limit(1)
        )
    ).scalar_one()


async def create_prompt_version(
    session,
    *,
    name: str,
    system_prompt: str,
    common_user_prompt: str,
    label_prompts: dict[str, Any] | None = None,
    created_by: str = "web",
) -> RewritePromptVersion:
    latest_version = (
        await session.execute(select(func.max(RewritePromptVersion.version)).where(RewritePromptVersion.name == name))
    ).scalar_one()
    next_version = int(latest_version or 0) + 1
    await session.execute(
        update(RewritePromptVersion)
        .where(RewritePromptVersion.name == name, RewritePromptVersion.is_active.is_(True))
        .values(is_active=False)
    )
    values = {
        "name": name,
        "version": next_version,
        "is_active": True,
        "system_prompt": system_prompt,
        "common_user_prompt": common_user_prompt,
        "label_prompts": label_prompts or {},
        "raw_config": prompt_config_from_values(name, system_prompt, common_user_prompt, label_prompts or {}),
        "created_by": created_by,
        "created_at": datetime.now(timezone.utc),
    }
    await session.execute(insert(RewritePromptVersion.__table__).values(**values))
    await session.commit()
    return (
        await session.execute(
            select(RewritePromptVersion)
            .where(RewritePromptVersion.name == name, RewritePromptVersion.version == next_version)
            .limit(1)
        )
    ).scalar_one()


def prompt_config_from_values(
    name: str,
    system_prompt: str,
    common_user_prompt: str,
    label_prompts: dict[str, Any],
) -> dict[str, Any]:
    if name == LINK_SUMMARY_PROMPT:
        return {"summary": {"system": system_prompt, "user": common_user_prompt}}
    return {"rewrite": {"system": system_prompt, "common_user": common_user_prompt, "labels": label_prompts}}
