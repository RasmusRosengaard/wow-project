"""add default_sniper_list_enabled to user

Revision ID: a1c9e3f5d7b2
Revises: f7b3d81c5e29
Create Date: 2026-08-05 15:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c9e3f5d7b2'
down_revision: Union[str, Sequence[str], None] = 'f7b3d81c5e29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # NOT NULL with server_default true, deliberately -- the opposite call
    # from created_at's nullable-no-backfill reasoning one migration back,
    # for a different reason. The standing rule scan shipped earlier the
    # same day already delivering to every subscriber with a webhook set,
    # so backfilling existing rows to false would silently switch a live
    # feature off for everyone who currently has it. "No stored preference"
    # genuinely means "on" here, because on is what they already have.
    #
    # server_default is kept (not dropped after backfill) so a row inserted
    # by anything that doesn't go through the SQLAlchemy model -- a manual
    # SQL insert, a future bulk import -- still lands in a valid state
    # rather than violating the NOT NULL.
    op.add_column('user', sa.Column('default_sniper_list_enabled', sa.Boolean(),
                                    nullable=False, server_default=sa.true()))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user', 'default_sniper_list_enabled')
