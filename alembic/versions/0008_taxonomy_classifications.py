"""Store Codex-trained taxonomy jobs independently of legacy classifications.

Revision ID: 0008_taxonomy_classifications
Revises: 0007_collector_media_state
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0008_taxonomy_classifications"
down_revision = "0007_collector_media_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "taxonomy_classifications",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("pipeline_entry_id", sa.BigInteger(), sa.ForeignKey("pipeline_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text_sha256", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("pipeline_entry_id", name="uq_taxonomy_classifications_entry"),
    )
    op.create_index("ix_taxonomy_classifications_status", "taxonomy_classifications", ["status"])
    op.create_index("ix_taxonomy_classifications_source_post_id", "taxonomy_classifications", ["source_post_id"])


def downgrade() -> None:
    op.drop_index("ix_taxonomy_classifications_source_post_id", table_name="taxonomy_classifications")
    op.drop_index("ix_taxonomy_classifications_status", table_name="taxonomy_classifications")
    op.drop_table("taxonomy_classifications")
