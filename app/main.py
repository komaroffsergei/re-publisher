from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from telethon.errors import SessionPasswordNeededError

from app.config import Settings, get_settings
from app.folders import extract_filter_title, resolve_folder_chats, fetch_dialog_filters
from app.logging_setup import setup_logging
from app.sync import run_service, sync_folder
from app.telegram_client import create_telegram_client

app = typer.Typer(no_args_is_help=True)


def settings_or_exit() -> Settings:
    try:
        settings = get_settings()
    except ValidationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc
    setup_logging(settings.log_level)
    return settings


def run_async(coro: Any) -> None:
    try:
        asyncio.run(coro)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


async def connect_authorized(settings: Settings):
    client = create_telegram_client(settings)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram session is not authorized. Run: python -m app.main login")
    return client


@app.command()
def login() -> None:
    """Create or validate a user MTProto session."""

    async def _login() -> None:
        settings = settings_or_exit()
        client = create_telegram_client(settings)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                phone = settings.tg_phone or typer.prompt("Telegram phone")
                await client.send_code_request(phone)
                code = typer.prompt("Telegram code")
                try:
                    await client.sign_in(phone=phone, code=code)
                except SessionPasswordNeededError:
                    password = typer.prompt("Telegram 2FA password", hide_input=True)
                    await client.sign_in(password=password)
            me = await client.get_me()
            typer.echo(
                f"Logged in: id={getattr(me, 'id', None)} "
                f"username={getattr(me, 'username', None)} phone={getattr(me, 'phone', None)}"
            )
        finally:
            await client.disconnect()

    run_async(_login())


@app.command("inspect-folders")
def inspect_folders() -> None:
    """Print all Telegram dialog filters/folders."""

    async def _inspect() -> None:
        settings = settings_or_exit()
        client = await connect_authorized(settings)
        try:
            filters = await fetch_dialog_filters(client)
            for item in filters:
                typer.echo(f"id={getattr(item, 'id', None)} title={extract_filter_title(item)}")
        finally:
            await client.disconnect()

    run_async(_inspect())


@app.command("inspect-folder")
def inspect_folder(folder: str = typer.Option(None, "--folder", "-f")) -> None:
    """Print chats resolved from a Telegram folder."""

    async def _inspect() -> None:
        settings = settings_or_exit()
        target_folder = folder or settings.folder_name
        client = await connect_authorized(settings)
        try:
            chats = await resolve_folder_chats(client, target_folder)
            for chat in chats:
                typer.echo(
                    f"peer_id={chat.peer_id} title={chat.title!r} "
                    f"username={chat.username!r} type={chat.chat_type}"
                )
        finally:
            await client.disconnect()

    run_async(_inspect())


@app.command()
def migrate() -> None:
    """Run Alembic migrations."""
    settings_or_exit()
    alembic_ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    command.upgrade(Config(str(alembic_ini)), "head")
    typer.echo("Migrations applied")


@app.command()
def sync(folder: str = typer.Option(None, "--folder", "-f")) -> None:
    """Perform an initial/incremental sync for the folder."""

    async def _sync() -> None:
        settings = settings_or_exit()
        await sync_folder(settings, folder or settings.folder_name)

    run_async(_sync())


@app.command()
def run(folder: str = typer.Option(None, "--folder", "-f")) -> None:
    """Run the daemon service."""

    async def _run() -> None:
        settings = settings_or_exit()
        await run_service(settings, folder or settings.folder_name)

    run_async(_run())


if __name__ == "__main__":
    app()
