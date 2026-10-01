from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
from scipy.sparse import csr_matrix

from app.taxonomy.inference import TaxonomyModel
from app.taxonomy.jobs import enqueue, invalidate_if_edited, mark_without_text, public_job, public_run, text_sha256
from app.models import TaxonomyRun


class FakeVectorizer:
    def transform(self, values):
        assert len(values) == 1
        return csr_matrix([[1.0]])


class FakeModel:
    def __init__(self, score):
        self.score = score

    def predict_proba(self, _features):
        return np.array([[1 - self.score, self.score]])


class FakeComplexity:
    def predict(self, _features):
        return np.array([3.7])


def test_inference_keeps_independent_subcategory_scores_and_all_required_features():
    categories = [
        {"id": "first", "name": "Первая", "subcategories": [{"id": "first.child", "name": "Подтема"}]},
        {"id": "second", "name": "Вторая", "subcategories": [{"id": "second.child", "name": "Подтема"}]},
        {"id": "third", "name": "Третья", "subcategories": [{"id": "third.child", "name": "Подтема"}]},
        {"id": "fourth", "name": "Четвёртая", "subcategories": []},
    ]
    features = ["is_ad", "is_event_related", "is_event_invitation", "is_job_vacancy", "is_scientific_paper", "is_joke"]
    scores = {"first": .8, "first.child": .2, "second": .6, "second.child": .7,
              "third": .4, "third.child": .1, "fourth": .3, **dict.fromkeys(features, .25)}
    model = TaxonomyModel.__new__(TaxonomyModel)
    model.taxonomy = {"categories": categories, "binary_features": features}
    model.bundle = {"word": FakeVectorizer(), "char": FakeVectorizer(),
                    "label_names": list(scores), "models": {name: FakeModel(score) for name, score in scores.items()},
                    "complexity": FakeComplexity()}

    result = model.classify("пример текста")

    assert [row["id"] for row in result["top_3"]] == ["first", "second", "third"]
    assert result["top_3"][0]["subcategories"][0]["score"] == .2
    assert result["top_3"][1]["subcategories"][0]["score"] == .7
    assert len(result["features"]) == 6
    assert result["technical_complexity"] == 4
    assert result["score_kind"] == "uncalibrated_model_score"


def test_edited_text_hides_outdated_result():
    job = SimpleNamespace(
        model_key="tfidf",
        text_sha256=text_sha256("старый текст"), status="complete", result={"top_3": [1]},
        error=None, model_version="v1", elapsed_ms=50,
        finished_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
    )
    assert public_job(job, "новый текст")["status"] == "stale"
    assert public_job(job, "новый текст")["result"] is None
    assert public_job(job, "старый текст")["result"] == {"top_3": [1]}


async def test_edited_post_clears_saved_source_marking():
    job = SimpleNamespace(text_sha256=text_sha256("старый текст"), current_run_id=None,
                          status="complete", result={"top_3": []}, error=None, updated_at=None)
    entry = SimpleNamespace(id=42, marked_text="старый текст с источником", marked_source_url="https://t.me/c/1/2",
                            marked_text_sha256=text_sha256("старый текст"), marked_at=datetime.now(timezone.utc),
                            stage="marking", status="marked", classification_id=None, last_operation_at=None, auto_enabled=False)
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        SimpleNamespace(scalar_one_or_none=lambda: entry),
        SimpleNamespace(scalars=lambda: [job]),
        SimpleNamespace(scalar_one_or_none=lambda: None),
    ])

    assert await invalidate_if_edited(session, 42, "новый текст") is True
    assert entry.marked_text is None
    assert entry.marked_source_url is None
    assert entry.marked_text_sha256 is None
    assert entry.stage == "received"
    assert job.status == "stale"


async def test_empty_media_is_sorted_without_queuing_either_model():
    entry = SimpleNamespace(id=42, stage="received", status="received", last_operation_at=None, marked_text_sha256=None)
    post = SimpleNamespace(id=42, text=" \n ", media_type="MessageMediaPhoto", media_path=None, is_deleted=False)
    chat = SimpleNamespace(folder_name="MAX")
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        SimpleNamespace(first=lambda: (entry, post, chat)),
        SimpleNamespace(scalar_one_or_none=lambda: None),
        SimpleNamespace(scalar_one_or_none=lambda: None),
    ])
    session.flush = AsyncMock()

    job = await enqueue(session, 42, "minilm")

    assert job.model_key == "media"
    assert job.status == "media_only"
    assert job.result["category"] == "only_media"
    assert job.attempts is None or job.attempts == 0
    assert entry.stage == "sorted"
    assert entry.status == "taxonomy_media_only"
    assert session.execute.await_count == 3


async def test_empty_without_media_is_not_mislabelled_as_media():
    entry = SimpleNamespace(id=43, stage="received", status="received", last_operation_at=None, marked_text_sha256=None)
    post = SimpleNamespace(id=43, text=None, media_type=None, media_path=None)
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    session.flush = AsyncMock()

    job = await mark_without_text(session, entry, post)

    assert job.status == "empty"
    assert job.result["category"] == "empty"
    assert entry.stage == "sorted"


async def test_repeated_classification_creates_another_persisted_run():
    entry = SimpleNamespace(id=57, stage="sorted", status="taxonomy_sorted", last_operation_at=None, auto_enabled=False)
    post = SimpleNamespace(id=57, text="текст поста", is_deleted=False)
    chat = SimpleNamespace(folder_name="MAX")
    existing = SimpleNamespace(
        id=70, pipeline_entry_id=57, source_post_id=57, model_key="minilm",
        text_sha256=text_sha256(post.text), status="complete", current_run_id=33,
        result={"top_3": []}, attempts=1,
    )
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        SimpleNamespace(first=lambda: (entry, post, chat)),
        SimpleNamespace(scalar_one_or_none=lambda: existing),
        SimpleNamespace(scalar_one_or_none=lambda: None),
    ])
    session.flush = AsyncMock()

    job = await enqueue(session, 57, "minilm")

    new_run = next(value for call in session.add.call_args_list
                   if isinstance(value := call.args[0], TaxonomyRun))
    assert new_run.model_key == "minilm"
    assert new_run.status == "queued"
    assert existing.status == "queued"
    assert existing.attempts == 1
    assert job is existing


def test_run_history_marks_results_for_previous_text():
    run = SimpleNamespace(
        id=45, model_key="tfidf", model_version="v1", status="complete",
        result={"top_3": []}, error=None, elapsed_ms=12, origin="run",
        text_sha256=text_sha256("старый текст"),
        queued_at=datetime(2026, 9, 29, tzinfo=timezone.utc), started_at=None, finished_at=None,
    )
    assert public_run(run, "старый текст")["is_current_text"] is True
    assert public_run(run, "новый текст")["is_current_text"] is False
    assert public_run(run, "новый текст")["result"] == {"top_3": []}
