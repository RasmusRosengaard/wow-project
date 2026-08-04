"""create visitor_ip table

Revision ID: e5c1a72b9f04
Revises: a6eb310b93ca
Create Date: 2026-08-04 10:12:44.118203

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5c1a72b9f04'
down_revision: Union[str, Sequence[str], None] = 'a6eb310b93ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'visitor_ip',
        sa.Column('ip', sa.String(length=45), nullable=False),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('hit_count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('ip'),
    )
    # The admin page's two reads are both "most recently active first" --
    # the live view filters last_seen >= cutoff, the history orders by it.
    op.create_index('ix_visitor_ip_last_seen', 'visitor_ip', ['last_seen'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_visitor_ip_last_seen', table_name='visitor_ip')
    op.drop_table('visitor_ip')
