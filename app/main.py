from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import typer
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from telethon.errors import RPCError, SessionPasswordNeededError

from app.config import Settings, get_settings
from app.folders import extract_filter_title, resolve_folder_chats, fetch_dialog_filters
from app.logging_setup import setup_logging
from app.sync import run_service, sync_folder
from app.telegram_client import create_telegram_client

app = typer.Typer(no_args_is_help=True)


def safe_text(value: Any, *, stream=None) -> str:
    text = str(value)
    target = stream or sys.stdout
    encoding = getattr(target, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def safe_echo(value: Any = "", *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    typer.echo(safe_text(value, stream=stream), err=err)


def settings_or_exit() -> Settings:
    try:
        settings = get_settings()
    except ValidationError as exc:
        safe_echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc
    setup_logging(settings.log_level)
    return settings


def run_async(coro: Any) -> None:
    try:
        asyncio.run(coro)
    except (RuntimeError, ValueError) as exc:
        safe_echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


async def connect_authorized(settings: Settings):
    client = create_telegram_client(settings)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram session is not authorized. Run: python -m app.main login")
    return client


def sent_code_summary(sent: Any) -> str:
    code_type = getattr(sent, "type", None)
    next_type = getattr(sent, "next_type", None)
    timeout = getattr(sent, "timeout", None)
    delivery = code_type.__class__.__name__ if code_type else "unknown"
    fallback = next_type.__class__.__name__ if next_type else "none"
    timeout_text = "none" if timeout is None else str(timeout)
    return f"delivery={delivery} fallback={fallback} timeout={timeout_text}"


def print_qr_code(url: str) -> None:
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, invert=True)


@app.command()
def login(force_sms: bool = typer.Option(False, "--force-sms", help="Ask Telegram for SMS delivery when possible.")) -> None:
    """Create or validate a user MTProto session."""

    async def _login() -> None:
        settings = settings_or_exit()
        client = create_telegram_client(settings)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                phone = settings.tg_phone or typer.prompt("Telegram phone")
                try:
                    sent = await client.send_code_request(phone, force_sms=force_sms)
                except RPCError as exc:
                    raise RuntimeError(f"Telegram refused to send code: {exc}") from exc
                safe_echo(f"Telegram code requested: {sent_code_summary(sent)}")
                code = typer.prompt("Telegram code")
                try:
                    await client.sign_in(phone=phone, code=code)
                except SessionPasswordNeededError:
                    password = typer.prompt("Telegram 2FA password", hide_input=True)
                    await client.sign_in(password=password)
            me = await client.get_me()
            safe_echo(
                f"Logged in: id={getattr(me, 'id', None)} "
                f"username={getattr(me, 'username', None)} phone={getattr(me, 'phone', None)}"
            )
        finally:
            await client.disconnect()

    run_async(_login())


@app.command("login-qr")
def login_qr(
    timeout: int = typer.Option(120, "--timeout", min=30, help="Seconds to wait for QR scan."),
    show_url: bool = typer.Option(False, "--show-url", help="Also print the sensitive QR login URL."),
) -> None:
    """Create a user MTProto session by scanning a Telegram QR code."""

    async def _login_qr() -> None:
        settings = settings_or_exit()
        client = create_telegram_client(settings)
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                safe_echo(
                    f"Already logged in: id={getattr(me, 'id', None)} "
                    f"username={getattr(me, 'username', None)} phone={getattr(me, 'phone', None)}"
                )
                return

            qr_login = await client.qr_login()
            wait_task = asyncio.create_task(qr_login.wait(timeout=timeout))
            await asyncio.sleep(0)
            safe_echo("Scan this QR in Telegram: Settings -> Devices -> Link Desktop Device")
            print_qr_code(qr_login.url)
            if show_url:
                safe_echo(f"QR login URL: {qr_login.url}")
            try:
                me = await wait_task
            except asyncio.TimeoutError as exc:
                raise RuntimeError("QR login timed out. Run login-qr again to generate a fresh QR.") from exc
            except SessionPasswordNeededError:
                password = typer.prompt("Telegram 2FA password", hide_input=True)
                await client.sign_in(password=password)
                me = await client.get_me()
            safe_echo(
                f"Logged in: id={getattr(me, 'id', None)} "
                f"username={getattr(me, 'username', None)} phone={getattr(me, 'phone', None)}"
            )
        finally:
            await client.disconnect()

    run_async(_login_qr())


@app.command("inspect-folders")
def inspect_folders() -> None:
    """Print all Telegram dialog filters/folders."""

    async def _inspect() -> None:
        settings = settings_or_exit()
        client = await connect_authorized(settings)
        try:
            filters = await fetch_dialog_filters(client)
            for item in filters:
                safe_echo(f"id={getattr(item, 'id', None)} title={extract_filter_title(item)}")
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
                safe_echo(
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
    safe_echo("Migrations applied")


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
