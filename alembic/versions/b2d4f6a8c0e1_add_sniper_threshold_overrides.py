"""add per-user sniper threshold overrides

Revision ID: b2d4f6a8c0e1
Revises: a1c9e3f5d7b2
Create Date: 2026-08-05 16:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2d4f6a8c0e1'
down_revision: Union[str, Sequence[str], None] = 'a1c9e3f5d7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable with no server_default and no backfill -- the opposite call
    # from default_sniper_list_enabled one migration back, and for a real
    # reason. That column needed every existing row to keep a live feature
    # switched on, so "no stored value" had to mean True. Here NULL is a
    # meaningful state in its own right: "follow whatever the current
    # default is". Backfilling today's 2,000g / 10% would pin every
    # existing account to those numbers forever and quietly break the next
    # retune for everyone who never touched the setting.
    op.add_column('user', sa.Column('sniper_min_sale_avg_copper', sa.BigInteger(), nullable=True))
    op.add_column('user', sa.Column('sniper_buy_fraction', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user', 'sniper_buy_fraction')
    op.drop_column('user', 'sniper_min_sale_avg_copper')
