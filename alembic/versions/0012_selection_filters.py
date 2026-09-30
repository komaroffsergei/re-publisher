"""Independent selection rules, dictionary and persistent post marks."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_selection_filters"
down_revision = "0011_source_marking"
branch_labels = depends_on = None


def timestamp(name):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def pk():
    return sa.Column("id", sa.BigInteger(), primary_key=True)


def ref(name, target, nullable=False, ondelete=None):
    return sa.Column(name, sa.BigInteger(), sa.ForeignKey(target, ondelete=ondelete), nullable=nullable)


def upgrade():
    op.create_table("filter_marks", pk(), sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("color", sa.Text(), nullable=False, server_default="#a78bfa"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default="false"), timestamp("created_at"), timestamp("updated_at"))
    op.create_table("selection_filters", pk(), sa.Column("name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("active_version_id", sa.BigInteger(), nullable=True), timestamp("created_at"), timestamp("updated_at"))
    op.create_table("selection_filter_versions", pk(), ref("filter_id", "selection_filters.id", ondelete="CASCADE"),
        sa.Column("number", sa.Integer(), nullable=False), sa.Column("name", sa.Text(), nullable=False),
        sa.Column("model_key", sa.Text(), nullable=False), ref("mark_id", "filter_marks.id"),
        sa.Column("expression", JSONB(), nullable=False), timestamp("created_at"),
        sa.UniqueConstraint("filter_id", "number", name="uq_selection_filter_version"))
    op.create_foreign_key("fk_selection_filter_active_version", "selection_filters", "selection_filter_versions", ["active_version_id"], ["id"])
    op.create_table("filter_evaluations", pk(), ref("entry_id", "pipeline_entries.id", ondelete="CASCADE"),
        ref("version_id", "selection_filter_versions.id"), ref("run_id", "taxonomy_runs.id", nullable=True, ondelete="SET NULL"),
        sa.Column("text_sha256", sa.Text(), nullable=False), sa.Column("input_key", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False), sa.Column("trace", JSONB(), nullable=False), timestamp("created_at"),
        sa.UniqueConstraint("entry_id", "version_id", "input_key", name="uq_filter_evaluation_input"))
    op.create_table("filter_applications", pk(), ref("version_id", "selection_filter_versions.id"),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("last_entry_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("max_entry_id", sa.BigInteger(), nullable=False),
        *(sa.Column(name, sa.Integer(), nullable=False, server_default="0") for name in ("processed", "matched", "unknown", "backfilled")),
        sa.Column("error", sa.Text(), nullable=True), timestamp("created_at"), timestamp("updated_at"))
    op.create_table("post_filter_marks", pk(), ref("entry_id", "pipeline_entries.id", ondelete="CASCADE"),
        ref("mark_id", "filter_marks.id"), sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        timestamp("assigned_at"), sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("entry_id", "mark_id", name="uq_post_filter_mark"))
    op.create_table("filter_mark_events", pk(), ref("entry_id", "pipeline_entries.id", ondelete="CASCADE"),
        ref("mark_id", "filter_marks.id"), ref("evaluation_id", "filter_evaluations.id", nullable=True),
        sa.Column("action", sa.Text(), nullable=False), sa.Column("dedup_key", sa.Text(), nullable=False, unique=True), timestamp("created_at"))
    for table, columns in {"selection_filter_versions": ["filter_id"], "filter_evaluations": ["entry_id", "version_id"],
                           "filter_applications": ["version_id"], "post_filter_marks": ["entry_id", "mark_id"],
                           "filter_mark_events": ["entry_id"]}.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    op.execute("UPDATE pipeline_entries SET stage='sorted' WHERE stage='filtered'")
    for table in ("filter_mark_events", "post_filter_marks", "filter_applications", "filter_evaluations"):
        op.drop_table(table)
    op.drop_constraint("fk_selection_filter_active_version", "selection_filters", type_="foreignkey")
    for table in ("selection_filter_versions", "selection_filters", "filter_marks"):
        op.drop_table(table)
