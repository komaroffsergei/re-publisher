from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TelegramChat(TimestampMixin, Base):
    __tablename__ = "telegram_chats"

    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_type: Mapped[str] = mapped_column(Text, nullable=False)
    folder_name: Mapped[str] = mapped_column(Text, nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class TelegramPost(TimestampMixin, Base):
    __tablename__ = "telegram_posts"
    __table_args__ = (UniqueConstraint("chat_peer_id", "message_id", name="uq_telegram_posts_chat_message"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_peer_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_peer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    edit_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    grouped_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forwards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replies_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)


class TelegramComment(TimestampMixin, Base):
    __tablename__ = "telegram_comments"
    __table_args__ = (
        UniqueConstraint("discussion_peer_id", "comment_message_id", name="uq_telegram_comments_discussion_message"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_chat_peer_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    post_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discussion_peer_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    comment_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parent_comment_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_peer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    edit_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)


class TelegramSyncState(Base):
    __tablename__ = "telegram_sync_state"

    chat_peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PostProcessed(Base):
    __tablename__ = "post_processed"

    post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), primary_key=True)
    clean_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_hash: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    emoji_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domains: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    hashtags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    mentions: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    has_code: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    has_github: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    has_arxiv: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    has_media: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PostLink(TimestampMixin, Base):
    __tablename__ = "post_links"
    __table_args__ = (
        UniqueConstraint("post_id", "original_url", name="uq_post_links_post_original_url"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    url_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default="false", index=True, nullable=False)
    extraction_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)


class LinkSnapshot(TimestampMixin, Base):
    __tablename__ = "link_snapshots"
    __table_args__ = (UniqueConstraint("link_id", name="uq_link_snapshots_link_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("post_links.id", ondelete="CASCADE"), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    site_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_quality_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    summary_short: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_asset_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class MediaAsset(TimestampMixin, Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        Index("uq_media_assets_sha256_not_null", "sha256", unique=True, postgresql_where=text("sha256 IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_post_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_posts.id"), nullable=True)
    source_link_id: Mapped[int | None] = mapped_column(ForeignKey("post_links.id"), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ContentItem(TimestampMixin, Base):
    __tablename__ = "content_items"
    __table_args__ = (UniqueConstraint("source_post_id", name="uq_content_items_source_post_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False)
    primary_link_id: Mapped[int | None] = mapped_column(ForeignKey("post_links.id"), nullable=True)
    primary_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("link_snapshots.id"), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    main_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_lang: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_lang: Mapped[str] = mapped_column(Text, server_default="ru", nullable=False)
    translated_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    translated_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_image_asset_id: Mapped[int | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    duplicate_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default="ready", nullable=False)


class PostClassification(Base):
    __tablename__ = "post_classifications"
    __table_args__ = (
        UniqueConstraint("post_id", "classifier_name", "classifier_version", name="uq_post_classifications_model"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False)
    content_item_id: Mapped[int | None] = mapped_column(ForeignKey("content_items.id", ondelete="CASCADE"), nullable=True)
    classifier_name: Mapped[str] = mapped_column(Text, nullable=False)
    classifier_version: Mapped[str] = mapped_column(Text, nullable=False)
    label_primary: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    label_secondary: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    label_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric, index=True, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, server_default="false", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class YandexGenreClassification(Base):
    __tablename__ = "yandex_genre_classifications"
    __table_args__ = (
        UniqueConstraint("source_post_id", "run_id", name="uq_yandex_genre_post_run"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False)
    content_item_id: Mapped[int | None] = mapped_column(ForeignKey("content_items.id", ondelete="CASCADE"), nullable=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(Text, nullable=False)
    genre_primary: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    genre_secondary: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    genre_confidence: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    difficulty_score: Mapped[int] = mapped_column(Integer, nullable=False)
    promo_score: Mapped[int] = mapped_column(Integer, nullable=False)
    opinion_score: Mapped[int] = mapped_column(Integer, nullable=False)
    event_score: Mapped[int] = mapped_column(Integer, nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, server_default="false", index=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CodexGenreClassification(Base):
    __tablename__ = "codex_genre_classifications"
    __table_args__ = (
        UniqueConstraint("source_post_id", "run_id", name="uq_codex_genre_post_run"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False)
    content_item_id: Mapped[int | None] = mapped_column(ForeignKey("content_items.id", ondelete="CASCADE"), nullable=True)
    run_id: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(Text, nullable=False)
    teacher_name: Mapped[str] = mapped_column(Text, nullable=False)
    split: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    genre_primary: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    genre_secondary: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    genre_confidence: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    difficulty_score: Mapped[int] = mapped_column(Integer, nullable=False)
    promo_score: Mapped[int] = mapped_column(Integer, nullable=False)
    opinion_score: Mapped[int] = mapped_column(Integer, nullable=False)
    event_score: Mapped[int] = mapped_column(Integer, nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CodexGenreModelComparison(Base):
    __tablename__ = "codex_genre_model_comparisons"
    __table_args__ = (
        UniqueConstraint("source_post_id", "run_id", "model_version", name="uq_codex_comparison_post_model_run"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False)
    content_item_id: Mapped[int | None] = mapped_column(ForeignKey("content_items.id", ondelete="CASCADE"), nullable=True)
    run_id: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    split: Mapped[str] = mapped_column(Text, nullable=False)
    teacher_genre: Mapped[str] = mapped_column(Text, nullable=False)
    teacher_secondary: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    teacher_axes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    predicted_genre: Mapped[str] = mapped_column(Text, nullable=False)
    predicted_secondary: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    predicted_axes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    label_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    match_percent: Mapped[float] = mapped_column(Numeric, index=True, nullable=False)
    mismatch_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CodexTrainingRun(Base):
    __tablename__ = "codex_training_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_iteration: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    duration_hours: Mapped[float] = mapped_column(Numeric, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False)
    report_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    latest_model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    best_match_percent: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    worst_match_percent: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    latest_match_percent: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PostLabel(Base):
    __tablename__ = "post_labels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    label_set_version: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default="proposed", nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)


class LabelingQueue(TimestampMixin, Base):
    __tablename__ = "labeling_queue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_confidence: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    human_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("model_name", "model_version", name="uq_model_versions_name_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_type: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    label_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    train_corpus_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    train_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    val_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    test_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    confusion_matrix: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default="candidate", nullable=False)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    output_model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Showcase(TimestampMixin, Base):
    __tablename__ = "showcases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_chat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_rewrite_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class PublicationTarget(TimestampMixin, Base):
    __tablename__ = "publication_targets"
    __table_args__ = (UniqueConstraint("content_item_id", "showcase_id", name="uq_publication_targets_item_showcase"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    content_item_id: Mapped[int] = mapped_column(ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False)
    showcase_id: Mapped[int] = mapped_column(ForeignKey("showcases.id"), nullable=False)
    route_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)


class PublicationDraft(TimestampMixin, Base):
    __tablename__ = "publication_drafts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    publication_target_id: Mapped[int] = mapped_column(ForeignKey("publication_targets.id", ondelete="CASCADE"), nullable=False)
    source_post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id"), nullable=False)
    rewrite_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    rewrite_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_asset_id: Mapped[int | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]", nullable=False)
    risk_flags: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    similarity_to_original: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    validation_errors: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default="draft", nullable=False)


class PublishedPost(Base):
    __tablename__ = "published_posts"
    __table_args__ = (UniqueConstraint("draft_id", "showcase_id", name="uq_published_posts_draft_showcase"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("publication_drafts.id"), nullable=False)
    showcase_id: Mapped[int] = mapped_column(ForeignKey("showcases.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_chat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RewritePromptVersion(Base):
    __tablename__ = "rewrite_prompt_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_rewrite_prompt_versions_name_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="false", index=True, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    common_user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    label_prompts: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    raw_config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PipelineEntry(TimestampMixin, Base):
    __tablename__ = "pipeline_entries"
    __table_args__ = (
        UniqueConstraint("content_item_id", name="uq_pipeline_entries_content_item"),
        UniqueConstraint("source_post_id", name="uq_pipeline_entries_source_post"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), index=True, nullable=False)
    content_item_id: Mapped[int | None] = mapped_column(ForeignKey("content_items.id", ondelete="CASCADE"), nullable=True)
    classification_id: Mapped[int | None] = mapped_column(ForeignKey("post_classifications.id", ondelete="SET NULL"), nullable=True)
    classification_model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    genre_primary: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    genre_secondary: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    difficulty_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    promo_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opinion_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_eligible: Mapped[bool] = mapped_column(Boolean, server_default="false", index=True, nullable=False)
    eligibility_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_allowed: Mapped[bool] = mapped_column(Boolean, server_default="true", index=True, nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    rewrite_prompt_version_id: Mapped[int | None] = mapped_column(ForeignKey("rewrite_prompt_versions.id"), nullable=True)
    latest_draft_id: Mapped[int | None] = mapped_column(ForeignKey("publication_drafts.id", ondelete="SET NULL"), nullable=True)
    published_post_id: Mapped[int | None] = mapped_column(ForeignKey("published_posts.id", ondelete="SET NULL"), nullable=True)
    stage: Mapped[str] = mapped_column(Text, server_default="received", index=True, nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default="rewrite_pending", index=True, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_operation_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True, nullable=False)


class RewriteAttempt(Base):
    __tablename__ = "rewrite_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pipeline_entry_id: Mapped[int] = mapped_column(ForeignKey("pipeline_entries.id", ondelete="CASCADE"), index=True, nullable=False)
    publication_draft_id: Mapped[int | None] = mapped_column(ForeignKey("publication_drafts.id", ondelete="SET NULL"), nullable=True)
    source_post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), index=True, nullable=False)
    prompt_version_id: Mapped[int | None] = mapped_column(ForeignKey("rewrite_prompt_versions.id"), nullable=True)
    rewrite_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    response_raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    risk_flags: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    validation_errors: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ContentPipelineState(Base):
    __tablename__ = "content_pipeline_state"

    post_id: Mapped[int] = mapped_column(ForeignKey("telegram_posts.id", ondelete="CASCADE"), primary_key=True)
    processing_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    link_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    enrichment_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    summary_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    translation_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    material_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    classification_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    routing_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    rewrite_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    publication_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SearchDocument(TimestampMixin, Base):
    __tablename__ = "search_documents"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", name="uq_search_documents_entity"),
        Index("ix_search_documents_tsv", "tsv", postgresql_using="gin"),
        Index("ix_search_documents_labels", "labels", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    source_domain: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    source_chat: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    tsv: Mapped[Any | None] = mapped_column(TSVECTOR, nullable=True)
