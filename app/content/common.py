from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar

import typer
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import create_session_factory
from app.logging_setup import setup_logging
from app.main import safe_echo

T = TypeVar("T")


def settings_or_exit() -> Settings:
    try:
        settings = get_settings()
    except ValidationError as exc:
        safe_echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc
    setup_logging(settings.log_level)
    ensure_runtime_dirs(settings)
    return settings


def ensure_runtime_dirs(settings: Settings) -> None:
    for path in [
        settings.media_dir,
        settings.cache_dir,
        settings.artifacts_dir,
        settings.reports_dir,
        str(Path(settings.classifier_active_model_path).parent),
    ]:
        Path(path).mkdir(parents=True, exist_ok=True)


def run_async(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


def session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(settings)


def limit_option(default: int = 500) -> Any:
    return typer.Option(default, "--limit", min=1, help="Maximum rows to process in one run.")

