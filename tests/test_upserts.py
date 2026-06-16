from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app.sync import build_post_upsert


def test_post_upsert_uses_on_conflict():
    stmt = build_post_upsert(
        {
            "chat_peer_id": -1001,
            "message_id": 1,
            "raw": {},
            "is_deleted": False,
        }
    )

    compiled = str(stmt.compile(dialect=postgresql.dialect()))

    assert "ON CONFLICT" in compiled
    assert "uq_telegram_posts_chat_message" in compiled
