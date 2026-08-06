"""add user_id to visitor_ip

Revision ID: e7a2c4b60d18
Revises: d4f6b8c0e2a3
Create Date: 2026-08-06 09:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from fastapi_users_db_sqlalchemy.generics import GUID


# revision identifiers, used by Alembic.
revision: str = 'e7a2c4b60d18'
down_revision: Union[str, Sequence[str], None] = 'd4f6b8c0e2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable with no backfill: existing rows predate any user association
    # and there is nothing to recover it from -- the tracker only ever stored
    # an IP. They stay NULL and the admin page shows them as anonymous.
    #
    # GUID (not sa.Uuid) to match db.User.id, which fastapi-users defines via
    # SQLAlchemyBaseUserTableUUID -- the same type every other user FK in this
    # schema uses (ForumPost.author_id, WatchlistItem.owner_id, ...).
    op.add_column('visitor_ip', sa.Column('user_id', GUID(), nullable=True))
    # ondelete SET NULL: deleting an account must neither cascade away its
    # traffic history nor fail on an FK violation. Named explicitly so the
    # downgrade can drop it -- an auto-named constraint is not portable to
    # drop on Postgres.
    op.create_foreign_key('fk_visitor_ip_user_id', 'visitor_ip', 'user',
                          ['user_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_visitor_ip_user_id', 'visitor_ip', type_='foreignkey')
    op.drop_column('visitor_ip', 'user_id')
