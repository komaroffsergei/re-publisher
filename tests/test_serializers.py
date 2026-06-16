from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.serializers import make_json_serializable, message_to_post_dict


@dataclass
class FakeNested:
    when: datetime


class FakeMessage:
    id = 10
    from_id = None
    sender_id = 42
    date = datetime(2026, 1, 2, tzinfo=timezone.utc)
    edit_date = None
    raw_text = "hello"
    message = "hello"
    grouped_id = None
    views = 5
    forwards = 1
    replies = None
    media = None

    def to_dict(self):
        return {"payload": b"abc", "nested": FakeNested(self.date)}


class FakeChat:
    id = 100


def test_json_serialization_helper_handles_datetime_bytes_and_dataclass():
    result = make_json_serializable({"ts": datetime(2026, 1, 1, tzinfo=timezone.utc), "bytes": b"abc"})

    assert result["ts"] == "2026-01-01T00:00:00+00:00"
    assert result["bytes"] == "YWJj"


def test_message_to_post_dict_extracts_required_fields():
    data = message_to_post_dict(None, FakeChat(), FakeMessage())

    assert data["message_id"] == 10
    assert data["sender_peer_id"] == 42
    assert data["text"] == "hello"
    assert data["raw"]["payload"] == "YWJj"
