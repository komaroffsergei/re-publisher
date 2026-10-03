"""Keep caption, OCR and legacy combined runs independent."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018_separate_ocr_scores"
down_revision = "0017_humor_ocr"
branch_labels = depends_on = None


def upgrade():
    for table in ("taxonomy_classifications", "taxonomy_runs"):
        op.add_column(table, sa.Column("input_source", sa.Text(), nullable=False, server_default="text"))
        op.execute(sa.text(f"UPDATE {table} SET input_source='combined' WHERE profile='humor_ocr'"))
    op.drop_constraint("uq_taxonomy_classifications_entry_model_profile", "taxonomy_classifications", type_="unique")
    op.create_unique_constraint("uq_taxonomy_entry_model_profile_source", "taxonomy_classifications",
                               ["pipeline_entry_id", "model_key", "profile", "input_source"])
    op.add_column("selection_filter_versions", sa.Column("requires_ocr", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("filter_evaluations", sa.Column("run_ids", postgresql.JSONB(), nullable=False, server_default="{}"))
    for name in ("completed_inputs", "total_inputs"):
        op.add_column("ocr_runs", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    raise RuntimeError("Keep result history; roll back images without lowering schema")
