"""create anon_session table

Revision ID: a6eb310b93ca
Revises: d4f7b2e9c1a3
Create Date: 2026-08-03 19:04:12.436642

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6eb310b93ca'
down_revision: Union[str, Sequence[str], None] = 'd4f7b2e9c1a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'anon_session',
        sa.Column('token', sa.String(length=32), nullable=False),
        sa.Column('locked_sell_realm', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('token'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('anon_session')
