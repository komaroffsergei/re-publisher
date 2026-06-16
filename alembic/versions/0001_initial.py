from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_chats",
        sa.Column("peer_id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("chat_type", sa.Text(), nullable=False),
        sa.Column("folder_name", sa.Text(), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "telegram_posts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_peer_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_peer_id", sa.BigInteger(), nullable=True),
        sa.Column("date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edit_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("grouped_id", sa.BigInteger(), nullable=True),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("forwards", sa.Integer(), nullable=True),
        sa.Column("replies_count", sa.Integer(), nullable=True),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("media_path", sa.Text(), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("chat_peer_id", "message_id", name="uq_telegram_posts_chat_message"),
    )
    op.create_index("ix_telegram_posts_chat_peer_id", "telegram_posts", ["chat_peer_id"])
    op.create_index("ix_telegram_posts_date", "telegram_posts", ["date"])
    op.create_index("ix_telegram_posts_grouped_id", "telegram_posts", ["grouped_id"])

    op.create_table(
        "telegram_comments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("post_chat_peer_id", sa.BigInteger(), nullable=False),
        sa.Column("post_message_id", sa.BigInteger(), nullable=False),
        sa.Column("discussion_peer_id", sa.BigInteger(), nullable=True),
        sa.Column("comment_message_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_comment_message_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_peer_id", sa.BigInteger(), nullable=True),
        sa.Column("date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edit_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("media_path", sa.Text(), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("discussion_peer_id", "comment_message_id", name="uq_telegram_comments_discussion_message"),
    )
    op.create_index(
        "ix_telegram_comments_post",
        "telegram_comments",
        ["post_chat_peer_id", "post_message_id"],
    )
    op.create_index("ix_telegram_comments_discussion_peer_id", "telegram_comments", ["discussion_peer_id"])
    op.create_index("ix_telegram_comments_date", "telegram_comments", ["date"])

    op.create_table(
        "telegram_sync_state",
        sa.Column("chat_peer_id", sa.BigInteger(), primary_key=True),
        sa.Column("last_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("telegram_sync_state")
    op.drop_index("ix_telegram_comments_date", table_name="telegram_comments")
    op.drop_index("ix_telegram_comments_discussion_peer_id", table_name="telegram_comments")
    op.drop_index("ix_telegram_comments_post", table_name="telegram_comments")
    op.drop_table("telegram_comments")
    op.drop_index("ix_telegram_posts_grouped_id", table_name="telegram_posts")
    op.drop_index("ix_telegram_posts_date", table_name="telegram_posts")
    op.drop_index("ix_telegram_posts_chat_peer_id", table_name="telegram_posts")
    op.drop_table("telegram_posts")
    op.drop_table("telegram_chats")
