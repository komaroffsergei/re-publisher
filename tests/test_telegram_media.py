from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.web.telegram_media import inline_media_type, local_telegram_media_path, telegram_media_for_post


def test_local_media_path_must_exist_inside_media_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    video = root / "post.MP4"
    video.write_bytes(b"video")
    outside = tmp_path / "outside.MP4"
    outside.write_bytes(b"outside")

    assert local_telegram_media_path(str(root), str(video)) == video.resolve()
    assert local_telegram_media_path(str(root), str(outside)) is None
    assert local_telegram_media_path(str(root), str(root / "missing.mp4")) is None
    assert inline_media_type(video) == ("video/mp4", "video")
    assert inline_media_type(Path("script.svg")) == ("application/octet-stream", "file")


@pytest.mark.asyncio
async def test_album_uses_all_downloaded_members_without_registration(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    files = []
    for name in ("one.mp4", "two.jpg"):
        path = root / name
        path.write_bytes(b"media")
        files.append(path)
    members = [
        SimpleNamespace(id=9367, chat_peer_id=-100, grouped_id=42, message_id=36899,
                        media_type="MessageMediaDocument", media_path=str(files[0]),
                        media_download_status="downloaded", is_deleted=False),
        SimpleNamespace(id=9366, chat_peer_id=-100, grouped_id=42, message_id=36900,
                        media_type="MessageMediaPhoto", media_path=str(files[1]),
                        media_download_status="downloaded", is_deleted=False),
        SimpleNamespace(id=9365, chat_peer_id=-100, grouped_id=42, message_id=36901,
                        media_type="MessageMediaPhoto", media_path=None,
                        media_download_status="skipped_too_large", is_deleted=False),
    ]

    class Result:
        def scalars(self):
            return members

    class Session:
        async def execute(self, _statement):
            return Result()

    media = await telegram_media_for_post(Session(), members[0], str(root))
    assert [item["message_id"] for item in media] == [36899, 36900, 36901]
    assert [item["kind"] for item in media] == ["video", "image", "file"]
    assert [item["available"] for item in media] == [True, True, False]
    assert media[2]["download_status"] == "skipped_too_large"
    assert media[2]["download_status_label"] == "файл больше лимита 100 МБ"
    assert all("media_path" not in item for item in media)


@pytest.mark.asyncio
async def test_downloaded_media_requires_auth_and_stays_inside_media_root(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    photo = root / "post.jpg"
    photo.write_bytes(b"photo")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"private")

    monkeypatch.setenv("DB_DSN", "postgresql+asyncpg://test:test@localhost:5432/test")
    monkeypatch.setenv("MEDIA_DIR", str(root))
    monkeypatch.setenv("WEB_BASIC_AUTH_USER", "owner")
    monkeypatch.setenv("WEB_BASIC_AUTH_PASSWORD", "test-password")
    from app.config import get_settings
    from app.web.main import create_app

    get_settings.cache_clear()
    app = create_app()
    current_post = SimpleNamespace(media_path=str(photo))

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: current_post)

    app.state.session_factory = Session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            unauthorized = await client.get("/telegram-media/9367")
            authorized = await client.get("/telegram-media/9367", auth=("owner", "test-password"))
            current_post.media_path = str(outside)
            escaped = await client.get("/telegram-media/9367", auth=("owner", "test-password"))
    finally:
        get_settings.cache_clear()

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.content == b"photo"
    assert authorized.headers["content-type"] == "image/jpeg"
    assert authorized.headers["cache-control"] == "private, no-store"
    assert escaped.status_code == 404
