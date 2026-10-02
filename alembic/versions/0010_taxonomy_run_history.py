"""Preserve every taxonomy run and seed the latest pre-migration result.

Revision ID: 0010_taxonomy_run_history
Revises: 0009_taxonomy_model_choices
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0010_taxonomy_run_history"
down_revision = "0009_taxonomy_model_choices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "taxonomy_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("pipeline_entry_id", sa.BigInteger(), sa.ForeignKey("pipeline_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_key", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("origin", sa.Text(), server_default="run", nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_taxonomy_runs_entry_id", "taxonomy_runs", ["pipeline_entry_id", "id"])
    op.add_column("taxonomy_classifications", sa.Column("current_run_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("fk_taxonomy_classifications_current_run", "taxonomy_classifications",
                          "taxonomy_runs", ["current_run_id"], ["id"], ondelete="SET NULL")
    # Earlier reruns overwrote their rows. Only the latest surviving state can be copied.
    op.execute("""
        WITH snapshots AS (
            INSERT INTO taxonomy_runs (
                pipeline_entry_id, source_post_id, model_key, text_sha256, model_version,
                status, result, error, elapsed_ms, origin, queued_at, started_at, finished_at
            )
            SELECT pipeline_entry_id, source_post_id, model_key, text_sha256, model_version,
                   status, result, error, elapsed_ms, 'legacy_snapshot', created_at,
                   started_at, finished_at
            FROM taxonomy_classifications
            RETURNING id, pipeline_entry_id, model_key
        )
        UPDATE taxonomy_classifications AS classification
        SET current_run_id = snapshots.id
        FROM snapshots
        WHERE classification.pipeline_entry_id = snapshots.pipeline_entry_id
          AND classification.model_key = snapshots.model_key
    """)


def downgrade() -> None:
    raise RuntimeError("Downgrade would discard classification run history")
