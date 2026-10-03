"""Несколько фильтров одного лейбла могут вести в один канал."""
from alembic import op

revision = '0019_multiple_channel_filters'
down_revision = '0018_separate_ocr_scores'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('uq_max_route_mark_channel', 'max_publication_routes', type_='unique')
    op.create_unique_constraint('uq_max_route_filter_channel', 'max_publication_routes', ['filter_id', 'channel_id'])


def downgrade():
    # Если после обновления появились несколько маршрутов одного лейбла,
    # прежнее ограничение не восстановится. Миграция не удаляет эти данные.
    op.create_unique_constraint('uq_max_route_mark_channel', 'max_publication_routes', ['mark_id', 'channel_id'])
    op.drop_constraint('uq_max_route_filter_channel', 'max_publication_routes', type_='unique')
