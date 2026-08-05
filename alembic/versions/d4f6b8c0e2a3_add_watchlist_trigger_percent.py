"""add trigger_percent to watchlist_item

Revision ID: d4f6b8c0e2a3
Revises: c3e5a7b9d1f2
Create Date: 2026-08-05 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4f6b8c0e2a3'
down_revision: Union[str, Sequence[str], None] = 'c3e5a7b9d1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, no backfill: every existing row uses an absolute gold
    # trigger, and NULL here is exactly what "this item is on the price
    # mode" means. The two columns are mutually exclusive by application
    # rule, not by constraint -- a CHECK would have to be dropped and
    # recreated to add a third mode later, and the invariant is enforced in
    # one place (watchlist.py's add/update routes) where it can carry the
    # reason with it.
    op.add_column('watchlist_item', sa.Column('trigger_percent', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('watchlist_item', 'trigger_percent')
