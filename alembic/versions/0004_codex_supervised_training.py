from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_codex_supervised"
down_revision = "0003_yandex_genre"
branch_labels = None
depends_on = None


def jsonb_object() -> sa.TextClause:
    return sa.text("'{}'::jsonb")


def text_array() -> sa.TextClause:
    return sa.text("'{}'::text[]")


def upgrade() -> None:
    op.create_table(
        "codex_genre_classifications",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_item_id", sa.BigInteger(), sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.Column("teacher_name", sa.Text(), nullable=False),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("genre_primary", sa.Text(), nullable=False),
        sa.Column("genre_secondary", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("genre_confidence", sa.Numeric(), nullable=True),
        sa.Column("difficulty_score", sa.Integer(), nullable=False),
        sa.Column("promo_score", sa.Integer(), nullable=False),
        sa.Column("opinion_score", sa.Integer(), nullable=False),
        sa.Column("event_score", sa.Integer(), nullable=False),
        sa.Column("needs_review", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("source_post_id", "run_id", name="uq_codex_genre_post_run"),
    )
    op.create_index("ix_codex_genre_classifications_run_id", "codex_genre_classifications", ["run_id"])
    op.create_index("ix_codex_genre_classifications_genre_primary", "codex_genre_classifications", ["genre_primary"])
    op.create_index("ix_codex_genre_classifications_split", "codex_genre_classifications", ["split"])

    op.create_table(
        "codex_genre_model_comparisons",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_item_id", sa.BigInteger(), sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("teacher_genre", sa.Text(), nullable=False),
        sa.Column("teacher_secondary", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("teacher_axes", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("predicted_genre", sa.Text(), nullable=False),
        sa.Column("predicted_secondary", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("predicted_axes", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("label_scores", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("match_percent", sa.Numeric(), nullable=False),
        sa.Column("mismatch_flags", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("source_post_id", "run_id", "model_version", name="uq_codex_comparison_post_model_run"),
    )
    op.create_index("ix_codex_genre_model_comparisons_run_id", "codex_genre_model_comparisons", ["run_id"])
    op.create_index("ix_codex_genre_model_comparisons_model_version", "codex_genre_model_comparisons", ["model_version"])
    op.create_index("ix_codex_genre_model_comparisons_match_percent", "codex_genre_model_comparisons", ["match_percent"])

    op.create_table(
        "codex_training_runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_iteration", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_hours", sa.Numeric(), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("report_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("latest_model_version", sa.Text(), nullable=True),
        sa.Column("best_match_percent", sa.Numeric(), nullable=True),
        sa.Column("worst_match_percent", sa.Numeric(), nullable=True),
        sa.Column("latest_match_percent", sa.Numeric(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("report_path", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("codex_training_runs")
    op.drop_index("ix_codex_genre_model_comparisons_match_percent", table_name="codex_genre_model_comparisons")
    op.drop_index("ix_codex_genre_model_comparisons_model_version", table_name="codex_genre_model_comparisons")
    op.drop_index("ix_codex_genre_model_comparisons_run_id", table_name="codex_genre_model_comparisons")
    op.drop_table("codex_genre_model_comparisons")
    op.drop_index("ix_codex_genre_classifications_split", table_name="codex_genre_classifications")
    op.drop_index("ix_codex_genre_classifications_genre_primary", table_name="codex_genre_classifications")
    op.drop_index("ix_codex_genre_classifications_run_id", table_name="codex_genre_classifications")
    op.drop_table("codex_genre_classifications")
