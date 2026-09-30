"""Real PostgreSQL + HTTP checks. Only run against a disposable QA database."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.config import get_settings
from app.content.selection_filters import application_batch, evaluate_completed_job, load_states, remove_mark
from app.content.selection_rules import taxonomy_catalog
from app.models import (FilterApplication, FilterEvaluation, FilterMark, FilterMarkEvent, PipelineEntry,
    PostFilterMark, SelectionFilter, SelectionFilterVersion, TaxonomyClassification, TaxonomyRun, TelegramChat, TelegramPost)
from app.taxonomy.jobs import invalidate_if_edited, text_sha256
from app.taxonomy.worker import finish_job
from app.web.main import create_app

DSN = os.environ.get("FILTER_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="FILTER_TEST_DSN for disposable PostgreSQL not provided")


@pytest_asyncio.fixture
async def db():
    assert make_url(DSN).database.startswith("publisher_filter_qa"), "Never run against production DB"
    engine = create_async_engine(DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("TRUNCATE filter_marks, selection_filters, telegram_chats, telegram_posts, pipeline_entries CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db):
    get_settings.cache_clear()
    app = create_app()
    app.state.session_factory = db
    app.state.settings.web_basic_auth_user = "qa"
    app.state.settings.web_basic_auth_password = "qa-password"
    app.state.settings.taxonomy_enabled = True
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa.local",
                                    auth=("qa", "qa-password")) as connection:
            yield connection
    finally:
        # Settings кэшируются на процесс: авторизация QA не должна утекать в другие тесты.
        get_settings.cache_clear()


async def seed(factory, legacy=False):
    async with factory() as session:
        chat = TelegramChat(peer_id=-1001234567890, title="QA chat", username="selection_qa", chat_type="channel", folder_name="MAX")
        post = TelegramPost(chat_peer_id=chat.peer_id, message_id=1, text="QA sample", date=datetime.now(timezone.utc), is_deleted=False, raw={})
        session.add_all([chat, post]); await session.flush()
        entry = PipelineEntry(source_post_id=post.id, stage="sorted", status="taxonomy_sorted")
        session.add(entry); await session.flush()
        scores = dict.fromkeys((label["id"] for label in taxonomy_catalog()["labels"]), .8)
        result = {"top_3": [], "review_status": "scored"} if legacy else {
            "taxonomy_version": taxonomy_catalog()["version"], "scores": scores, "top_3": [], "review_status": "scored"}
        for key in ("tfidf", "minilm"):
            run = TaxonomyRun(pipeline_entry_id=entry.id, source_post_id=post.id, model_key=key,
                status="complete", text_sha256=text_sha256(post.text), result=result)
            session.add(run); await session.flush()
            session.add(TaxonomyClassification(pipeline_entry_id=entry.id, source_post_id=post.id, model_key=key,
                current_run_id=run.id, status="complete", text_sha256=text_sha256(post.text), result=result))
        await session.commit()
        return entry.id


async def save_rule(client, mark_id, name="Rule", model="tfidf", threshold=60, item=None, enabled=True):
    draft = {"name": name, "model_key": model, "mark_id": mark_id, "enabled": enabled,
        "expression": {"op": "condition", "label_id": "tool_description", "compare": "gte", "threshold": threshold}}
    if item:
        draft.update(filter_id=item["id"], base_version_id=item["base_version_id"])
    response = await client.post("/api/pipeline/filters/preview", json=draft)
    assert response.status_code == 200, response.text
    response = await client.post("/api/pipeline/filters/apply", json={**draft, "preview_digest": response.json()["preview_digest"]})
    assert response.status_code == 200, response.text
    return response.json()


async def flush_apps(factory):
    for _ in range(20):
        await application_batch(factory)
        async with factory() as session:
            pending = (await session.execute(select(func.count()).select_from(FilterApplication).where(
                FilterApplication.status.in_(("queued", "running"))))).scalar_one()
        if not pending:
            return
    raise AssertionError("application did not complete")


async def test_multiple_filters_sticky_marks_remove_and_reapply(db, client):
    entry_id = await seed(db)
    marks = []
    for name in ("First", "Second"):
        response = await client.post("/api/pipeline/marks", json={"name": name})
        assert response.status_code == 200
        marks.append(response.json()["id"])
    a = await save_rule(client, marks[0], "A")
    await save_rule(client, marks[0], "Same mark", model="minilm")
    await save_rule(client, marks[1], "B")
    await flush_apps(db)
    state = (await client.get(f"/api/pipeline/board-state?entry_ids={entry_id}")).json()["entries"][str(entry_id)]
    assert state["stage"] == "filtered" and len(state["selection"]["marks"]) == 2
    assert len(state["selection"]["marks"][0]["sources"]) == 2
    filtered_board = await client.get(f"/pipeline?mark={marks[0]}&selection_filter={a['id']}")
    assert filtered_board.status_code == 200 and "QA sample" in filtered_board.text
    rules = (await client.get("/api/pipeline/filters")).json()["filters"]
    await save_rule(client, marks[0], "A", threshold=99, item=next(rule for rule in rules if rule["id"] == a["id"]))
    await flush_apps(db)
    assert len((await client.get(f"/api/pipeline/{entry_id}/marks")).json()["marks"]) == 2
    for mark_id in marks:
        assert (await client.post(f"/api/pipeline/{entry_id}/marks/{mark_id}/remove", json={})).status_code == 200
    for _ in range(2):
        state = (await client.get(f"/api/pipeline/board-state?entry_ids={entry_id}")).json()["entries"][str(entry_id)]
        assert state["stage"] == "sorted" and not state["selection"]["marks"]
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        job = (await session.execute(select(TaxonomyClassification).where(TaxonomyClassification.pipeline_entry_id == entry_id,
            TaxonomyClassification.model_key == "minilm"))).scalar_one()
        await evaluate_completed_job(session, entry, post, job)
        await session.commit()
    assert not (await client.get(f"/api/pipeline/{entry_id}/marks")).json()["marks"]
    # Явное применение может назначить снятый признак повторно; обычное чтение — нет.
    rules = (await client.get("/api/pipeline/filters")).json()["filters"]
    await save_rule(client, marks[0], "Same mark", model="minilm", item=next(rule for rule in rules if rule["name"] == "Same mark"))
    await flush_apps(db)
    assert len((await client.get(f"/api/pipeline/{entry_id}/marks")).json()["marks"]) == 1
    async with db() as session:
        assert (await session.execute(select(func.count()).select_from(PostFilterMark).where(
            PostFilterMark.entry_id == entry_id))).scalar_one() == 2


async def test_backfill_resume_and_inference_hook(db, client):
    entry_id = await seed(db, legacy=True)
    mark_id = (await client.post("/api/pipeline/marks", json={"name": "Legacy"})).json()["id"]
    await save_rule(client, mark_id)
    await application_batch(db)
    await flush_apps(db)
    async with db() as session:
        job = (await session.execute(select(TaxonomyClassification).where(
            TaxonomyClassification.pipeline_entry_id == entry_id, TaxonomyClassification.model_key == "tfidf"))).scalar_one()
        assert job.status == "queued"
        job.status = "running"; job_id = job.id
        await session.commit()
    await finish_job(db, job_id, {"taxonomy_version": taxonomy_catalog()["version"],
        "scores": {"tool_description": .9}, "top_3": [], "review_status": "scored"}, 42)
    state = (await client.get(f"/api/pipeline/board-state?entry_ids={entry_id}")).json()["entries"][str(entry_id)]
    assert state["stage"] == "filtered" and state["can_mark_source"]
    assert (await client.post(f"/api/pipeline/{entry_id}/mark-source", json={})).status_code == 200
    assert (await client.post(f"/api/pipeline/{entry_id}/marks/{mark_id}/remove", json={})).json()["stage"] == "marking"


async def test_edits_unknown_auth_dictionary_and_pages(db, client):
    entry_id = await seed(db)
    mark_id = (await client.post("/api/pipeline/marks", json={"name": "Stable"})).json()["id"]
    await save_rule(client, mark_id)
    await flush_apps(db)
    assert (await client.put(f"/api/pipeline/marks/{mark_id}", json={"name": "Renamed"})).status_code == 200
    assert (await client.put(f"/api/pipeline/marks/{mark_id}", json={"name": "Renamed", "archived": True})).status_code == 409
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        post.text = "Changed"
        await invalidate_if_edited(session, post.id, post.text)
        await session.commit()
    state = (await client.get(f"/api/pipeline/board-state?entry_ids={entry_id}")).json()["entries"][str(entry_id)]
    assert state["stage"] == "filtered" and not state["can_mark_source"]
    assert state["selection"]["checks"][0]["outcome"] == "unknown"
    assert state["selection"]["marks"][0]["name"] == "Renamed"
    assert (await client.post(f"/api/pipeline/{entry_id}/mark-source", json={})).status_code == 409
    for route in ("/pipeline", f"/pipeline/{entry_id}", "/marks", "/pipeline/filters", f"/pipeline?mark={mark_id}&selection_filter=1"):
        assert (await client.get(route)).status_code == 200
    assert (await client.get("/api/pipeline/filters", auth=None)).status_code == 401
    assert (await client.post("/api/pipeline/marks", json={"name": "Rejected"}, headers={"Origin": "https://other.invalid"})).status_code == 403
