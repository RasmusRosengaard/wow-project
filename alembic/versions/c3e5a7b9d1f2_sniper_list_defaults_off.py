"""default_sniper_list_enabled now defaults to off

Revision ID: c3e5a7b9d1f2
Revises: b2d4f6a8c0e1
Create Date: 2026-08-05 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3e5a7b9d1f2'
down_revision: Union[str, Sequence[str], None] = 'b2d4f6a8c0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Only the *default* changes. Existing rows are deliberately left
    # alone: a1c9e3f5d7b2 set them all to true so an already-live feature
    # would not vanish, and flipping them now would switch it off for
    # anyone currently relying on it -- including accounts that ticked the
    # box on purpose, which is indistinguishable from the migration default
    # at the column level. Turning it off for yourself is one click in the
    # UI; having it turned off for you by a deploy is not.
    op.alter_column('user', 'default_sniper_list_enabled',
                    existing_type=sa.Boolean(), nullable=False,
                    server_default=sa.false())


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('user', 'default_sniper_list_enabled',
                    existing_type=sa.Boolean(), nullable=False,
                    server_default=sa.true())
