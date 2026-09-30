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
from app.content.selection_filters import (
    application_batch,
    evaluate_completed_job,
    load_states,
    remove_mark,
)
from app.content.selection_rules import taxonomy_catalog
from app.models import (
    FilterApplication,
    FilterEvaluation,
    FilterMark,
    FilterMarkEvent,
    PipelineEntry,
    PostFilterMark,
    SelectionFilter,
    SelectionFilterVersion,
    TaxonomyClassification,
    TaxonomyRun,
    TelegramChat,
    TelegramPost,
)
from app.taxonomy.jobs import invalidate_if_edited, text_sha256
from app.taxonomy.worker import finish_job
from app.web.main import create_app

DSN = os.environ.get("FILTER_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="FILTER_TEST_DSN for disposable PostgreSQL not provided"
)


@pytest_asyncio.fixture
async def db():
    assert make_url(DSN).database.startswith("publisher_filter_qa"), (
        "Never run against production DB"
    )
    engine = create_async_engine(DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "TRUNCATE filter_marks, selection_filters, telegram_chats, telegram_posts, pipeline_entries CASCADE"
            )
        )
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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qa.local",
            auth=("qa", "qa-password"),
        ) as connection:
            yield connection
    finally:
        # Settings кэшируются на процесс: авторизация QA не должна утекать в другие тесты.
        get_settings.cache_clear()


async def seed(factory, legacy=False, empty_first=0):
    async with factory() as session:
        chat = TelegramChat(
            peer_id=-1001234567890,
            title="QA chat",
            username="selection_qa",
            chat_type="channel",
            folder_name="MAX",
        )
        post = TelegramPost(
            chat_peer_id=chat.peer_id,
            message_id=1,
            text="QA sample",
            date=datetime.now(timezone.utc),
            is_deleted=False,
            raw={},
        )
        session.add_all([chat, post])
        await session.flush()
        for index in range(empty_first):
            media_post = TelegramPost(
                chat_peer_id=chat.peer_id,
                message_id=index + 2,
                text=None,
                date=datetime.now(timezone.utc),
                is_deleted=False,
                raw={},
            )
            session.add(media_post)
            await session.flush()
            session.add(
                PipelineEntry(
                    source_post_id=media_post.id,
                    stage="sorted",
                    status="taxonomy_sorted",
                )
            )
        await session.flush()
        entry = PipelineEntry(
            source_post_id=post.id, stage="sorted", status="taxonomy_sorted"
        )
        session.add(entry)
        await session.flush()
        scores = dict.fromkeys(
            (label["id"] for label in taxonomy_catalog()["labels"]), 0.8
        )
        result = (
            {"top_3": [], "review_status": "scored"}
            if legacy
            else {
                "taxonomy_version": taxonomy_catalog()["version"],
                "scores": scores,
                "top_3": [],
                "review_status": "scored",
            }
        )
        for key in ("tfidf", "minilm"):
            run = TaxonomyRun(
                pipeline_entry_id=entry.id,
                source_post_id=post.id,
                model_key=key,
                status="complete",
                text_sha256=text_sha256(post.text),
                result=result,
            )
            session.add(run)
            await session.flush()
            session.add(
                TaxonomyClassification(
                    pipeline_entry_id=entry.id,
                    source_post_id=post.id,
                    model_key=key,
                    current_run_id=run.id,
                    status="complete",
                    text_sha256=text_sha256(post.text),
                    result=result,
                )
            )
        await session.commit()
        return entry.id


async def save_rule(
    client, mark_id, name="Rule", model="tfidf", threshold=60, item=None, enabled=True
):
    draft = {
        "name": name,
        "model_key": model,
        "mark_id": mark_id,
        "enabled": enabled,
        "expression": {
            "op": "condition",
            "label_id": "tool_description",
            "compare": "gte",
            "threshold": threshold,
        },
    }
    if item:
        draft.update(filter_id=item["id"], base_version_id=item["base_version_id"])
    response = await client.post("/api/pipeline/filters/preview", json=draft)
    assert response.status_code == 200, response.text
    response = await client.post(
        "/api/pipeline/filters/apply",
        json={**draft, "preview_digest": response.json()["preview_digest"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def flush_apps(factory):
    for _ in range(20):
        await application_batch(factory)
        async with factory() as session:
            pending = (
                await session.execute(
                    select(func.count())
                    .select_from(FilterApplication)
                    .where(FilterApplication.status.in_(("queued", "running")))
                )
            ).scalar_one()
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
    state = (
        await client.get(f"/api/pipeline/board-state?entry_ids={entry_id}")
    ).json()["entries"][str(entry_id)]
    assert state["stage"] == "filtered" and len(state["selection"]["marks"]) == 2
    assert len(state["selection"]["marks"][0]["sources"]) == 2
    filtered_board = await client.get(
        f"/pipeline?mark={marks[0]}&selection_filter={a['id']}"
    )
    assert filtered_board.status_code == 200 and "QA sample" in filtered_board.text
    rules = (await client.get("/api/pipeline/filters")).json()["filters"]
    await save_rule(
        client,
        marks[0],
        "A",
        threshold=99,
        item=next(rule for rule in rules if rule["id"] == a["id"]),
    )
    await flush_apps(db)
    assert (
        len((await client.get(f"/api/pipeline/{entry_id}/marks")).json()["marks"]) == 2
    )
    for mark_id in marks:
        assert (
            await client.post(
                f"/api/pipeline/{entry_id}/marks/{mark_id}/remove", json={}
            )
        ).status_code == 200
    for _ in range(2):
        state = (
            await client.get(f"/api/pipeline/board-state?entry_ids={entry_id}")
        ).json()["entries"][str(entry_id)]
        assert state["stage"] == "sorted" and not state["selection"]["marks"]
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        job = (
            await session.execute(
                select(TaxonomyClassification).where(
                    TaxonomyClassification.pipeline_entry_id == entry_id,
                    TaxonomyClassification.model_key == "minilm",
                )
            )
        ).scalar_one()
        await evaluate_completed_job(session, entry, post, job)
        await session.commit()
    assert not (await client.get(f"/api/pipeline/{entry_id}/marks")).json()["marks"]
    # Явное применение может назначить снятый признак повторно; обычное чтение — нет.
    rules = (await client.get("/api/pipeline/filters")).json()["filters"]
    await save_rule(
        client,
        marks[0],
        "Same mark",
        model="minilm",
        item=next(rule for rule in rules if rule["name"] == "Same mark"),
    )
    await flush_apps(db)
    assert (
        len((await client.get(f"/api/pipeline/{entry_id}/marks")).json()["marks"]) == 1
    )
    async with db() as session:
        assert (
            await session.execute(
                select(func.count())
                .select_from(PostFilterMark)
                .where(PostFilterMark.entry_id == entry_id)
            )
        ).scalar_one() == 2


async def test_backfill_resume_and_inference_hook(db, client):
    entry_id = await seed(db, legacy=True)
    mark_id = (
        await client.post("/api/pipeline/marks", json={"name": "Legacy"})
    ).json()["id"]
    await save_rule(client, mark_id)
    await application_batch(db)
    await flush_apps(db)
    async with db() as session:
        job = (
            await session.execute(
                select(TaxonomyClassification).where(
                    TaxonomyClassification.pipeline_entry_id == entry_id,
                    TaxonomyClassification.model_key == "tfidf",
                )
            )
        ).scalar_one()
        assert job.status == "queued"
        job.status = "running"
        job_id = job.id
        await session.commit()
    await finish_job(
        db,
        job_id,
        {
            "taxonomy_version": taxonomy_catalog()["version"],
            "scores": {"tool_description": 0.9},
            "top_3": [],
            "review_status": "scored",
        },
        42,
    )
    state = (
        await client.get(f"/api/pipeline/board-state?entry_ids={entry_id}")
    ).json()["entries"][str(entry_id)]
    assert state["stage"] == "filtered" and state["can_mark_source"]
    assert (
        await client.post(f"/api/pipeline/{entry_id}/mark-source", json={})
    ).status_code == 200
    assert (
        await client.post(f"/api/pipeline/{entry_id}/marks/{mark_id}/remove", json={})
    ).json()["stage"] == "marking"


async def test_edits_unknown_auth_dictionary_and_pages(db, client):
    entry_id = await seed(db)
    mark_id = (
        await client.post("/api/pipeline/marks", json={"name": "Stable"})
    ).json()["id"]
    await save_rule(client, mark_id)
    await flush_apps(db)
    assert (
        await client.put(f"/api/pipeline/marks/{mark_id}", json={"name": "Renamed"})
    ).status_code == 200
    assert (
        await client.put(
            f"/api/pipeline/marks/{mark_id}", json={"name": "Renamed", "archived": True}
        )
    ).status_code == 409
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        post.text = "Changed"
        await invalidate_if_edited(session, post.id, post.text)
        await session.commit()
    state = (
        await client.get(f"/api/pipeline/board-state?entry_ids={entry_id}")
    ).json()["entries"][str(entry_id)]
    assert state["stage"] == "filtered" and not state["can_mark_source"]
    assert state["selection"]["checks"][0]["outcome"] == "unknown"
    assert state["selection"]["marks"][0]["name"] == "Renamed"
    assert (
        await client.post(f"/api/pipeline/{entry_id}/mark-source", json={})
    ).status_code == 409
    for route in (
        "/pipeline",
        f"/pipeline/{entry_id}",
        "/marks",
        "/pipeline/filters",
        f"/pipeline?mark={mark_id}&selection_filter=1",
    ):
        assert (await client.get(route)).status_code == 200
    assert (await client.get("/api/pipeline/filters", auth=None)).status_code == 401
    assert (
        await client.post(
            "/api/pipeline/marks",
            json={"name": "Rejected"},
            headers={"Origin": "https://other.invalid"},
        )
    ).status_code == 403


async def test_backfill_waits_for_other_model_without_losing_cursor(db, client):
    entry_id = await seed(db)
    async with db() as session:
        jobs = {
            job.model_key: job
            for job in (
                await session.execute(
                    select(TaxonomyClassification).where(
                        TaxonomyClassification.pipeline_entry_id == entry_id
                    )
                )
            ).scalars()
        }
        jobs["tfidf"].status = "stale"
        jobs["tfidf"].result = None
        jobs["minilm"].status = "running"
        minilm_id = jobs["minilm"].id
        await session.commit()
    mark_id = (
        await client.post("/api/pipeline/marks", json={"name": "Deferred"})
    ).json()["id"]
    await save_rule(client, mark_id)
    await application_batch(db)
    async with db() as session:
        application = (await session.execute(select(FilterApplication))).scalar_one()
        assert application.processed == 0 and application.last_entry_id == 0
    result = {
        "taxonomy_version": taxonomy_catalog()["version"],
        "scores": {"tool_description": 0.9},
        "top_3": [],
        "review_status": "scored",
    }
    await finish_job(db, minilm_id, result, 42)
    await flush_apps(db)
    async with db() as session:
        job = (
            await session.execute(
                select(TaxonomyClassification).where(
                    TaxonomyClassification.pipeline_entry_id == entry_id,
                    TaxonomyClassification.model_key == "tfidf",
                )
            )
        ).scalar_one()
        assert job.status == "queued"
        job.status = "running"
        tfidf_id = job.id
        await session.commit()
    await finish_job(db, tfidf_id, result, 42)
    assert (await client.get(f"/api/pipeline/{entry_id}/marks")).json()["marks"]


async def test_manual_dictionary_label_with_two_model_conditions(db, client):
    entry_id = await seed(db)
    mark_id = (
        await client.post(
            "/api/pipeline/marks", json={"name": "Мой лейбл", "color": "#ff8800"}
        )
    ).json()["id"]
    for model in ("tfidf", "minilm"):
        draft = {
            "name": model,
            "model_key": model,
            "mark_id": mark_id,
            "expression": {
                "op": "and",
                "children": [
                    {
                        "op": "condition",
                        "label_id": label["id"],
                        "compare": "gte",
                        "threshold": 60,
                    }
                    for label in taxonomy_catalog()["labels"]
                ],
            },
        }
        response = await client.post("/api/pipeline/filters/preview", json=draft)
        assert response.status_code == 200, response.text
        data = response.json()
        assert (
            data["matched"] == 1 and len(data["examples"][0]["trace"]["children"]) == 38
        )
        assert "assigned" not in data["examples"][0]["trace"]
        assert len((await client.get("/api/pipeline/marks")).json()["marks"]) == 1
        response = await client.post(
            "/api/pipeline/filters/apply",
            json={**draft, "preview_digest": data["preview_digest"]},
        )
        assert response.status_code == 200, response.text
        await flush_apps(db)
    marks = (await client.get(f"/api/pipeline/{entry_id}/marks")).json()["marks"]
    assert (
        len(marks) == 1
        and marks[0]["name"] == "Мой лейбл"
        and marks[0]["label_id"] is None
    )
    assert len(marks[0]["sources"]) == 2
    assert all(source["assigned"] is None for source in marks[0]["sources"])
    assert (
        await client.put(
            f"/api/pipeline/marks/{mark_id}", json={"name": "Переименовал"}
        )
    ).status_code == 200
    assert (await client.get(f"/api/pipeline/{entry_id}/marks")).json()["marks"][0][
        "name"
    ] == "Переименовал"
    async with db() as session:
        assert all(
            version.assigned_label_id is None
            for version in (
                await session.execute(select(SelectionFilterVersion))
            ).scalars()
        )


async def test_dictionary_target_required_and_model_label_target_rejected(db, client):
    await seed(db)
    expression = {
        "op": "condition",
        "label_id": "is_job_vacancy",
        "compare": "gte",
        "threshold": 60,
    }
    for draft in (
        {"name": "Missing", "expression": expression},
        {"name": "Auto", "assigned_label_id": "is_ad", "expression": expression},
    ):
        assert (
            await client.post("/api/pipeline/filters/preview", json=draft)
        ).status_code == 422
    assert (
        await client.post(
            "/api/pipeline/filters/preview",
            json={"name": "Invalid", "mark_id": 123456, "expression": expression},
        )
    ).status_code == 409
    assert not (await client.get("/api/pipeline/marks")).json()["marks"]


async def test_preview_keeps_scored_post_after_many_media_cards(db, client):
    entry_id = await seed(db, empty_first=13)
    mark_id = (
        await client.post("/api/pipeline/marks", json={"name": "Preview label"})
    ).json()["id"]
    draft = {
        "name": "Scored example",
        "mark_id": mark_id,
        "expression": {
            "op": "condition",
            "label_id": "society",
            "compare": "gte",
            "threshold": 60,
        },
    }
    response = await client.post("/api/pipeline/filters/preview", json=draft)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 14 and data["unknown"] == 13 and data["matched"] == 1
    assert len(data["examples"]) == 12
    assert data["examples"][0]["entry_id"] == entry_id
    assert data["examples"][0]["trace"]["score"] == 0.8


async def test_current_pages_and_dashboard_match_database(db, client):
    entry_id = await seed(db)
    for path in (
        "/dashboard",
        "/pipeline?q=&mark=&selection_filter=&order=desc&limit=30",
        f"/pipeline/{entry_id}",
    ):
        response = await client.get(path)
        assert response.status_code == 200, response.text
        for retired in (
            "Материалы по ссылкам",
            "Классификация и публикация",
            "История рерайтов",
            "Признаки и фильтры",
        ):
            assert retired not in response.text
    dashboard = (await client.get("/api/dashboard")).json()
    assert dashboard["total"] == 1 and dashboard["stages"] == {"sorted": 1}
    assert sum(job["count"] for job in dashboard["jobs"]) == 2
    assert len(dashboard["runs"]) == 2
    assert (
        len(
            (await client.get(f"/api/pipeline/{entry_id}/taxonomy-runs")).json()[
                "catalog"
            ]["labels"]
        )
        == 38
    )
    async with db() as session:
        other_chat = TelegramChat(
            peer_id=12345, chat_type="group", folder_name="PRIVATE"
        )
        other_post = TelegramPost(
            chat_peer_id=other_chat.peer_id,
            message_id=1,
            text="Not MAX",
            is_deleted=False,
            raw={},
        )
        session.add_all([other_chat, other_post])
        await session.flush()
        other_entry = PipelineEntry(
            source_post_id=other_post.id, stage="received", status="received"
        )
        session.add(other_entry)
        await session.commit()
        other_id = other_entry.id
    assert (await client.get(f"/pipeline/{other_id}")).status_code == 404
    assert (
        str(other_id)
        not in (
            await client.get(f"/api/pipeline/board-state?entry_ids={other_id}")
        ).json()["entries"]
    )
    assert (await client.get("/api/dashboard")).json()["total"] == 1
