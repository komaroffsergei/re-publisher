"""Настоящий PostgreSQL, одноразовые QA-данные; никакой отправки в Telegram."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func
from telethon.errors import FloodWaitError

import test_selection_integration as shared
from test_selection_integration import seed, save_rule, flush_apps

db = shared.db
client = shared.client
from app.config import get_settings
from app.folders import FolderChat
from app.live_collector import register_chats, reconcile_chat
from app.runtime_status import heartbeat
from app.pipeline_coordinator import coordinator_batch
from app.taxonomy.jobs import invalidate_if_edited
from app.taxonomy.worker import finish_job
from app.content.selection_rules import taxonomy_catalog
from app.sync import mark_messages_deleted
from app.models import (
    PipelineEntry,
    TelegramPost,
    TelegramSyncState,
    ServiceRuntime,
    TaxonomyClassification,
    TaxonomyRun,
    SelectionFilter,
    PostFilterMark,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("FILTER_TEST_DSN"), reason="Disposable PostgreSQL required"
)


async def configure(db, client, two=False, threshold=60):
    entry_id = await seed(db)
    mark = (await client.post("/api/pipeline/marks", json={"name": "QA label"})).json()[
        "id"
    ]
    await save_rule(client, mark, threshold=threshold)
    if two:
        await save_rule(
            client, mark, name="Second model", model="minilm", threshold=threshold
        )
    await flush_apps(db)
    return entry_id


async def state(client, entry_id):
    return (await client.get(f"/api/pipeline/board-state?entry_ids={entry_id}")).json()[
        "entries"
    ][str(entry_id)]


async def enable(db, entry_id, manual=False):
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        entry.auto_enabled = True
        entry.auto_state = "pending"
        entry.auto_manual_mark = manual
        entry.auto_retry_at = None
        await session.commit()


@pytest.mark.parametrize("two", [False, True])
async def test_complete_pipeline_idempotent_original_and_no_extra_runs(db, client, two):
    entry_id = await configure(db, client, two)
    for _ in range(5):
        await coordinator_batch(db, get_settings())
    data = await state(client, entry_id)
    assert (
        data["stage"] == "ready" and data["ready_at"] and data["auto_state"] == "done"
    )
    assert data["marked_text"].count("Источник:") == 1 and data[
        "marked_text"
    ].startswith("QA sample")
    async with db() as session:
        post = await session.get(
            TelegramPost, (await session.get(PipelineEntry, entry_id)).source_post_id
        )
        assert post.text == "QA sample"
        assert (
            await session.scalar(select(func.count()).select_from(TaxonomyRun))
        ) == 2
    response = await client.get("/api/pipeline/board-fragment?mark=1&limit=15")
    assert response.status_code == 200 and response.text.count("data-column=") == 5
    assert (
        await client.get("/api/pipeline/board-fragment", auth=None)
    ).status_code == 401


async def test_old_received_and_no_match_are_not_advanced(db, client):
    entry_id = await configure(db, client, threshold=99)
    await enable(db, entry_id)
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        old = TelegramPost(
            chat_peer_id=post.chat_peer_id,
            message_id=2,
            text="Old archive",
            raw={},
            is_deleted=False,
        )
        session.add(old)
        await session.flush()
        old_entry = PipelineEntry(
            source_post_id=old.id, stage="received", status="received"
        )
        session.add(old_entry)
        await session.commit()
        old_id = old_entry.id
    await coordinator_batch(db, get_settings())
    assert (await state(client, entry_id))["stage"] == "sorted"
    data = await state(client, old_id)
    assert (
        data["stage"] == "received"
        and data["taxonomies"] == {}
        and data["auto_state"] is None
    )


@pytest.mark.parametrize("media_status", ["failed", "missing", "skipped_too_large"])
async def test_media_blocks_and_retry_is_protected(db, client, media_status):
    entry_id = await configure(db, client)
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        post.media_type = "MessageMediaPhoto"
        post.media_download_status = media_status
        await session.commit()
    await coordinator_batch(db, get_settings())
    await coordinator_batch(db, get_settings())
    data = await state(client, entry_id)
    assert (
        data["stage"] == "marking"
        and data["auto_state"] == "blocked"
        and media_status in data["auto_error"]
    )
    assert (
        await client.post(
            f"/api/pipeline/{entry_id}/automation/retry", json={}, auth=None
        )
    ).status_code == 401
    assert (
        await client.post(
            f"/api/pipeline/{entry_id}/automation/retry",
            json={},
            headers={"Origin": "https://other.invalid"},
        )
    ).status_code == 403
    assert (
        await client.post(f"/api/pipeline/{entry_id}/automation/retry", json={})
    ).status_code == 200
    assert (
        await client.post(f"/api/pipeline/{entry_id}/automation/retry", json={})
    ).status_code == 409
    async with db() as session:
        post = await session.get(
            TelegramPost, (await session.get(PipelineEntry, entry_id)).source_post_id
        )
        assert post.media_download_status == (
            "pending" if media_status in {"failed", "missing"} else media_status
        )


async def test_manual_mark_without_active_match_can_be_ready(db, client):
    entry_id = await configure(db, client)
    assert (
        await client.post(f"/api/pipeline/{entry_id}/mark-source", json={})
    ).status_code == 200
    async with db() as session:
        for rule in (await session.execute(select(SelectionFilter))).scalars():
            rule.enabled = False
        for mark in (await session.execute(select(PostFilterMark))).scalars():
            mark.active = False
        await session.commit()
    await coordinator_batch(db, get_settings())
    assert (await state(client, entry_id))["stage"] == "ready"


async def test_sticky_label_without_current_match_not_ready(db, client):
    entry_id = await configure(db, client)
    async with db() as session:
        job = (
            await session.execute(
                select(TaxonomyClassification).where(
                    TaxonomyClassification.model_key == "tfidf"
                )
            )
        ).scalar_one()
        job.result = {**job.result, "scores": dict.fromkeys(job.result["scores"], 0.1)}
        await session.commit()
    await coordinator_batch(db, get_settings())
    data = await state(client, entry_id)
    assert (
        data["stage"] == "filtered"
        and data["selection"]["marks"]
        and not data["marked_text"]
    )


async def test_edit_revokes_ready_marks_survive_and_delete_stops(db, client):
    entry_id = await configure(db, client)
    await coordinator_batch(db, get_settings())
    await coordinator_batch(db, get_settings())
    assert (await state(client, entry_id))["stage"] == "ready"
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        post.text = "Edited"
        await invalidate_if_edited(session, post.id, post.text)
        await session.commit()
    data = await state(client, entry_id)
    assert (
        data["ready_at"] is None
        and data["marked_text"] is None
        and data["selection"]["marks"]
    )
    await coordinator_batch(db, get_settings())
    assert (await state(client, entry_id))["taxonomies"]["tfidf"]["status"] == "queued"
    async with db() as session:
        post = await session.get(
            TelegramPost, (await session.get(PipelineEntry, entry_id)).source_post_id
        )
        await mark_messages_deleted(session, post.chat_peer_id, [post.message_id])
        await session.commit()
    data = await state(client, entry_id)
    assert (
        data["deleted"] and data["ready_at"] is None and data["auto_state"] == "stopped"
    )


async def test_models_sequential_wait_for_both_before_marking(db, client):
    entry_id = await configure(db, client, two=True)
    async with db() as session:
        for job in (await session.execute(select(TaxonomyClassification))).scalars():
            job.status = "stale"
            job.result = None
        await session.commit()
    for _ in range(2):
        await coordinator_batch(db, get_settings())
        async with db() as session:
            jobs = list(
                (await session.execute(select(TaxonomyClassification))).scalars()
            )
            queued = [job for job in jobs if job.status == "queued"]
            assert len(queued) == 1
            job = queued[0]
            job.status = "running"
            job_id = job.id
            await session.commit()
        await finish_job(
            db,
            job_id,
            {
                "taxonomy_version": taxonomy_catalog()["version"],
                "scores": dict.fromkeys(
                    (l["id"] for l in taxonomy_catalog()["labels"]), 0.8
                ),
                "top_3": [],
                "review_status": "scored",
            },
            12,
        )
        await enable(db, entry_id)
    await coordinator_batch(db, get_settings())
    await coordinator_batch(db, get_settings())
    assert (await state(client, entry_id))["stage"] == "ready"


@pytest.mark.parametrize("text,media_status", [(None, "missing"), ("", "downloaded")])
async def test_textless_never_enters_semantic_queue(db, client, text, media_status):
    entry_id = await seed(db)
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        post.text = text
        post.media_type = "photo" if media_status == "downloaded" else None
        entry.stage = "received"
        entry.auto_enabled = True
        await session.commit()
    mark = (await client.post("/api/pipeline/marks", json={"name": "Rule"})).json()[
        "id"
    ]
    await save_rule(client, mark)
    await coordinator_batch(db, get_settings())
    data = await state(client, entry_id)
    assert data["stage"] == "sorted" and data["taxonomies"]["media"]["status"] in {
        "empty",
        "media_only",
    }
    assert not data["active"]


async def test_boundary_restart_new_chat_and_realtime_cursor_independence(db, client):
    chat = FolderChat(-1001234567890, "Live", "live_qa", "channel", None, {})
    await register_chats(db, {chat.peer_id: chat})
    async with db() as session:
        since = (await session.get(TelegramSyncState, chat.peer_id)).live_since
    await heartbeat(db, "collector", success=True)
    await register_chats(db, {chat.peer_id: chat})
    async with db() as session:
        sync = await session.get(TelegramSyncState, chat.peer_id)
        assert sync.live_since == since
        sync.last_message_id = 1000  # раннее realtime-событие не двигает сверку
        await session.commit()
    seen = []

    class FakeClient:
        async def get_messages(self, *a, **kw):
            return [SimpleNamespace(id=10)]

        async def iter_messages(self, *a, **kw):
            assert kw["min_id"] == 0
            for i, date in [
                (10, since + timedelta(seconds=1)),
                (9, since),
                (8, since - timedelta(seconds=1)),
            ]:
                yield SimpleNamespace(id=i, date=date)

    async def persist(_chat, message):
        seen.append(message.id)

    assert await reconcile_chat(FakeClient(), db, chat, persist)
    assert seen == [10, 9]
    async with db() as session:
        sync = await session.get(TelegramSyncState, chat.peer_id)
        assert sync.reconciled_message_id == 10 and sync.last_message_id == 1000
    other = FolderChat(-1001234567891, "New", "new_qa", "channel", None, {})
    await register_chats(db, {chat.peer_id: chat, other.peer_id: other})
    async with db() as session:
        assert (await session.get(TelegramSyncState, other.peer_id)).live_since > since
    await register_chats(db, {other.peer_id: other})
    async with db() as session:
        assert not (await session.get(TelegramSyncState, chat.peer_id)).live_member


async def test_failed_reconciliation_does_not_move_cursor_and_flood_wait(
    db, client, monkeypatch
):
    chat = FolderChat(-1001234567890, "Live", "live_qa", "channel", None, {})
    await register_chats(db, {chat.peer_id: chat})
    now = datetime.now(timezone.utc) + timedelta(seconds=1)

    class FakeClient:
        async def get_messages(self, *a, **kw):
            return [SimpleNamespace(id=3)]

        async def iter_messages(self, *a, **kw):
            yield SimpleNamespace(id=3, date=now)
            raise RuntimeError("Disconnected")

    assert not await reconcile_chat(FakeClient(), db, chat, AsyncMock())
    async with db() as session:
        assert (
            await session.get(TelegramSyncState, chat.peer_id)
        ).reconciled_message_id is None

    class Recover(FakeClient):
        count = 0

        async def iter_messages(self, *a, **kw):
            self.count += 1
            if self.count == 1:
                raise FloodWaitError(request=None, capture=1)
            yield SimpleNamespace(id=3, date=now)

    wait = AsyncMock()
    monkeypatch.setattr("app.live_collector.sleep_for_flood_wait", wait)
    assert await reconcile_chat(Recover(), db, chat, AsyncMock())
    wait.assert_awaited_once()


async def test_pulse_does_not_hide_failed_refresh(db, client):
    await heartbeat(db, "collector", error="Failed")
    await heartbeat(db, "collector")
    async with db() as session:
        assert (await session.get(ServiceRuntime, "collector")).error == "Failed"
    await heartbeat(db, "collector", success=True)
    async with db() as session:
        assert (await session.get(ServiceRuntime, "collector")).error is None


async def test_album_requires_every_known_file(db, client, tmp_path):
    entry_id = await configure(db, client)
    settings = get_settings()
    settings.media_dir = str(tmp_path)
    file = tmp_path / "image.jpg"
    file.write_bytes(b"QA attachment")
    async with db() as session:
        post = await session.get(
            TelegramPost, (await session.get(PipelineEntry, entry_id)).source_post_id
        )
        post.grouped_id = 88
        post.updated_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        child = TelegramPost(
            chat_peer_id=post.chat_peer_id,
            message_id=2,
            grouped_id=88,
            text=None,
            is_deleted=False,
            raw={},
            media_type="photo",
            media_download_status="failed",
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        session.add(child)
        await session.commit()
        child_id = child.id
    await coordinator_batch(db, settings)
    await coordinator_batch(db, settings)
    data = await state(client, entry_id)
    assert data["stage"] == "marking" and data["auto_state"] == "blocked"
    assert data["marked_source_url"].endswith("/1")
    async with db() as session:
        child = await session.get(TelegramPost, child_id)
        child.media_download_status = "downloaded"
        child.media_path = str(file)
        child.updated_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await session.commit()
    await enable(db, entry_id)
    await coordinator_batch(db, settings)
    assert (await state(client, entry_id))["stage"] == "ready"


async def test_missing_source_stops_before_marking(db, client):
    entry_id = await configure(db, client)
    from app.models import TelegramChat

    async with db() as session:
        post = await session.get(
            TelegramPost, (await session.get(PipelineEntry, entry_id)).source_post_id
        )
        chat = await session.get(TelegramChat, post.chat_peer_id)
        # Публичного username нет, peer ID относится к обычной группе.
        chat.username = None
        post.message_id = 0
        await session.commit()
    await coordinator_batch(db, get_settings())
    data = await state(client, entry_id)
    assert data["stage"] == "filtered" and data["auto_state"] == "blocked"
    assert "ссылки" in data["auto_error"]


async def test_failed_model_has_only_three_delayed_retries(db, client):
    entry_id = await configure(db, client)
    async with db() as session:
        job = (
            await session.execute(
                select(TaxonomyClassification).where(
                    TaxonomyClassification.model_key == "tfidf"
                )
            )
        ).scalar_one()
        job.status = "failed"
        job.error = "QA failure"
        job.result = None
        await session.commit()
    for index, delay in enumerate((10, 30, 120), 1):
        await coordinator_batch(db, get_settings())
        async with db() as session:
            entry = await session.get(PipelineEntry, entry_id)
            assert entry.auto_attempts == index and entry.auto_state == "pending"
            assert (
                delay - 2
                < (entry.auto_retry_at - datetime.now(timezone.utc)).total_seconds()
                <= delay
            )
            entry.auto_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
        await coordinator_batch(db, get_settings())
        async with db() as session:
            job = (
                await session.execute(
                    select(TaxonomyClassification).where(
                        TaxonomyClassification.model_key == "tfidf"
                    )
                )
            ).scalar_one()
            assert job.status == "queued"
            job.status = "running"
            job_id = job.id
            await session.commit()
        await finish_job(db, job_id, None, 5, "QA failure")
        async with db() as session:
            entry = await session.get(PipelineEntry, entry_id)
            entry.auto_retry_at = None
            await session.commit()
    await coordinator_batch(db, get_settings())
    data = await state(client, entry_id)
    assert data["auto_state"] == "stopped" and data["can_retry"]
    async with db() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TaxonomyRun))
        ) == 5


async def test_live_events_atomic_duplicate_old_edit_and_delete(
    db, client, monkeypatch
):
    from app.live_collector import run_live

    await seed(db)
    chat = FolderChat(
        -1001234567890,
        "Live",
        "live_qa",
        "channel",
        SimpleNamespace(id=-1001234567890),
        {},
    )
    settings = get_settings()
    settings.collector_process_saved_posts = settings.auto_publish = (
        settings.collect_comments
    ) = False
    observed = []

    class FakeClient:
        handlers = []

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        def is_connected(self):
            return True

        def add_event_handler(self, handler, event):
            self.handlers.append(handler)

        async def get_messages(self, *a, **kw):
            return []

        async def iter_messages(self, *a, **kw):
            if False:
                yield None

        async def run_until_disconnected(self):
            now = datetime.now(timezone.utc) + timedelta(seconds=1)

            def message(i, text, date=now):
                return SimpleNamespace(
                    id=i, date=date, message=text, media=None, edit_date=None
                )

            def event(msg):
                return SimpleNamespace(chat_id=chat.peer_id, message=msg)

            await self.handlers[0](
                event(message(10, "Too old", now - timedelta(hours=1)))
            )
            await self.handlers[0](event(message(11, "New post")))
            await self.handlers[0](event(message(11, "New post")))
            await self.handlers[1](
                event(message(10, "Unknown old edit", now - timedelta(hours=1)))
            )
            edited = message(11, "Edited new post")
            edited.edit_date = now
            await self.handlers[1](event(edited))
            await self.handlers[1](
                event(message(1, "Known old edit", now - timedelta(hours=1)))
            )
            async with db() as session:
                rows = (
                    await session.execute(
                        select(PipelineEntry, TelegramPost).join(
                            TelegramPost,
                            TelegramPost.id == PipelineEntry.source_post_id,
                        )
                    )
                ).all()
                assert len(rows) == 2
                for entry, post in rows:
                    observed.append((post.message_id, entry.auto_enabled, post.text))
            await self.handlers[2](
                SimpleNamespace(chat_id=chat.peer_id, deleted_ids=[11])
            )

    monkeypatch.setattr(
        "app.live_collector.create_telegram_client", lambda _: FakeClient()
    )
    monkeypatch.setattr(
        "app.live_collector.resolve_folder_chats", AsyncMock(return_value=[chat])
    )
    await run_live(settings)
    assert sorted(observed) == [
        (1, False, "Known old edit"),
        (11, True, "Edited new post"),
    ]
    async with db() as session:
        post = (
            await session.execute(
                select(TelegramPost).where(TelegramPost.message_id == 11)
            )
        ).scalar_one()
        assert post.is_deleted
        entry = (
            await session.execute(
                select(PipelineEntry).where(PipelineEntry.source_post_id == post.id)
            )
        ).scalar_one()
        assert entry.auto_state == "stopped"
