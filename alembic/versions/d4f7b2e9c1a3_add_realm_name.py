"""add realm_name to wow_account_realm

Revision ID: d4f7b2e9c1a3
Revises: c8f2a91d4b6e
Create Date: 2026-08-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f7b2e9c1a3'
down_revision: Union[str, Sequence[str], None] = 'c8f2a91d4b6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('wow_account_realm', sa.Column('realm_name', sa.String(length=100), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('wow_account_realm', 'realm_name')
