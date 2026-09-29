from app.content.source_marking import marked_post_text


def test_marking_appends_attribution_without_rewriting_original():
    original = "Текст поста"
    url = "https://t.me/example/123"
    assert marked_post_text(original, url) == "Текст поста\n\nИсточник: [оригинальный пост](https://t.me/example/123)"
    assert original == "Текст поста"


def test_marking_media_only_and_existing_link():
    url = "https://t.me/c/123/456"
    assert marked_post_text(None, url) == f"Источник: [оригинальный пост]({url})"
    assert marked_post_text(f"Уже указан {url}", url).endswith(f"Источник: [оригинальный пост]({url})")
    marked = marked_post_text("Текст", url)
    assert marked_post_text(marked, url) == marked
