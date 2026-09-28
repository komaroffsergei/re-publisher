"""Add collector media state.

Revision ID: 0007_collector_media_state
Revises: 0006_pipeline_lifecycle_kanban
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_collector_media_state"
down_revision = "0006_pipeline_lifecycle_kanban"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("telegram_posts", sa.Column("media_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column(
        "telegram_posts",
        sa.Column("media_download_status", sa.Text(), server_default="missing", nullable=False),
    )
    op.add_column("telegram_posts", sa.Column("media_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("telegram_posts", "media_error")
    op.drop_column("telegram_posts", "media_download_status")
    op.drop_column("telegram_posts", "media_size_bytes")

