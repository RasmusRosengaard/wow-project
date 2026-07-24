"""add locked_sell_realm

Revision ID: d73d88b42ac7
Revises: 88b11ff6272a
Create Date: 2026-07-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd73d88b42ac7'
down_revision: Union[str, Sequence[str], None] = '88b11ff6272a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('user', sa.Column('locked_sell_realm', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user', 'locked_sell_realm')
