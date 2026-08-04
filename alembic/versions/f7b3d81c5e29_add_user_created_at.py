"""add created_at to user

Revision ID: f7b3d81c5e29
Revises: e5c1a72b9f04
Create Date: 2026-08-04 14:41:07.882914

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7b3d81c5e29'
down_revision: Union[str, Sequence[str], None] = 'e5c1a72b9f04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable with NO server_default and no backfill, deliberately: existing
    # accounts have no recoverable signup date (uuid4 ids carry no timestamp),
    # and a server_default of now() would silently stamp every one of them
    # with the deploy time -- inventing data rather than admitting its
    # absence. They stay NULL and the admin page shows "before tracking".
    op.add_column('user', sa.Column('created_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user', 'created_at')
