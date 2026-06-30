from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.content.topic_audit import (
    latest_topic_audit_summary,
    entry_has_publishable_basics,
    summarize_entries,
    topic_slugs_for_entry,
    write_report_files,
)


def test_topic_rules_route_non_noise_ai_categories():
    technical = {
        "genre_primary": "technical_research",
        "difficulty_score": 4,
        "promo_score": 0,
        "title": "New benchmark for LLM agents",
        "preview": "Detailed evals and architecture notes",
    }
    tool = {"genre_primary": "tool_product", "promo_score": 0, "title": "Open source AI IDE"}
    business_opinion = {
        "genre_primary": "opinion_commentary",
        "promo_score": 0,
        "title": "Как компании внедряют ИИ в продуктовую стратегию",
    }
    noise = {"genre_primary": "community_chat", "promo_score": 0, "title": "Спасибо"}

    assert topic_slugs_for_entry(technical) == ["ai_research_engineering"]
    assert topic_slugs_for_entry(tool) == ["ai_tools_products"]
    assert topic_slugs_for_entry(business_opinion) == ["ai_business_strategy"]
    assert topic_slugs_for_entry(noise) == []


def test_publishable_basics_require_image_and_article_summaries():
    base = {
        "is_eligible": True,
        "publication_allowed": True,
        "genre_primary": "technical_research",
        "promo_score": 0,
        "status": "ready_for_publication",
        "has_image": True,
        "article_links_total": 2,
        "article_summaries": 2,
    }

    assert entry_has_publishable_basics(base) is True
    assert entry_has_publishable_basics(base | {"has_image": False}) is False
    assert entry_has_publishable_basics(base | {"article_summaries": 1}) is False
    assert entry_has_publishable_basics(base | {"genre_primary": "promo_ad"}) is False


def test_latest_topic_audit_summary_picks_newest_summary(tmp_path: Path):
    old_dir = tmp_path / "20260101_000000"
    new_dir = tmp_path / "20260102_000000"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / "summary.json").write_text(json.dumps({"generated_at": "old"}), encoding="utf-8")
    (new_dir / "summary.json").write_text(json.dumps({"generated_at": "new"}), encoding="utf-8")

    summary = latest_topic_audit_summary(tmp_path)

    assert summary is not None
    assert summary["generated_at"] == "new"
    assert summary["artifacts"]["summary_json"].endswith("summary.json")


def test_report_writer_creates_expected_artifacts(tmp_path: Path):
    summary = summarize_entries([], generated_at=datetime.now(timezone.utc), scope="all-unpublished", sample_per_topic=2)

    artifacts = write_report_files(summary, [], tmp_path)

    assert Path(artifacts["summary_json"]).exists()
    assert Path(artifacts["summary_md"]).read_text(encoding="utf-8").startswith("# Аудит")
    assert Path(artifacts["topics_csv"]).exists()
    assert Path(artifacts["samples_jsonl"]).read_text(encoding="utf-8") == ""
    assert Path(artifacts["training_plan_md"]).exists()
