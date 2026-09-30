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
async def test_retired_mutations_have_no_handler(monkeypatch, path, data):
    monkeypatch.setenv("DB_DSN", "postgresql+asyncpg://test:test@localhost:5432/test")
    monkeypatch.setenv("ENABLE_PROCESSING", "false")
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)
    from app.web.main import create_app

    app = create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(path, data=data)

    assert response.status_code in {404, 405}


@pytest.mark.parametrize("processing", ["false", "true"])
async def test_old_pages_are_not_revived_by_processing_flag(monkeypatch, processing):
    from app.config import get_settings
    from app.web.main import create_app

    monkeypatch.setenv("DB_DSN", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("ENABLE_PROCESSING", processing)
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)
    get_settings.cache_clear()
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in (
            "/processed",
            "/drafts",
            "/rewrite-progress",
            "/pipeline-prompts",
            "/models",
            "/labels",
            "/yandex-genres",
            "/codex-training",
            "/topic-audit",
            "/search",
            "/api/stats",
            "/api/rewrite-progress",
        ):
            assert (await client.get(path)).status_code == 404, path
        response = await client.get("/")
        assert (
            response.status_code == 303 and response.headers["location"] == "/dashboard"
        )
    get_settings.cache_clear()


async def test_dashboard_is_private(monkeypatch):
    from app.config import get_settings
    from app.web.main import create_app

    monkeypatch.setenv("DB_DSN", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("WEB_BASIC_AUTH_USER", "test")
    monkeypatch.setenv("WEB_BASIC_AUTH_PASSWORD", "test-password")
    get_settings.cache_clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        for path in (
            "/dashboard",
            "/api/dashboard",
            "/pipeline",
            "/marks",
            "/pipeline/filters",
        ):
            assert (await client.get(path)).status_code == 401
    get_settings.cache_clear()
