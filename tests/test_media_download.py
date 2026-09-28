from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.sync import download_message_media


def settings(tmp_path: Path, limit: int = 100) -> Settings:
    return Settings(
        _env_file=None,
        DB_DSN="postgresql+asyncpg://user:pass@localhost:5432/db",
        MEDIA_DIR=str(tmp_path),
        MEDIA_MAX_BYTES=limit,
        DOWNLOAD_MEDIA=True,
    )


class FakeMessage:
    def __init__(self, payload: bytes, declared_size: int | None = None):
        self.media = object()
        self.file = SimpleNamespace(size=declared_size if declared_size is not None else len(payload))
        self.payload = payload
        self.called = False

    async def download_media(self, file: str, progress_callback):
        self.called = True
        progress_callback(len(self.payload), len(self.payload))
        target = Path(file) / "file.bin"
        target.write_bytes(self.payload)
        return str(target)


@pytest.mark.asyncio
async def test_small_media_is_downloaded_atomically(tmp_path):
    message = FakeMessage(b"12345")

    result = await download_message_media(settings(tmp_path), message, 42, 7)

    assert result.status == "downloaded"
    assert result.size_bytes == 5
    assert Path(result.path).read_bytes() == b"12345"
    assert not list((tmp_path / "42" / "7").glob(".tmp-*"))


@pytest.mark.asyncio
async def test_declared_large_media_is_skipped_without_download(tmp_path):
    message = FakeMessage(b"small", declared_size=101)

    result = await download_message_media(settings(tmp_path), message, 42, 8)

    assert result.status == "skipped_too_large"
    assert result.size_bytes == 101
    assert message.called is False


@pytest.mark.asyncio
async def test_stream_crossing_limit_is_removed(tmp_path):
    message = FakeMessage(b"x" * 101, declared_size=None)
    message.file.size = None

    result = await download_message_media(settings(tmp_path), message, 42, 9)

    assert result.status == "skipped_too_large"
    assert not any((tmp_path / "42" / "9").rglob("*.bin"))
