from __future__ import annotations

import ipaddress
from types import SimpleNamespace

from app.content.classifier import build_classification_text
from app.content.local_llm import local_model_label, parse_chat_completion_response
from app.content.local_summary import extractive_summary
from app.content.link_materials import (
    LinkMaterial,
    append_link_materials_section,
    build_enriched_numbered_digest_body,
    format_link_summary_context,
    is_article_like_link,
    link_slots_for_post,
    parse_numbered_sections,
    link_summary_gate,
    markdown_link_segments,
    restore_missing_markdown_links,
    telegram_link_segments,
    telegram_text_with_markdown_links,
)
from app.content.model_promoter import promotion_decision
from app.content.pipeline_rewriter import append_source_post_link, should_block_link_summary_gate, telegram_post_source_url
from app.content.prompt_versions import LINK_SUMMARY_PROMPT, PIPELINE_REWRITE_PROMPT, default_prompt_values, prompt_config_from_values
from app.content.rewriter import draft_from_template
from app.content.text_utils import clean_text, normalize_text
from app.content.url_utils import detect_url_type, extract_urls, is_public_ip, normalize_url, youtube_thumbnail_url, youtube_video_id
from app.content.yandex_gpt import (
    build_rewrite_messages,
    extract_yandex_keys_from_text,
    label_prompts,
    load_prompt_config,
    parse_completion_response,
    parse_rewrite_response,
    redact,
)


def test_clean_and_normalize_text():
    assert clean_text("  A\r\n\r\n\r\nB  ") == "A\n\nB"
    assert normalize_text("Hello https://example.com/?utm_source=x  WORLD") == "hello world"


def test_extract_urls_from_text_and_raw_entities():
    raw = {"entities": [{"url": "https://example.com/page?utm_source=tg", "offset": 10}]}
    urls = extract_urls("see https://github.com/org/repo", raw)

    normalized = [normalize_url(item.url) for item in urls]

    assert "https://github.com/org/repo" in normalized
    assert "https://example.com/page" in normalized
    assert detect_url_type("https://github.com/org/repo") == "github"


def test_youtube_thumbnail_url_from_short_and_watch_urls():
    assert youtube_video_id("https://youtu.be/RSaP_x4qTmQ") == "RSaP_x4qTmQ"
    assert youtube_video_id("https://www.youtube.com/watch?v=RSaP_x4qTmQ&utm_source=x") == "RSaP_x4qTmQ"
    assert youtube_thumbnail_url("https://youtu.be/RSaP_x4qTmQ") == "https://img.youtube.com/vi/RSaP_x4qTmQ/hqdefault.jpg"


def test_prompt_defaults_cover_rewrite_and_link_summary():
    rewrite_values = default_prompt_values(PIPELINE_REWRITE_PROMPT)
    summary_values = default_prompt_values(LINK_SUMMARY_PROMPT)

    assert rewrite_values["system_prompt"]
    assert rewrite_values["common_user_prompt"]
    assert isinstance(rewrite_values["label_prompts"], dict)
    assert summary_values["system_prompt"]
    assert summary_values["common_user_prompt"]
    assert summary_values["label_prompts"] == {}
    assert prompt_config_from_values(LINK_SUMMARY_PROMPT, "sys", "user", {}) == {"summary": {"system": "sys", "user": "user"}}


def test_link_material_gate_blocks_only_article_like_links():
    article_without_summary = LinkMaterial(
        link=SimpleNamespace(url_type="article", extraction_status="pending", original_url="https://example.com/a", domain="example.com"),
        snapshot=None,
    )
    github_without_summary = LinkMaterial(
        link=SimpleNamespace(url_type="github", extraction_status="pending", original_url="https://github.com/o/r", domain="github.com"),
        snapshot=None,
    )
    article_with_summary = LinkMaterial(
        link=SimpleNamespace(url_type="arxiv", extraction_status="done", original_url="https://arxiv.org/abs/1", domain="arxiv.org"),
        snapshot=SimpleNamespace(summary_short="Summary ready", title="Paper", final_url=None, site_name=None, error=None),
    )

    gate = link_summary_gate([article_without_summary, github_without_summary])

    assert is_article_like_link(article_without_summary.link) is True
    assert is_article_like_link(github_without_summary.link) is False
    assert gate.ok is False
    assert gate.status == "link_summary_pending"
    assert link_summary_gate([github_without_summary, article_with_summary]).ok is True


def test_link_material_gate_marks_failed_article_snapshots():
    failed_article = LinkMaterial(
        link=SimpleNamespace(url_type="telegram", extraction_status="failed", original_url="https://t.me/channel/1", domain="t.me"),
        snapshot=SimpleNamespace(summary_short=None, title=None, final_url=None, site_name=None, error="fetch failed"),
    )

    gate = link_summary_gate([failed_article])

    assert gate.ok is False
    assert gate.status == "link_summary_failed"


def test_manual_rewrite_can_bypass_pending_link_summary_gate_only():
    pending_gate = SimpleNamespace(ok=False, status="link_summary_pending")
    failed_gate = SimpleNamespace(ok=False, status="link_summary_failed")

    assert should_block_link_summary_gate(pending_gate, allow_pending_link_summaries=False) is True
    assert should_block_link_summary_gate(pending_gate, allow_pending_link_summaries=True) is False
    assert should_block_link_summary_gate(failed_gate, allow_pending_link_summaries=True) is True


def test_telegram_post_source_url_and_append_are_idempotent():
    post = SimpleNamespace(chat_peer_id=-1002086767935, message_id=282)
    chat = SimpleNamespace(username="andre_dataist")

    source_url = telegram_post_source_url(post, chat)
    body = append_source_post_link("Черновик", source_url)

    assert source_url == "https://t.me/andre_dataist/282"
    assert body.endswith("Источник: [оригинальный пост](https://t.me/andre_dataist/282)")
    assert append_source_post_link(body, source_url) == body


def test_telegram_post_source_url_uses_private_channel_fallback():
    post = SimpleNamespace(chat_peer_id=-1002086767935, message_id=282)
    chat = SimpleNamespace(username=None)

    assert telegram_post_source_url(post, chat) == "https://t.me/c/2086767935/282"


def test_link_summary_context_and_draft_section_include_article_urls():
    materials = [
        LinkMaterial(
            link=SimpleNamespace(url_type="article", extraction_status="done", original_url="https://example.com/a", final_url=None, canonical_url=None, domain="example.com"),
            snapshot=SimpleNamespace(summary_short="Article summary", title="Article title", final_url="https://example.com/final", site_name=None, error=None),
        ),
        LinkMaterial(
            link=SimpleNamespace(url_type="huggingface", extraction_status="pending", original_url="https://huggingface.co/model", final_url=None, canonical_url=None, domain="huggingface.co"),
            snapshot=None,
        ),
    ]

    context = format_link_summary_context(materials)
    body = append_link_materials_section("Черновик", materials)

    assert "1. Article title" in context
    assert "Article summary" in context
    assert "huggingface" not in context
    assert "Материалы по ссылкам:" in body
    assert "https://example.com/final" in body


def test_telegram_link_segments_use_utf16_offsets_for_emoji():
    text = "😀 Обзор статьи"
    raw = {"entities": [{"_": "MessageEntityTextUrl", "offset": 3, "length": 5, "url": "https://example.com/article"}]}

    segments = telegram_link_segments(text, raw)

    assert [(segment.text, segment.url) for segment in segments] == [
        ("😀 ", None),
        ("Обзор", "https://example.com/article"),
        (" статьи", None),
    ]


def test_telegram_text_with_markdown_links_preserves_hidden_urls():
    text = "См. обзор и код"
    raw = {
        "entities": [
            {"_": "MessageEntityTextUrl", "offset": 4, "length": 5, "url": "https://example.com/review"},
            {"_": "MessageEntityTextUrl", "offset": 12, "length": 3, "url": "https://github.com/org/repo"},
        ]
    }

    linked = telegram_text_with_markdown_links(text, raw)
    segments = markdown_link_segments(linked)

    assert linked == "См. [обзор](https://example.com/review) и [код](https://github.com/org/repo)"
    assert [(segment.text, segment.url) for segment in segments] == [
        ("См. ", None),
        ("обзор", "https://example.com/review"),
        (" и ", None),
        ("код", "https://github.com/org/repo"),
    ]


def utf16_offset(text: str, marker: str) -> int:
    return len(text[: text.index(marker)].encode("utf-16-le")) // 2


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_overlapping_and_decorative_telegram_entities_do_not_create_empty_links():
    text = "1. Мир\n\n🔍 Обзор статьи | 📜 Полная статья"
    raw = {
        "entities": [
            {
                "_": "MessageEntityTextUrl",
                "offset": utf16_offset(text, "🔍"),
                "length": utf16_length("🔍"),
                "url": "https://telegra.ph/preview",
            },
            {
                "_": "MessageEntityTextUrl",
                "offset": utf16_offset(text, "Обзор статьи"),
                "length": utf16_length("Обзор статьи"),
                "url": "https://t.me/dataism_science/129",
            },
            {
                "_": "MessageEntityTextUrl",
                "offset": utf16_offset(text, "📜 Полная статья"),
                "length": utf16_length("📜 Полная статья"),
                "url": "https://arxiv.org/html/1",
            },
        ]
    }

    linked = telegram_text_with_markdown_links(text, raw)

    assert "[🔍]" not in linked
    assert "[](https://" not in linked
    assert "[Обзор статьи](https://t.me/dataism_science/129)" in linked
    assert "[📜 Полная статья](https://arxiv.org/html/1)" in linked


def test_numbered_sections_assign_links_and_insert_summaries_in_place():
    text = "\n\n".join(
        [
            "Вступление",
            "1. Первый пункт\nОписание первого.\n\n🔍 Обзор статьи | 📜 Полная статья",
            "2. Второй пункт\nОписание второго.\n\n🔍 Обзор статьи",
        ]
    )
    review_url = "https://t.me/dataism_science/129"
    article_url = "https://arxiv.org/html/1"
    second_url = "https://t.me/dataism_science/130"
    raw = {
        "entities": [
            {"_": "MessageEntityTextUrl", "offset": utf16_offset(text, "Обзор статьи"), "length": utf16_length("Обзор статьи"), "url": review_url},
            {"_": "MessageEntityTextUrl", "offset": utf16_offset(text, "📜 Полная статья"), "length": utf16_length("📜 Полная статья"), "url": article_url},
            {
                "_": "MessageEntityTextUrl",
                "offset": text.index("🔍 Обзор статьи", text.index("2.")).bit_length(),  # overwritten below
                "length": utf16_length("Обзор статьи"),
                "url": second_url,
            },
        ]
    }
    raw["entities"][2]["offset"] = utf16_offset(text[text.index("2.") :], "Обзор статьи") + utf16_offset(text, "2.")
    materials = [
        LinkMaterial(
            link=SimpleNamespace(url_type="telegram", original_url=review_url, canonical_url=review_url, final_url=None, domain="t.me"),
            snapshot=SimpleNamespace(summary_short="Summary первого обзора", title="Обзор", final_url=None, canonical_url=None, site_name=None, error=None),
        ),
        LinkMaterial(
            link=SimpleNamespace(url_type="arxiv", original_url=article_url, canonical_url=article_url, final_url=None, domain="arxiv.org"),
            snapshot=SimpleNamespace(summary_short="Summary полной статьи", title="Paper", final_url=None, canonical_url=None, site_name=None, error=None),
        ),
        LinkMaterial(
            link=SimpleNamespace(url_type="telegram", original_url=second_url, canonical_url=second_url, final_url=None, domain="t.me"),
            snapshot=SimpleNamespace(summary_short="Summary второго обзора", title="Обзор 2", final_url=None, canonical_url=None, site_name=None, error=None),
        ),
    ]

    slots = link_slots_for_post(text, raw, materials)
    _preamble, sections = parse_numbered_sections(text, slots)
    body, meta = build_enriched_numbered_digest_body(text, raw, materials)

    assert [slot.url for slot in sections[0].slots] == [review_url, article_url]
    assert [slot.url for slot in sections[1].slots] == [second_url]
    assert meta["enriched_sections"] == 2
    assert body.index("Summary первого обзора") < body.index("2. Второй пункт")
    assert "Summary второго обзора" in body
    assert "Ссылки из исходного поста" not in body


def test_restore_missing_markdown_links_relinks_matching_text_and_adds_fallbacks():
    source_segments = [
        SimpleNamespace(text="обзор", url="https://example.com/review"),
        SimpleNamespace(text="код", url="https://github.com/org/repo"),
    ]

    body, restored = restore_missing_markdown_links("Новый обзор статьи.", source_segments)

    assert "[обзор](https://example.com/review)" in body
    assert "[код](https://github.com/org/repo)" in body
    assert restored == ["https://example.com/review", "https://github.com/org/repo"]


def test_restore_missing_markdown_links_puts_fallbacks_inside_numbered_sections():
    source_text = "1. Первый\n\nОбзор\n\n2. Второй\n\nКод"
    source_segments = [
        SimpleNamespace(text="1. Первый\n\n", url=None, start=0, end=11),
        SimpleNamespace(text="Обзор", url="https://example.com/review", start=11, end=16),
        SimpleNamespace(text="\n\n2. Второй\n\n", url=None, start=16, end=28),
        SimpleNamespace(text="Код", url="https://github.com/org/repo", start=28, end=31),
    ]

    body, restored = restore_missing_markdown_links("1. Первый\n\nНовый текст.\n\n2. Второй\n\nЕще текст.", source_segments)

    assert "Ссылки из исходного поста" not in body
    assert "1. Первый\n\nНовый текст.\n\nСсылки: [Обзор](https://example.com/review)" in body
    assert "2. Второй\n\nЕще текст.\n\nСсылки: [Код](https://github.com/org/repo)" in body
    assert restored == ["https://example.com/review", "https://github.com/org/repo"]


def test_private_ips_are_blocked_for_fetching():
    assert is_public_ip(ipaddress.ip_address("127.0.0.1")) is False
    assert is_public_ip(ipaddress.ip_address("10.1.2.3")) is False
    assert is_public_ip(ipaddress.ip_address("169.254.169.254")) is False


def test_classification_text_contains_expected_sections():
    text = build_classification_text(
        "Пост",
        "Заголовок",
        "Описание",
        "Саммари",
        ["example.com"],
        {"has_github": True, "has_arxiv": False, "has_code": True},
    )

    assert "POST:" in text
    assert "LINK_TITLE:" in text
    assert "DOMAINS:" in text
    assert "has_github=True" in text


def test_extractive_summary_is_shorter_for_long_text():
    source = "\n\n".join([f"Абзац {index}. Это важный текст про новую модель OpenAI и практическое использование." for index in range(20)])

    summary = extractive_summary(source, "новая модель OpenAI", max_chars=300)

    assert 40 <= len(summary) <= 300
    assert "модель" in summary


def test_promotion_requires_comparable_active_metrics():
    candidate = SimpleNamespace(metrics={"test": {"macro_f1": 0.8, "weighted_f1": 0.82, "per_class": {}}})
    active = SimpleNamespace(metrics={})
    settings = SimpleNamespace(
        model_promotion_min_macro_f1_delta=-0.01,
        model_promotion_min_weighted_f1_delta=-0.01,
        model_promotion_min_class_f1=0.25,
    )

    promoted, reason = promotion_decision(candidate, active, settings)

    assert promoted is False
    assert reason == "active_model_has_no_comparable_test_metrics"


def test_rewriter_for_post_without_link_has_no_internal_jargon():
    item = SimpleNamespace(
        source_url=None,
        source_domain=None,
        translated_summary=None,
        source_summary=None,
        main_text='А пока ловите мем. Data scientist: "Что думаете по поводу XGBoost 2?" Leadership: "Никогда не слышал о такой LLM-ке".',
        translated_title=None,
        title="Всем привет из отпуска!",
        primary_image_asset_id=None,
    )
    showcase = SimpleNamespace(default_rewrite_template="news_short")

    draft = draft_from_template(item, showcase, {}, "opinion_commentary")

    assert "Коротко" not in draft["body"]
    assert "opinion_commentary" not in draft["body"]
    assert "source unavailable" not in draft["body"]
    assert "Автоматическое извлечение" not in draft["body"]
    assert "AI-среды" not in draft["body"]


def test_yandex_key_mapping_redacts_secret_values():
    text = """yandexgpt_keys:
secret:
very-secret-token
api:
key-id-123
folder:
folder-123
"""

    values = extract_yandex_keys_from_text(text)

    assert values == {
        "YANDEX_API_KEY": "very-secret-token",
        "YANDEX_API_KEY_ID": "key-id-123",
        "YANDEX_FOLDER_ID": "folder-123",
    }
    assert "very-secret-token" not in redact(values["YANDEX_API_KEY"])


def test_yandex_completion_response_parser_extracts_text_and_usage():
    payload = {
        "result": {
            "alternatives": [{"message": {"role": "assistant", "text": " Готово к работе. "}}],
            "usage": {"inputTextTokens": "10", "completionTokens": "3"},
        }
    }

    completion = parse_completion_response(payload, "gpt://folder/yandexgpt-5-lite")

    assert completion.text == "Готово к работе."
    assert completion.usage["inputTextTokens"] == "10"


def test_yandex_prompt_selector_covers_all_labels():
    expected = {
        "news_digest",
        "technical_research",
        "tool_product",
        "education_guide",
        "opinion_commentary",
        "business_market",
        "promo_career_event",
        "community_chat",
        "humor_meme",
    }

    prompts = load_prompt_config()
    labels = label_prompts(prompts)
    system, user = build_rewrite_messages(
        prompts,
        label="technical_research",
        title="Заголовок",
        post_text="Пост о benchmark новой модели.",
        source_summary="Summary ссылки.",
        source_url="https://example.com",
        showcase_title="AI Technical",
        max_input_chars=2000,
    )

    assert set(labels) == expected
    assert "Коротко" in system
    assert "технические пометки" in system
    assert "обязательно сформируй новый редакционный заголовок" in system
    assert "title обязателен" in user
    assert "technical_research" in user


def test_yandex_rewrite_json_parser_handles_plain_and_fenced_json():
    plain = '{"title":"Заголовок","body":"Текст публикации.","risk_flags":[],"claims":[{"text":"Факт","source":"post"}]}'
    fenced = """```json
{"title":"Коротко: Заголовок","body":"Коротко: Текст публикации.","risk_flags":["needs_review"],"claims":[]}
```"""

    parsed_plain = parse_rewrite_response(plain)
    parsed_fenced = parse_rewrite_response(fenced)

    assert parsed_plain["claims"] == [{"text": "Факт", "source": "post"}]
    assert parsed_fenced["title"] == "Заголовок"
    assert parsed_fenced["body"] == "Текст публикации."
    assert parsed_fenced["risk_flags"] == ["needs_review"]


def test_local_llm_response_parser_extracts_text_usage_and_model_label():
    payload = {
        "model": "IlyaGusev/saiga_nemo_12b_gguf:Q4_K_M",
        "choices": [{"message": {"role": "assistant", "content": " {\"title\":\"T\",\"body\":\"B\",\"risk_flags\":[],\"claims\":[]} "}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    completion = parse_chat_completion_response(payload, "fallback")

    assert completion.text.startswith("{")
    assert completion.usage["completion_tokens"] == 5
    assert local_model_label(completion.model) == "saiga_nemo_12b.Q4_K_M"
