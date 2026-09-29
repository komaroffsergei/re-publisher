from types import SimpleNamespace

from app.web.source_media import album_primary, downloaded_media_path, source_media_items


def post(post_id, message_id, text, path, media_type, status="downloaded"):
    return SimpleNamespace(
        id=post_id,
        message_id=message_id,
        text=text,
        media_path=str(path) if path else None,
        media_type=media_type,
        media_download_status=status,
        media_size_bytes=path.stat().st_size if path and path.exists() else None,
    )


def test_album_uses_caption_and_shows_every_downloaded_part(tmp_path):
    image = tmp_path / "photo.jpg"
    video = tmp_path / "clip.MP4"
    image.write_bytes(b"image")
    video.write_bytes(b"video")
    caption = post(9367, 36899, "Album caption", video, "MessageMediaDocument")
    picture = post(9366, 36900, "", image, "MessageMediaPhoto")

    assert album_primary([picture, caption]) is caption
    items = source_media_items([picture, caption], str(tmp_path))
    assert [(item["post_id"], item["kind"]) for item in items] == [
        (9367, "video"), (9366, "image")
    ]
    assert [item["url"] for item in items] == ["/source-media/9367", "/source-media/9366"]


def test_media_path_cannot_escape_root_or_serve_unavailable_file(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside.html"
    outside.write_text("<script>alert(1)</script>", encoding="utf-8")
    unsafe = post(1, 1, "", outside, "MessageMediaDocument")
    assert downloaded_media_path(unsafe, str(root)) is None
    assert source_media_items([unsafe], str(root))[0]["url"] is None

    inside = root / "document.html"
    inside.write_text("<script>alert(1)</script>", encoding="utf-8")
    safe = post(2, 2, "", inside, "MessageMediaDocument")
    assert source_media_items([safe], str(root))[0]["kind"] == "file"
    safe.media_download_status = "failed"
    assert downloaded_media_path(safe, str(root)) is None
