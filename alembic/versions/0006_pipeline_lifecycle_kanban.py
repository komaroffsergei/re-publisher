from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_pipeline_lifecycle_kanban"
down_revision = "0005_publication_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("pipeline_entries", "content_item_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("pipeline_entries", sa.Column("stage", sa.Text(), server_default="received", nullable=False))
    op.add_column(
        "pipeline_entries",
        sa.Column("last_operation_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_unique_constraint("uq_pipeline_entries_source_post", "pipeline_entries", ["source_post_id"])
    op.create_index("ix_pipeline_entries_stage", "pipeline_entries", ["stage"])
    op.create_index("ix_pipeline_entries_last_operation_at", "pipeline_entries", ["last_operation_at"])

    op.execute(
        """
        UPDATE pipeline_entries
        SET
            stage = CASE
                WHEN published_post_id IS NOT NULL OR status = 'published' THEN 'published'
                WHEN status = 'ready_for_publication' THEN 'ready'
                WHEN latest_draft_id IS NOT NULL THEN 'rewritten'
                WHEN classification_id IS NOT NULL AND is_eligible IS TRUE THEN 'enriched'
                WHEN classification_id IS NOT NULL THEN 'sorted'
                ELSE 'received'
            END,
            last_operation_at = COALESCE(updated_at, created_at, now())
        """
    )

    op.execute(
        """
        INSERT INTO pipeline_entries (
            source_post_id,
            content_item_id,
            stage,
            status,
            publication_allowed,
            is_eligible,
            genre_secondary,
            created_at,
            updated_at,
            last_operation_at
        )
        SELECT
            p.id,
            NULL,
            'received',
            'received',
            true,
            false,
            '{}'::text[],
            now(),
            now(),
            COALESCE(p.created_at, now())
        FROM telegram_posts p
        JOIN telegram_chats c ON c.peer_id = p.chat_peer_id
        LEFT JOIN pipeline_entries e ON e.source_post_id = p.id
        WHERE e.id IS NULL
          AND p.is_deleted IS FALSE
          AND c.folder_name = 'MAX'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_entries_last_operation_at", table_name="pipeline_entries")
    op.drop_index("ix_pipeline_entries_stage", table_name="pipeline_entries")
    op.drop_constraint("uq_pipeline_entries_source_post", "pipeline_entries", type_="unique")
    op.drop_column("pipeline_entries", "last_operation_at")
    op.drop_column("pipeline_entries", "stage")
    op.alter_column("pipeline_entries", "content_item_id", existing_type=sa.BigInteger(), nullable=False)
