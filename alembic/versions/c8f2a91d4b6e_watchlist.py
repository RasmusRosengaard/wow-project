"""add discord_webhook_url and create watchlist_item

Revision ID: c8f2a91d4b6e
Revises: b3e0f41fe4e9
Create Date: 2026-08-02 00:00:00.000000

"""
from typing import Sequence, Union

import fastapi_users_db_sqlalchemy
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8f2a91d4b6e'
down_revision: Union[str, Sequence[str], None] = 'b3e0f41fe4e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('user', sa.Column('discord_webhook_url', sa.String(length=500), nullable=True))
    op.create_table(
        'watchlist_item',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('owner_id', fastapi_users_db_sqlalchemy.generics.GUID(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('pet_species_id', sa.Integer(), nullable=True),
        sa.Column('trigger_price_copper', sa.Integer(), nullable=True),
        sa.Column('label', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_notified_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('watchlist_item')
    op.drop_column('user', 'discord_webhook_url')
