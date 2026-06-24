from __future__ import annotations

from types import SimpleNamespace

from app.content.yandex_genre_classifier import (
    FALLBACK_GENRE,
    build_genre_messages,
    is_media_only_unknown,
    load_taxonomy,
    media_only_result,
    parse_genre_response,
    usage_total_tokens,
)
from app.content.yandex_genre_trainer import map_legacy_label, model_version_values


def test_yandex_genre_taxonomy_has_minimum_genres_and_axes():
    taxonomy = load_taxonomy()

    assert taxonomy.version == "yandex_axes_v1"
    assert len(taxonomy.genres) >= 10
    assert FALLBACK_GENRE in taxonomy.genres
    assert {"difficulty_score", "promo_score", "opinion_score", "event_score"} <= set(taxonomy.axes)


def test_yandex_genre_parser_handles_clean_and_fenced_json():
    taxonomy = load_taxonomy()
    clean = """
    {
      "genre_primary": "tool_product",
      "genre_secondary": ["news_announcement"],
      "genre_confidence": 0.81,
      "difficulty_score": 3,
      "promo_score": 2,
      "opinion_score": 1,
      "event_score": 0,
      "needs_review": false,
      "reason": "Пост описывает инструмент."
    }
    """
    fenced = """```json
    {
      "genre_primary": "event_webinar",
      "genre_secondary": ["education_guide"],
      "genre_confidence": 0.7,
      "difficulty_score": 2,
      "promo_score": 3,
      "opinion_score": 0,
      "event_score": 5,
      "needs_review": false,
      "reason": "Это анонс вебинара."
    }
    ```"""

    parsed_clean = parse_genre_response(clean, taxonomy)
    parsed_fenced = parse_genre_response(fenced, taxonomy)

    assert parsed_clean["genre_primary"] == "tool_product"
    assert parsed_clean["genre_secondary"] == ["news_announcement"]
    assert parsed_fenced["genre_primary"] == "event_webinar"
    assert parsed_fenced["event_score"] == 5


def test_yandex_genre_parser_accepts_empty_reason():
    taxonomy = load_taxonomy()
    parsed = parse_genre_response(
        """
        {
          "genre_primary": "community_chat",
          "genre_secondary": [],
          "genre_confidence": 0.72,
          "difficulty_score": 0,
          "promo_score": 0,
          "opinion_score": 2,
          "event_score": 0,
          "needs_review": false,
          "reason": ""
        }
        """,
        taxonomy,
    )

    assert parsed["genre_primary"] == "community_chat"
    assert parsed["reason"] == ""


def test_yandex_genre_scores_are_clamped_to_zero_five():
    taxonomy = load_taxonomy()
    parsed = parse_genre_response(
        """
        {
          "genre_primary": "technical_research",
          "genre_secondary": [],
          "genre_confidence": 2,
          "difficulty_score": 9,
          "promo_score": -4,
          "opinion_score": "3",
          "event_score": "bad",
          "needs_review": false,
          "reason": "Технический материал."
        }
        """,
        taxonomy,
    )

    assert parsed["genre_confidence"] == 1.0
    assert parsed["difficulty_score"] == 5
    assert parsed["promo_score"] == 0
    assert parsed["opinion_score"] == 3
    assert parsed["event_score"] == 0


def test_yandex_genre_invalid_genre_falls_back_to_review():
    taxonomy = load_taxonomy()
    parsed = parse_genre_response(
        """
        {
          "genre_primary": "unknown",
          "genre_secondary": ["tool_product"],
          "genre_confidence": 0.9,
          "difficulty_score": 1,
          "promo_score": 1,
          "opinion_score": 1,
          "event_score": 1,
          "needs_review": false,
          "reason": "Невалидная метка."
        }
        """,
        taxonomy,
    )

    assert parsed["genre_primary"] == FALLBACK_GENRE
    assert parsed["needs_review"] is True
    assert "Некорректный жанр" in parsed["reason"]


def test_yandex_genre_usage_total_tokens_supports_yandex_keys():
    assert usage_total_tokens({"totalTokens": "42"}) == 42
    assert usage_total_tokens({"inputTextTokens": "10", "completionTokens": "7"}) == 17
    assert usage_total_tokens({}) == 0


def test_yandex_genre_prompt_contains_five_axes():
    taxonomy = load_taxonomy()
    item = SimpleNamespace(
        translated_summary=None,
        source_summary="Ссылка описывает новый инструмент для работы с LLM.",
        translated_title=None,
        title="Новый AI-инструмент",
        main_text="Запустили инструмент для анализа промптов.",
        source_url="https://example.com",
        source_domain="example.com",
    )
    processed = SimpleNamespace(
        language="ru",
        word_count=12,
        url_count=1,
        domains=["example.com"],
        has_code=False,
        has_github=False,
        has_arxiv=False,
        has_media=False,
    )

    system, user = build_genre_messages(taxonomy, item=item, processed=processed, snapshot=None)
    prompt_text = f"{system}\n{user}"

    assert "strict JSON" in system
    assert "genre_primary" in prompt_text
    assert "difficulty_score" in prompt_text
    assert "promo_score" in prompt_text
    assert "opinion_score" in prompt_text
    assert "event_score" in prompt_text


def test_yandex_genre_prompt_omits_reason_by_default():
    taxonomy = load_taxonomy()
    item = SimpleNamespace(
        translated_summary=None,
        source_summary="Саммари.",
        translated_title=None,
        title="Заголовок",
        main_text="Текст поста.",
        source_url=None,
        source_domain=None,
    )
    processed = SimpleNamespace(language="ru", word_count=2, url_count=0, domains=[], has_code=False, has_github=False, has_arxiv=False, has_media=False)

    _, user = build_genre_messages(taxonomy, item=item, processed=processed, snapshot=None)

    assert 'reason: верни пустую строку ""' in user


def test_media_only_unknown_is_local_and_zero_tokens():
    taxonomy = load_taxonomy()
    item = SimpleNamespace(
        id=123,
        source_post_id=456,
        translated_summary=None,
        source_summary=None,
        translated_title=None,
        title=None,
        main_text=None,
    )
    processed = SimpleNamespace(has_media=True, word_count=0)

    assert is_media_only_unknown(item, processed, None) is True
    result = media_only_result(item, taxonomy)

    assert result.genre_primary == FALLBACK_GENRE
    assert result.model_name == "local_media_prefilter"
    assert usage_total_tokens(result.usage) == 0


def test_yandex_trainer_reuses_existing_tfidf_model_name():
    values = model_version_values("candidate", "models/candidates/candidate/tfidf_logreg.joblib", "run1", {"splits": {}}, {})

    assert values["model_name"] == "tfidf_logreg"
    assert values["status"] == "candidate"


def test_yandex_trainer_maps_legacy_labels_to_axes_taxonomy():
    assert map_legacy_label("news_digest") == "news_announcement"
    assert map_legacy_label("promo_career_event", "приглашаем на вебинар") == "event_webinar"
    assert map_legacy_label("promo_career_event", "ищем data scientist") == "career_job"
