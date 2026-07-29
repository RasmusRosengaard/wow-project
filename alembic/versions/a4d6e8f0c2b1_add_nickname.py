"""add nickname

Revision ID: a4d6e8f0c2b1
Revises: f3a7c9d1b2e4
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4d6e8f0c2b1'
down_revision: Union[str, Sequence[str], None] = 'f3a7c9d1b2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('user', sa.Column('nickname', sa.String(length=50), nullable=True))
    op.add_column('forum_post', sa.Column('author_nickname', sa.String(length=50), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('forum_post', 'author_nickname')
    op.drop_column('user', 'nickname')
