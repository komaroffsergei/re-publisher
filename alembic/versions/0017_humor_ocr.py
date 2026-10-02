"""Separate OCR inputs and humor profile; retain existing model/filter history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017_humor_ocr"
down_revision = "0016_max_publication_review"
branch_labels = depends_on = None


def upgrade():
    for table in ("taxonomy_classifications", "taxonomy_runs", "selection_filter_versions"):
        op.add_column(table, sa.Column("profile", sa.Text(), nullable=False, server_default="taxonomy"))
    for table in ("taxonomy_classifications", "taxonomy_runs"):
        op.add_column(table, sa.Column("input_sha256", sa.Text()))
        op.add_column(table, sa.Column("ocr_run_id", sa.BigInteger()))
    op.drop_constraint("uq_taxonomy_classifications_entry_model", "taxonomy_classifications", type_="unique")
    op.create_unique_constraint("uq_taxonomy_classifications_entry_model_profile", "taxonomy_classifications",
                                ["pipeline_entry_id", "model_key", "profile"])
    op.add_column("telegram_posts", sa.Column("ocr_preview_path", sa.Text()))
    op.add_column("telegram_posts", sa.Column("ocr_preview_status", sa.Text(), nullable=False, server_default="missing"))
    op.create_table("ocr_jobs",
        sa.Column("entry_id", sa.BigInteger(), sa.ForeignKey("pipeline_entries.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_sha256", sa.Text(), nullable=False), sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_run_id", sa.BigInteger()), sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_at", sa.DateTime(timezone=True)), sa.Column("error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_ocr_jobs_status", "ocr_jobs", ["status"])
    op.create_table("ocr_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("entry_id", sa.BigInteger(), sa.ForeignKey("pipeline_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False), sa.Column("input_sha256", sa.Text()),
        sa.Column("engine_version", sa.Text()), sa.Column("status", sa.Text(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("results", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text()), sa.Column("elapsed_ms", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_ocr_runs_entry_id", "ocr_runs", ["entry_id"])


def downgrade():
    raise RuntimeError("Retain OCR and classification history; roll back images without lowering schema")
