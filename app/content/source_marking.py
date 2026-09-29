"""Prepare an attributed copy of a Telegram post without changing its original."""


def marked_post_text(text: str | None, source_url: str) -> str:
    body = (text or "").strip()
    attribution = f"Источник: [оригинальный пост]({source_url})"
    if attribution in body:
        return body
    return f"{body}\n\n{attribution}" if body else attribution
