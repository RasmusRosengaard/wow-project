"""create wow_account and wow_account_realm

Revision ID: b3e0f41fe4e9
Revises: a4d6e8f0c2b1
Create Date: 2026-08-01 00:00:00.000000

"""
from typing import Sequence, Union

import fastapi_users_db_sqlalchemy
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e0f41fe4e9'
down_revision: Union[str, Sequence[str], None] = 'a4d6e8f0c2b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'wow_account',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('owner_id', fastapi_users_db_sqlalchemy.generics.GUID(), nullable=False),
        sa.Column('label', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'wow_account_realm',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('wow_account_id', sa.Integer(), nullable=False),
        sa.Column('connected_realm_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['wow_account_id'], ['wow_account.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('wow_account_id', 'connected_realm_id', name='uq_wow_account_realm_account_realm'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('wow_account_realm')  # child first, FK to wow_account
    op.drop_table('wow_account')
