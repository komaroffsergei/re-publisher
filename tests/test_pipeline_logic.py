from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.content.max_publisher as max_publisher
from app.content.pipeline_logic import (
    READY_DRAFT_STATUS,
    axes_from_label_scores,
    education_eligibility,
    status_from_state,
)
from app.content.max_publisher import MAX_TEXT_LIMIT, MaxPublisherError, compose_max_text, max_text_size
from app.content.pipeline_entries import (
    INCOMPLETE_ENTRY_STATUSES,
    PIPELINE_STAGE_ENRICHED,
    PIPELINE_STAGE_PUBLISHED,
    PIPELINE_STAGE_READY,
    PIPELINE_STAGE_RECEIVED,
    PIPELINE_STAGE_REWRITTEN,
    PIPELINE_STAGE_SORTED,
    pipeline_stage_for_state,
    link_summary_status_from_missing,
)
from app.content.link_materials import LINK_SUMMARY_FAILED_STATUS, LINK_SUMMARY_PENDING_STATUS
from app.content.pipeline_rewriter import composed_max_text_size, fit_body_to_max_text_limit
from app.content.pipeline_activity import active_state_phase
from app.web.main import content_state_activity, parse_entry_ids


def test_axes_from_label_scores_accepts_nested_and_flat_values():
    nested = axes_from_label_scores({"axes": {"difficulty_score": 3, "promo_score": 0, "opinion_score": 2, "event_score": 0}})
    flat = axes_from_label_scores({"difficulty_score": 9, "axis:promo_score": -1})

    assert nested["difficulty_score"] == 3
    assert nested["opinion_score"] == 2
    assert flat["difficulty_score"] == 5
    assert flat["promo_score"] == 0


def test_education_eligibility_requires_education_genre_and_axes():
    ok = education_eligibility(
        "tool_product",
        ["education_guide"],
        {"difficulty_score": 2, "promo_score": 0, "opinion_score": 1, "event_score": 0},
    )
    promo = education_eligibility(
        "education_guide",
        [],
        {"difficulty_score": 2, "promo_score": 1, "opinion_score": 0, "event_score": 0},
    )
    missing = education_eligibility(
        "technical_research",
        ["tool_product"],
        {"difficulty_score": 4, "promo_score": 0, "opinion_score": 0, "event_score": 0},
    )

    assert ok.is_eligible is True
    assert promo.is_eligible is False
    assert promo.reason == "promo_not_zero"
    assert missing.reason == "education_guide_missing"


def test_pipeline_status_reflects_allowed_ready_published_and_errors():
    assert status_from_state(publication_allowed=False, is_eligible=True, draft_status=None, has_published_post=False) == "blocked"
    assert status_from_state(publication_allowed=True, is_eligible=False, draft_status=None, has_published_post=False) == "ineligible"
    assert (
        status_from_state(
            publication_allowed=True,
            is_eligible=True,
            draft_status=READY_DRAFT_STATUS,
            has_published_post=False,
        )
        == READY_DRAFT_STATUS
    )
    assert status_from_state(publication_allowed=True, is_eligible=True, draft_status=None, has_published_post=True) == "published"
    assert status_from_state(publication_allowed=True, is_eligible=True, draft_status=None, has_published_post=False, has_error=True) == "rewrite_failed"


def test_pipeline_stage_tracks_lifecycle_order():
    base = {
        "has_classification": True,
        "has_content_item": True,
        "is_eligible": True,
        "draft_status": None,
        "has_published_post": False,
    }
    assert (
        pipeline_stage_for_state(
            has_classification=False,
            has_content_item=False,
            is_eligible=False,
            draft_status=None,
            has_published_post=False,
        )
        == PIPELINE_STAGE_RECEIVED
    )
    assert (
        pipeline_stage_for_state(
            **base,
        )
        == PIPELINE_STAGE_SORTED
    )
    assert (
        pipeline_stage_for_state(
            has_classification=True,
            has_content_item=True,
            is_eligible=False,
            draft_status=None,
            has_published_post=False,
        )
        == PIPELINE_STAGE_SORTED
    )
    assert (
        pipeline_stage_for_state(
            **base,
            is_enriched=True,
        )
        == PIPELINE_STAGE_ENRICHED
    )
    assert (
        pipeline_stage_for_state(
            **(base | {"draft_status": "draft"}),
        )
        == PIPELINE_STAGE_REWRITTEN
    )
    assert (
        pipeline_stage_for_state(
            **(base | {"draft_status": READY_DRAFT_STATUS}),
        )
        == PIPELINE_STAGE_REWRITTEN
    )
    assert (
        pipeline_stage_for_state(
            **(base | {"draft_status": READY_DRAFT_STATUS}),
            is_publication_ready=True,
        )
        == PIPELINE_STAGE_READY
    )
    assert (
        pipeline_stage_for_state(
            **(base | {"draft_status": READY_DRAFT_STATUS, "has_published_post": True}),
        )
        == PIPELINE_STAGE_REWRITTEN
    )
    assert (
        pipeline_stage_for_state(
            **(base | {"draft_status": READY_DRAFT_STATUS, "has_published_post": True}),
            is_publication_ready=True,
            is_failed_or_incomplete=True,
        )
        == PIPELINE_STAGE_RECEIVED
    )
    assert (
        pipeline_stage_for_state(
            **(base | {"draft_status": READY_DRAFT_STATUS, "has_published_post": True}),
            is_publication_ready=True,
        )
        == PIPELINE_STAGE_PUBLISHED
    )


def test_link_summary_statuses_are_incomplete_terminal_states():
    assert LINK_SUMMARY_PENDING_STATUS in INCOMPLETE_ENTRY_STATUSES
    assert LINK_SUMMARY_FAILED_STATUS in INCOMPLETE_ENTRY_STATUSES
    assert (
        pipeline_stage_for_state(
            has_classification=True,
            has_content_item=True,
            is_eligible=True,
            draft_status=READY_DRAFT_STATUS,
            has_published_post=False,
            is_publication_ready=False,
            is_failed_or_incomplete=True,
        )
        == PIPELINE_STAGE_RECEIVED
    )


def test_link_summary_status_is_derived_from_readiness_missing():
    assert link_summary_status_from_missing((LINK_SUMMARY_PENDING_STATUS,)) == LINK_SUMMARY_PENDING_STATUS
    assert link_summary_status_from_missing((LINK_SUMMARY_FAILED_STATUS, LINK_SUMMARY_PENDING_STATUS)) == LINK_SUMMARY_FAILED_STATUS
    assert link_summary_status_from_missing(("image", "rewrite")) is None


def test_max_text_limit_uses_utf16_units_for_emoji():
    class Draft:
        title = "Заголовок"
        body = ("Текст с emoji 📜 " * 500).strip()

    text = compose_max_text(Draft())

    assert max_text_size(text) <= MAX_TEXT_LIMIT
    assert len(text) <= MAX_TEXT_LIMIT


def test_rewrite_body_fit_preserves_source_link_and_max_limit():
    title = "Почему ИИ галлюцинирует и как он учится управлять миром"
    source_url = "https://t.me/c/123456/789"
    source_line = f"Источник: [оригинальный пост]({source_url})"
    body = (("1. Пункт с summary 📜 и ссылками на исследование.\n") * 500).strip()
    body = f"{body}\n\n{source_line}"

    fitted, trimmed = fit_body_to_max_text_limit(title, body, source_url)

    assert trimmed is True
    assert source_line in fitted
    assert composed_max_text_size(title, fitted) <= MAX_TEXT_LIMIT


def test_pipeline_board_entry_id_parser_deduplicates_and_ignores_noise():
    assert parse_entry_ids("1, 2, x, 2; 3, -4, 0") == [1, 2, 3]


def test_content_state_activity_uses_running_operation_not_pending_default():
    pending_state = SimpleNamespace(processing_status="pending", summary_status="pending")
    running_state = SimpleNamespace(processing_status="done", summary_status="running")

    assert content_state_activity(pending_state) is None
    activity = content_state_activity(running_state)
    assert activity is not None
    assert activity["active_kind"] == "collector"
    assert activity["phase"] == "summarize_links"


def test_active_state_phase_reports_first_running_pipeline_field():
    state = SimpleNamespace(
        processing_status="done",
        link_status="done",
        enrichment_status="running",
        summary_status="pending",
        material_status="pending",
        classification_status="pending",
        rewrite_status="pending",
        publication_status="pending",
    )

    assert active_state_phase(state) == ("enrich_links", "обогащение ссылок")


def test_max_upload_token_extraction_accepts_nested_payload_and_upload_url():
    assert max_publisher.extract_upload_token({"payload": {"token": "abc"}}) == "abc"
    assert max_publisher.extract_upload_token({}, "https://iu.oneme.ru/upload.do?token=from-url") == "from-url"


def test_max_message_image_attachment_detection_uses_body_attachments():
    message = {"body": {"attachments": [{"type": "image", "payload": {"token": "abc"}}]}}

    assert max_publisher.message_has_image_attachment(message) is True
    assert max_publisher.message_has_image_attachment({"body": {"attachments": [{"type": "file"}]}}) is False


@pytest.mark.asyncio
async def test_send_max_message_requires_media():
    settings = SimpleNamespace(max_bot_token="token", max_channel_chat_id="1", max_api_base="https://example.test")
    draft = SimpleNamespace(title="Title", body="Body")

    with pytest.raises(MaxPublisherError, match="missing_media"):
        await max_publisher.send_max_message(settings, draft, None)


@pytest.mark.asyncio
async def test_send_max_message_uploads_image_attachment_and_verifies(monkeypatch):
    settings = SimpleNamespace(max_bot_token="token", max_channel_chat_id="1", max_api_base="https://example.test")
    draft = SimpleNamespace(title="Title", body="Body")
    media = SimpleNamespace(id=7)
    captured = {}

    async def fake_upload(_settings, _media):
        return {"token": "uploaded-token"}

    async def fake_request(_settings, method, path, **kwargs):
        if method == "POST" and path == "/messages":
            captured["payload"] = kwargs["json"]
            return {"message": {"body": {"mid": "mid-1"}, "url": "https://max.test/mid-1"}}
        if method == "GET" and path == "/messages/mid-1":
            return {"body": {"attachments": [{"type": "image", "payload": {"token": "uploaded-token"}}]}, "url": "https://max.test/mid-1"}
        raise AssertionError((method, path))

    monkeypatch.setattr(max_publisher, "upload_max_image", fake_upload)
    monkeypatch.setattr(max_publisher, "max_request", fake_request)

    message_id, url = await max_publisher.send_max_message(settings, draft, media)

    assert message_id == "mid-1"
    assert url == "https://max.test/mid-1"
    assert captured["payload"]["attachments"] == [{"type": "image", "payload": {"token": "uploaded-token"}}]


@pytest.mark.asyncio
async def test_send_max_message_retries_attachment_not_ready(monkeypatch):
    settings = SimpleNamespace(max_bot_token="token", max_channel_chat_id="1", max_api_base="https://example.test")
    draft = SimpleNamespace(title="Title", body="Body")
    media = SimpleNamespace(id=7)
    post_calls = 0

    async def fake_upload(_settings, _media):
        return {"token": "uploaded-token"}

    async def fake_sleep(_seconds):
        return None

    async def fake_request(_settings, method, path, **kwargs):
        nonlocal post_calls
        if method == "POST" and path == "/messages":
            post_calls += 1
            if post_calls == 1:
                raise MaxPublisherError("MAX HTTP 400: attachment.not.ready")
            return {"message": {"body": {"mid": "mid-2"}}}
        if method == "GET" and path == "/messages/mid-2":
            return {"body": {"attachments": [{"type": "image", "payload": {"token": "uploaded-token"}}]}}
        raise AssertionError((method, path))

    monkeypatch.setattr(max_publisher, "upload_max_image", fake_upload)
    monkeypatch.setattr(max_publisher, "max_request", fake_request)
    monkeypatch.setattr(max_publisher.asyncio, "sleep", fake_sleep)

    message_id, _url = await max_publisher.send_max_message(settings, draft, media)

    assert message_id == "mid-2"
    assert post_calls == 2
