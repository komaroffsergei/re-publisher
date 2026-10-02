"""Retain an explicit hold without creating a MAX send intent."""
from alembic import op

revision = '0016_max_publication_review'
down_revision = '0015_max_publications'
branch_labels = depends_on = None


def upgrade():
    op.drop_constraint('ck_max_delivery_status', 'max_publication_deliveries', type_='check')
    op.create_check_constraint('ck_max_delivery_status', 'max_publication_deliveries',
        "status IN ('queued','preparing','sending','verifying','delivered','failed','unknown','stale','cancelled','held')")


def downgrade():
    raise RuntimeError('Retain review and delivery history; roll back images without lowering the schema')
