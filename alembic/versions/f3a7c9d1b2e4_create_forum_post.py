"""create forum_post

Revision ID: f3a7c9d1b2e4
Revises: d73d88b42ac7
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

import fastapi_users_db_sqlalchemy
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a7c9d1b2e4'
down_revision: Union[str, Sequence[str], None] = 'd73d88b42ac7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'forum_post',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('author_id', fastapi_users_db_sqlalchemy.generics.GUID(), nullable=False),
        sa.Column('author_email', sa.String(length=320), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=True),
        sa.Column('image_filename', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['author_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('forum_post')
