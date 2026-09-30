"""Assign taxonomy labels with their model scores; retain existing mark IDs."""
from alembic import op
import sqlalchemy as sa

revision = "0013_model_filter_marks"
down_revision = "0012_selection_filters"
branch_labels = depends_on = None


def upgrade():
    op.add_column("filter_marks", sa.Column("label_id", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_filter_marks_label_id", "filter_marks", ["label_id"])
    op.add_column("selection_filter_versions", sa.Column("assigned_label_id", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("selection_filter_versions", "assigned_label_id")
    op.drop_constraint("uq_filter_marks_label_id", "filter_marks", type_="unique")
    op.drop_column("filter_marks", "label_id")
