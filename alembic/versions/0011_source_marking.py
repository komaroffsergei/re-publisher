"""Store marked post output and retire the old intermediate board stages."""

from alembic import op
import sqlalchemy as sa

revision = "0011_source_marking"
down_revision = "0010_taxonomy_run_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipeline_entries", sa.Column("marked_text", sa.Text(), nullable=True))
    op.add_column("pipeline_entries", sa.Column("marked_source_url", sa.Text(), nullable=True))
    op.add_column("pipeline_entries", sa.Column("marked_text_sha256", sa.Text(), nullable=True))
    op.add_column("pipeline_entries", sa.Column("marked_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE pipeline_entries SET stage = 'sorted' WHERE stage IN ('enriched', 'rewritten')")


def downgrade() -> None:
    op.drop_column("pipeline_entries", "marked_at")
    op.drop_column("pipeline_entries", "marked_text_sha256")
    op.drop_column("pipeline_entries", "marked_source_url")
    op.drop_column("pipeline_entries", "marked_text")
