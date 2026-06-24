from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_yandex_genre"
down_revision = "0002_content_pipeline"
branch_labels = None
depends_on = None


def jsonb_object() -> sa.TextClause:
    return sa.text("'{}'::jsonb")


def text_array() -> sa.TextClause:
    return sa.text("'{}'::text[]")


def upgrade() -> None:
    op.create_table(
        "yandex_genre_classifications",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_post_id", sa.BigInteger(), sa.ForeignKey("telegram_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_item_id", sa.BigInteger(), sa.ForeignKey("content_items.id", ondelete="CASCADE"), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.Column("genre_primary", sa.Text(), nullable=False),
        sa.Column("genre_secondary", postgresql.ARRAY(sa.Text()), server_default=text_array(), nullable=False),
        sa.Column("genre_confidence", sa.Numeric(), nullable=True),
        sa.Column("difficulty_score", sa.Integer(), nullable=False),
        sa.Column("promo_score", sa.Integer(), nullable=False),
        sa.Column("opinion_score", sa.Integer(), nullable=False),
        sa.Column("event_score", sa.Integer(), nullable=False),
        sa.Column("needs_review", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("usage", postgresql.JSONB(), server_default=jsonb_object(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("source_post_id", "run_id", name="uq_yandex_genre_post_run"),
    )
    op.create_index("ix_yandex_genre_classifications_genre_primary", "yandex_genre_classifications", ["genre_primary"])
    op.create_index("ix_yandex_genre_classifications_needs_review", "yandex_genre_classifications", ["needs_review"])
    op.create_index("ix_yandex_genre_classifications_run_id", "yandex_genre_classifications", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_yandex_genre_classifications_run_id", table_name="yandex_genre_classifications")
    op.drop_index("ix_yandex_genre_classifications_needs_review", table_name="yandex_genre_classifications")
    op.drop_index("ix_yandex_genre_classifications_genre_primary", table_name="yandex_genre_classifications")
    op.drop_table("yandex_genre_classifications")
