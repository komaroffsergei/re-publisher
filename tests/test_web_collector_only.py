from __future__ import annotations

import httpx
import pytest

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/api/pipeline/1/block", {"reason": "test"}),
        ("/api/pipeline/1/allow", {}),
        ("/api/pipeline/1/schedule", {"scheduled_publish_at": "2026-09-29T12:00"}),
        ("/api/pipeline/1/rewrite", {}),
        ("/api/drafts/1/approve", {}),
    ],
)
async def test_processing_mutations_are_rejected_in_collector_mode(monkeypatch, path, data):
    monkeypatch.setenv("DB_DSN", "postgresql+asyncpg://test:test@localhost:5432/test")
    monkeypatch.setenv("ENABLE_PROCESSING", "false")
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)
    from app.web.main import create_app

    app = create_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, data=data)

    assert response.status_code == 409
