"""create oauth_account

Revision ID: a1c4e7f92b60
Revises: e7a2c4b60d18
Create Date: 2026-08-06

"""
from typing import Sequence, Union

import fastapi_users_db_sqlalchemy
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1c4e7f92b60'
down_revision: Union[str, Sequence[str], None] = 'e7a2c4b60d18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Backs Google login (db.OAuthAccount). Every column comes from
    # FastAPI-Users' SQLAlchemyBaseOAuthAccountTableUUID -- the lengths below
    # are that base table's, not chosen here.
    #
    # ondelete="cascade" on the FK: a linked login is meaningless without the
    # account it belongs to, so deleting a user must take its oauth rows with
    # it. This is the opposite of visitor_ip.user_id's SET NULL, and
    # deliberately so -- that table is about traffic history worth keeping,
    # this one is pure account plumbing.
    op.create_table(
        'oauth_account',
        sa.Column('id', fastapi_users_db_sqlalchemy.generics.GUID(), nullable=False),
        sa.Column('user_id', fastapi_users_db_sqlalchemy.generics.GUID(), nullable=False),
        sa.Column('oauth_name', sa.String(length=100), nullable=False),
        sa.Column('access_token', sa.String(length=1024), nullable=False),
        sa.Column('expires_at', sa.Integer(), nullable=True),
        sa.Column('refresh_token', sa.String(length=1024), nullable=True),
        sa.Column('account_id', sa.String(length=320), nullable=False),
        sa.Column('account_email', sa.String(length=320), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='cascade'),
        sa.PrimaryKeyConstraint('id'),
    )
    # The first two indexes are FastAPI-Users' own (index=True on those
    # columns): the OAuth callback's first act is a lookup by
    # (oauth_name, account_id).
    op.create_index(op.f('ix_oauth_account_oauth_name'), 'oauth_account',
                    ['oauth_name'], unique=False)
    op.create_index(op.f('ix_oauth_account_account_id'), 'oauth_account',
                    ['account_id'], unique=False)
    # This one is ours, and the base table does NOT declare it:
    # User.oauth_accounts is mapped lazy="selectin" (see db.py for why not
    # joined), so every user lookup -- including
    # auth.resolve_user_from_request() on the /api/me and /api/snipes hot path
    # -- issues a `WHERE user_id IN (...)` against this table. Without the
    # index that's a sequential scan on every authenticated request.
    op.create_index(op.f('ix_oauth_account_user_id'), 'oauth_account',
                    ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_oauth_account_user_id'), table_name='oauth_account')
    op.drop_index(op.f('ix_oauth_account_account_id'), table_name='oauth_account')
    op.drop_index(op.f('ix_oauth_account_oauth_name'), table_name='oauth_account')
    op.drop_table('oauth_account')
