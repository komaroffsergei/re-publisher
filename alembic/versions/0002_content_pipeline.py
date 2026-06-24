from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_content_pipeline"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def jsonb_object() -> sa.TextClause:
    return sa.text("'{}'::jsonb")


def jsonb_array() -> sa.TextClause:
    return sa.text("'[]'::jsonb")


def text_array() -> sa.TextClause:
    return sa.text("'{}'::text[]")


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "post_processed",
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("clean_text", sa.Text(), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("text_hash", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column("emoji_count", sa.Integer(), nullable=True),
        sa.Column("url_count", sa.Integer(), nullable=True),
        sa.Column("domains", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("hashtags", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("mentions", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("has_code", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("has_github", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("has_arxiv", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("has_media", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_post_processed_text_hash", "post_processed", ["text_hash"])
    op.create_index("ix_post_processed_language", "post_processed", ["language"])
    op.create_index("ix_post_processed_processed_at", "post_processed", ["processed_at"])

    op.create_table(
        "post_links",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("url_type", sa.Text(), nullable=True),
        sa.Column("position_index", sa.Integer(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("extraction_status", sa.Text(), server_default="pending", nullable=False),
        *timestamps(),
        sa.UniqueConstraint("post_id", "original_url", name="uq_post_links_post_original_url"),
    )
    op.create_index("ix_post_links_domain", "post_links", ["domain"])
    op.create_index("ix_post_links_canonical_url", "post_links", ["canonical_url"])
    op.create_index("ix_post_links_is_primary", "post_links", ["is_primary"])

    op.create_table(
        "media_assets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id"), nullable=True),
        sa.Column("source_link_id", sa.BigInteger(), sa.ForeignKey("post_links.id"), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("storage_url", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("download_status", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        *timestamps(),
    )
    op.create_index(
        "uq_media_assets_sha256_not_null",
        "media_assets",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text("sha256 IS NOT NULL"),
    )

    op.create_table(
        "link_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("link_id", sa.BigInteger(), sa.ForeignKey("post_links.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("site_name", sa.Text(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extracted_text_hash", sa.Text(), nullable=True),
        sa.Column("extraction_method", sa.Text(), nullable=True),
        sa.Column("extraction_quality_score", sa.Numeric(), nullable=True),
        sa.Column("summary_short", sa.Text(), nullable=True),
        sa.Column("summary_model", sa.Text(), nullable=True),
        sa.Column("summary_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("image_asset_id", sa.BigInteger(), nullable=True),
        sa.Column("raw_metadata", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        *timestamps(),
        sa.UniqueConstraint("link_id", name="uq_link_snapshots_link_id"),
    )

    op.create_table(
        "content_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("primary_link_id", sa.BigInteger(), sa.ForeignKey("post_links.id"), nullable=True),
        sa.Column("primary_snapshot_id", sa.BigInteger(), sa.ForeignKey("link_snapshots.id"), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("main_text", sa.Text(), nullable=True),
        sa.Column("source_summary", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_domain", sa.Text(), nullable=True),
        sa.Column("source_lang", sa.Text(), nullable=True),
        sa.Column("target_lang", sa.Text(), server_default="ru", nullable=False),
        sa.Column("translated_title", sa.Text(), nullable=True),
        sa.Column("translated_summary", sa.Text(), nullable=True),
        sa.Column("primary_image_asset_id", sa.BigInteger(), sa.ForeignKey("media_assets.id"), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("quality_score", sa.Numeric(), nullable=True),
        sa.Column("duplicate_group_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), server_default="ready", nullable=False),
        *timestamps(),
        sa.UniqueConstraint("source_post_id", name="uq_content_items_source_post_id"),
    )

    op.create_table(
        "post_classifications",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_item_id", sa.BigInteger(), sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=True),
        sa.Column("classifier_name", sa.Text(), nullable=False),
        sa.Column("classifier_version", sa.Text(), nullable=False),
        sa.Column("label_primary", sa.Text(), nullable=True),
        sa.Column("label_secondary", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("label_scores", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("confidence", sa.Numeric(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("needs_review", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("post_id", "classifier_name", "classifier_version", name="uq_post_classifications_model"),
    )
    op.create_index("ix_post_classifications_label_primary", "post_classifications", ["label_primary"])
    op.create_index("ix_post_classifications_confidence", "post_classifications", ["confidence"])
    op.create_index("ix_post_classifications_needs_review", "post_classifications", ["needs_review"])

    op.create_table(
        "post_labels",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("label_set_version", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="proposed", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
    )

    op.create_table(
        "labeling_queue",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("suggested_label", sa.Text(), nullable=True),
        sa.Column("suggested_confidence", sa.Numeric(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("human_label", sa.Text(), nullable=True),
        sa.Column("reviewer", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
    )

    op.create_table(
        "model_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("model_type", sa.Text(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("label_schema_version", sa.Text(), nullable=False),
        sa.Column("train_corpus_hash", sa.Text(), nullable=True),
        sa.Column("train_size", sa.Integer(), nullable=True),
        sa.Column("val_size", sa.Integer(), nullable=True),
        sa.Column("test_size", sa.Integer(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("confusion_matrix", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("status", sa.Text(), server_default="candidate", nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("model_name", "model_version", name="uq_model_versions_name_version"),
    )

    op.create_table(
        "training_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text(), unique=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("input_stats", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("output_model_version", sa.Text(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("report_path", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )

    op.create_table(
        "showcases",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.Text(), unique=True, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_chat_id", sa.Text(), nullable=True),
        sa.Column("target_username", sa.Text(), nullable=True),
        sa.Column("default_rewrite_template", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *timestamps(),
    )

    op.create_table(
        "publication_targets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("content_item_id", sa.BigInteger(), sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("showcase_id", sa.BigInteger(), sa.ForeignKey("showcases.id"), nullable=False),
        sa.Column("route_reason", sa.Text(), nullable=True),
        sa.Column("route_score", sa.Numeric(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        *timestamps(),
        sa.UniqueConstraint("content_item_id", "showcase_id", name="uq_publication_targets_item_showcase"),
    )

    op.create_table(
        "publication_drafts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("publication_target_id", sa.BigInteger(), sa.ForeignKey("publication_targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id"), nullable=False),
        sa.Column("rewrite_model", sa.Text(), nullable=True),
        sa.Column("rewrite_template", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_domain", sa.Text(), nullable=True),
        sa.Column("image_asset_id", sa.BigInteger(), sa.ForeignKey("media_assets.id"), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("claims", postgresql.JSONB(), server_default=jsonb_array(), nullable=False),
        sa.Column("risk_flags", postgresql.JSONB(), server_default=jsonb_array(), nullable=False),
        sa.Column("similarity_to_original", sa.Numeric(), nullable=True),
        sa.Column("validation_errors", postgresql.JSONB(), server_default=jsonb_array(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        *timestamps(),
    )

    op.create_table(
        "published_posts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("draft_id", sa.BigInteger(), sa.ForeignKey("publication_drafts.id"), nullable=False),
        sa.Column("showcase_id", sa.BigInteger(), sa.ForeignKey("showcases.id"), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_chat_id", sa.Text(), nullable=True),
        sa.Column("target_message_id", sa.Text(), nullable=True),
        sa.Column("published_url", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("draft_id", "showcase_id", name="uq_published_posts_draft_showcase"),
    )

    op.create_table(
        "content_pipeline_state",
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("processing_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("link_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("enrichment_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("summary_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("translation_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("material_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("classification_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("routing_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("rewrite_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("publication_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "search_documents",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.Column("labels", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("source_domain", sa.Text(), nullable=True),
        sa.Column("source_chat", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("tsv", postgresql.TSVECTOR(), nullable=True),
        *timestamps(),
        sa.UniqueConstraint("entity_type", "entity_id", name="uq_search_documents_entity"),
    )
    op.create_index("ix_search_documents_tsv", "search_documents", ["tsv"], postgresql_using="gin")
    op.create_index("ix_search_documents_labels", "search_documents", ["labels"], postgresql_using="gin")
    op.create_index("ix_search_documents_source_domain", "search_documents", ["source_domain"])
    op.create_index("ix_search_documents_source_chat", "search_documents", ["source_chat"])
    op.create_index("ix_search_documents_created_at", "search_documents", ["created_at"])

    op.execute(
        """
        INSERT INTO showcases (slug, title, description, target_type, default_rewrite_template)
        VALUES
            ('ai_news', 'AI News', 'News and announcements', 'telegram', 'news_short'),
            ('ai_technical', 'AI Technical', 'Technical deep dives and engineering notes', 'telegram', 'technical_explainer'),
            ('ai_tools', 'AI Tools', 'AI tools and products', 'telegram', 'tool_card'),
            ('ai_research', 'AI Research', 'Research papers and methods', 'telegram', 'research_note'),
            ('ai_business', 'AI Business', 'Business and market stories', 'telegram', 'business_brief'),
            ('ai_education', 'AI Education', 'Guides and explainers', 'telegram', 'education_guide'),
            ('ai_humor', 'AI Humor', 'Humor and memes', 'telegram', 'humor_short'),
            ('ai_jobs_events', 'AI Jobs & Events', 'Jobs, events, promos and career posts', 'telegram', 'news_short'),
            ('quarantine', 'Quarantine', 'Items that need manual review', 'review', 'news_short')
        ON CONFLICT (slug) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_search_documents_created_at", table_name="search_documents")
    op.drop_index("ix_search_documents_source_chat", table_name="search_documents")
    op.drop_index("ix_search_documents_source_domain", table_name="search_documents")
    op.drop_index("ix_search_documents_labels", table_name="search_documents")
    op.drop_index("ix_search_documents_tsv", table_name="search_documents")
    op.drop_table("search_documents")
    op.drop_table("content_pipeline_state")
    op.drop_table("published_posts")
    op.drop_table("publication_drafts")
    op.drop_table("publication_targets")
    op.drop_table("showcases")
    op.drop_table("training_runs")
    op.drop_table("model_versions")
    op.drop_table("labeling_queue")
    op.drop_table("post_labels")
    op.drop_index("ix_post_classifications_needs_review", table_name="post_classifications")
    op.drop_index("ix_post_classifications_confidence", table_name="post_classifications")
    op.drop_index("ix_post_classifications_label_primary", table_name="post_classifications")
    op.drop_table("post_classifications")
    op.drop_table("content_items")
    op.drop_table("link_snapshots")
    op.drop_index("uq_media_assets_sha256_not_null", table_name="media_assets")
    op.drop_table("media_assets")
    op.drop_index("ix_post_links_is_primary", table_name="post_links")
    op.drop_index("ix_post_links_canonical_url", table_name="post_links")
    op.drop_index("ix_post_links_domain", table_name="post_links")
    op.drop_table("post_links")
    op.drop_index("ix_post_processed_processed_at", table_name="post_processed")
    op.drop_index("ix_post_processed_language", table_name="post_processed")
    op.drop_index("ix_post_processed_text_hash", table_name="post_processed")
    op.drop_table("post_processed")
