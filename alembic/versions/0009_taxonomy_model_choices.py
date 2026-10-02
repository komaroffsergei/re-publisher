"""Keep TF-IDF and MiniLM results separately; reserve one row for media-only posts.

Revision ID: 0009_taxonomy_model_choices
Revises: 0008_taxonomy_classifications
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_taxonomy_model_choices"
down_revision = "0008_taxonomy_classifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("taxonomy_classifications", sa.Column("model_key", sa.Text(), server_default="tfidf", nullable=False))
    op.execute("UPDATE taxonomy_classifications SET model_key = 'media' WHERE status = 'media_only'")
    op.drop_constraint("uq_taxonomy_classifications_entry", "taxonomy_classifications", type_="unique")
    op.create_unique_constraint(
        "uq_taxonomy_classifications_entry_model", "taxonomy_classifications", ["pipeline_entry_id", "model_key"]
    )
    op.create_index("ix_taxonomy_classifications_model_status", "taxonomy_classifications", ["model_key", "status"])


def downgrade() -> None:
    # A post may have results from both models. Dropping either would lose data.
    raise RuntimeError("Downgrade requires an explicit decision about the two classification results")
