from __future__ import annotations

from app.main import safe_text


class FakeStream:
    encoding = "cp1251"


def test_safe_text_replaces_unencodable_characters():
    assert safe_text("AI 🤖", stream=FakeStream()) == "AI ?"
