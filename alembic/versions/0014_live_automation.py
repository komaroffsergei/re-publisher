"""New-only cursors, service liveness and automatic preparation state."""

from alembic import op
import sqlalchemy as sa

revision = "0014_live_automation"
down_revision = "0013_model_filter_marks"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "telegram_sync_state",
        sa.Column("live_member", sa.Boolean(), server_default="false", nullable=False),
    )
    for name, kind in [
        ("live_since", sa.DateTime(timezone=True)),
        ("reconciled_message_id", sa.BigInteger()),
    ]:
        op.add_column("telegram_sync_state", sa.Column(name, kind, nullable=True))
    for name, kind, default, nullable in [
        ("auto_enabled", sa.Boolean(), "false", False),
        ("auto_state", sa.Text(), "pending", False),
        ("auto_attempts", sa.Integer(), "0", False),
        ("auto_retry_at", sa.DateTime(timezone=True), None, True),
        ("auto_phase", sa.Text(), None, True),
        ("auto_manual_mark", sa.Boolean(), "false", False),
        ("ready_at", sa.DateTime(timezone=True), None, True),
    ]:
        op.add_column(
            "pipeline_entries",
            sa.Column(name, kind, server_default=default, nullable=nullable),
        )
    op.create_index(
        "ix_pipeline_entries_auto_enabled", "pipeline_entries", ["auto_enabled"]
    )
    op.create_table(
        "service_runtime",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_table("service_runtime")
    op.drop_index("ix_pipeline_entries_auto_enabled", "pipeline_entries")
    for name in (
        "ready_at",
        "auto_manual_mark",
        "auto_phase",
        "auto_retry_at",
        "auto_attempts",
        "auto_state",
        "auto_enabled",
    ):
        op.drop_column("pipeline_entries", name)
    op.drop_column("telegram_sync_state", "reconciled_message_id")
    op.drop_column("telegram_sync_state", "live_since")
    op.drop_column("telegram_sync_state", "live_member")
