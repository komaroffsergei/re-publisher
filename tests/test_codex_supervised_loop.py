from __future__ import annotations

from types import SimpleNamespace

from joblib import load

from app.content.codex_supervised_loop import (
    AXIS_COLUMNS,
    is_media_only_unknown,
    match_percent,
    normalize_teacher_row,
    predict_bundle,
    teacher_input_row,
    train_bundle_from_records,
    training_goal_incomplete,
)
from app.content.yandex_genre_classifier import FALLBACK_GENRE, load_taxonomy


def test_codex_teacher_input_row_contains_taxonomy_context_fields():
    item = SimpleNamespace(
        source_post_id=123,
        id=456,
        translated_title=None,
        title="Новый AI-инструмент",
        main_text="Пост про запуск инструмента.",
        translated_summary=None,
        source_summary="Summary ссылки",
        source_url="https://example.com",
        source_domain="example.com",
    )
    processed = SimpleNamespace(
        language="ru",
        word_count=10,
        url_count=1,
        domains=["example.com"],
        has_code=False,
        has_github=False,
        has_arxiv=False,
        has_media=False,
    )

    row = teacher_input_row(item, processed, None)

    assert row["source_post_id"] == 123
    assert row["content_item_id"] == 456
    assert row["title"] == "Новый AI-инструмент"
    assert row["flags"]["domains"] == ["example.com"]


def test_media_only_item_bypasses_codex_teacher_path():
    item = SimpleNamespace(translated_title=None, title=None, main_text=None, translated_summary=None, source_summary=None)
    processed = SimpleNamespace(has_media=True, word_count=0)

    assert is_media_only_unknown(item, processed, None) is True


def test_codex_teacher_import_rejects_invalid_genre_to_fallback():
    taxonomy = load_taxonomy()

    row = normalize_teacher_row(
        {
            "source_post_id": 1,
            "content_item_id": 2,
            "genre_primary": "not_a_genre",
            "genre_secondary": ["tool_product"],
            "genre_confidence": 2,
            "difficulty_score": 9,
            "promo_score": -2,
            "opinion_score": 1,
            "event_score": 0,
            "needs_review": False,
        },
        taxonomy,
    )

    assert row is not None
    assert row["genre_primary"] == FALLBACK_GENRE
    assert row["needs_review"] is True
    assert row["genre_confidence"] == 1.0
    assert row["difficulty_score"] == 5
    assert row["promo_score"] == 0


def test_match_percent_exact_partial_and_failed():
    axes = {axis: 0 for axis in AXIS_COLUMNS}

    exact, exact_flags = match_percent("tool_product", [], axes, "tool_product", [], axes)
    partial, partial_flags = match_percent("tool_product", ["business_market"], axes, "business_market", [], axes)
    failed, failed_flags = match_percent("tool_product", [], axes, "technical_research", [], {axis: 5 for axis in AXIS_COLUMNS})

    assert exact == 100.0
    assert exact_flags == []
    assert partial == 70.0
    assert "genre_secondary_match" in partial_flags
    assert failed == 0.0
    assert "genre_mismatch" in failed_flags


def test_trainer_creates_tfidf_bundle_artifact(tmp_path):
    records = [
        {
            "source_post_id": 1,
            "text": "POST: релиз нового AI инструмента",
            "genre_primary": "news_announcement",
            "genre_secondary": ["tool_product", "education_guide"],
            "difficulty_score": 2,
            "promo_score": 1,
            "opinion_score": 0,
            "event_score": 0,
        },
        {
            "source_post_id": 2,
            "text": "POST: paper benchmark модель датасет",
            "genre_primary": "technical_research",
            "genre_secondary": ["tool_product"],
            "difficulty_score": 5,
            "promo_score": 0,
            "opinion_score": 0,
            "event_score": 0,
        },
    ]

    artifact, metadata = train_bundle_from_records(records, tmp_path, "test_run", 1)
    bundle = load(artifact)

    assert artifact.name == "tfidf_logreg.joblib"
    assert metadata["train_rows"] == 2
    assert bundle["kind"] == "codex_supervised_genre_axes_bundle_v1"
    assert set(bundle["axis_models"]) == set(AXIS_COLUMNS)
    assert bundle["genre_multilabel"] is not None
    assert metadata["multilabel_enabled"] is True
    prediction = predict_bundle(bundle, "POST: релиз нового AI инструмента")
    assert prediction["genre_secondary"]


def test_training_goal_requires_min_labels_and_target_match():
    assert training_goal_incomplete(label_count=2999, best_match_percent=95.0, min_labels=3000, target_match_percent=90.0) is True
    assert training_goal_incomplete(label_count=3000, best_match_percent=89.9, min_labels=3000, target_match_percent=90.0) is True
    assert training_goal_incomplete(label_count=3000, best_match_percent=90.0, min_labels=3000, target_match_percent=90.0) is False
