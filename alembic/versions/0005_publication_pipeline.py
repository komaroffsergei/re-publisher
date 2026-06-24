from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_publication_pipeline"
down_revision = "0004_codex_supervised"
branch_labels = None
depends_on = None


def jsonb_object() -> sa.TextClause:
    return sa.text("'{}'::jsonb")


def jsonb_array() -> sa.TextClause:
    return sa.text("'[]'::jsonb")


def text_array() -> sa.TextClause:
    return sa.text("'{}'::text[]")


def upgrade() -> None:
    op.create_table(
        "rewrite_prompt_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("common_user_prompt", sa.Text(), nullable=False),
        sa.Column("label_prompts", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("raw_config", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_rewrite_prompt_versions_name_version"),
    )
    op.create_index("ix_rewrite_prompt_versions_is_active", "rewrite_prompt_versions", ["is_active"])

    op.create_table(
        "pipeline_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_item_id", sa.BigInteger(), sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("classification_id", sa.BigInteger(), sa.ForeignKey("post_classifications.id", ondelete="SET NULL"), nullable=True),
        sa.Column("classification_model_version", sa.Text(), nullable=True),
        sa.Column("genre_primary", sa.Text(), nullable=True),
        sa.Column("genre_secondary", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("confidence", sa.Numeric(), nullable=True),
        sa.Column("difficulty_score", sa.Integer(), nullable=True),
        sa.Column("promo_score", sa.Integer(), nullable=True),
        sa.Column("opinion_score", sa.Integer(), nullable=True),
        sa.Column("event_score", sa.Integer(), nullable=True),
        sa.Column("is_eligible", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("eligibility_reason", sa.Text(), nullable=True),
        sa.Column("publication_allowed", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("scheduled_publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rewrite_prompt_version_id", sa.BigInteger(), sa.ForeignKey("rewrite_prompt_versions.id"), nullable=True),
        sa.Column("latest_draft_id", sa.BigInteger(), sa.ForeignKey("publication_drafts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("published_post_id", sa.BigInteger(), sa.ForeignKey("published_posts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.Text(), server_default="rewrite_pending", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("content_item_id", name="uq_pipeline_entries_content_item"),
    )
    op.create_index("ix_pipeline_entries_source_post_id", "pipeline_entries", ["source_post_id"])
    op.create_index("ix_pipeline_entries_genre_primary", "pipeline_entries", ["genre_primary"])
    op.create_index("ix_pipeline_entries_is_eligible", "pipeline_entries", ["is_eligible"])
    op.create_index("ix_pipeline_entries_publication_allowed", "pipeline_entries", ["publication_allowed"])
    op.create_index("ix_pipeline_entries_scheduled_publish_at", "pipeline_entries", ["scheduled_publish_at"])
    op.create_index("ix_pipeline_entries_status", "pipeline_entries", ["status"])

    op.create_table(
        "rewrite_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("pipeline_entry_id", sa.BigInteger(), sa.ForeignKey("pipeline_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("publication_draft_id", sa.BigInteger(), sa.ForeignKey("publication_drafts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("prompt_version_id", sa.BigInteger(), sa.ForeignKey("rewrite_prompt_versions.id"), nullable=True),
        sa.Column("rewrite_model", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("request_meta", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("response_raw", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("risk_flags", postgresql.JSONB(), server_default=jsonb_array(), nullable=False),
        sa.Column("validation_errors", postgresql.JSONB(), server_default=jsonb_array(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_rewrite_attempts_pipeline_entry_id", "rewrite_attempts", ["pipeline_entry_id"])
    op.create_index("ix_rewrite_attempts_source_post_id", "rewrite_attempts", ["source_post_id"])


def downgrade() -> None:
    op.drop_index("ix_rewrite_attempts_source_post_id", table_name="rewrite_attempts")
    op.drop_index("ix_rewrite_attempts_pipeline_entry_id", table_name="rewrite_attempts")
    op.drop_table("rewrite_attempts")
    op.drop_index("ix_pipeline_entries_status", table_name="pipeline_entries")
    op.drop_index("ix_pipeline_entries_scheduled_publish_at", table_name="pipeline_entries")
    op.drop_index("ix_pipeline_entries_publication_allowed", table_name="pipeline_entries")
    op.drop_index("ix_pipeline_entries_is_eligible", table_name="pipeline_entries")
    op.drop_index("ix_pipeline_entries_genre_primary", table_name="pipeline_entries")
    op.drop_index("ix_pipeline_entries_source_post_id", table_name="pipeline_entries")
    op.drop_table("pipeline_entries")
    op.drop_index("ix_rewrite_prompt_versions_is_active", table_name="rewrite_prompt_versions")
    op.drop_table("rewrite_prompt_versions")
