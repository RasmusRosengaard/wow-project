"""grandfather existing users as verified

Revision ID: b2d5f8a03c71
Revises: a1c4e7f92b60
Create Date: 2026-08-06

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2d5f8a03c71'
down_revision: Union[str, Sequence[str], None] = 'a1c4e7f92b60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Data migration, no schema change: `is_verified` has existed on `user`
    # since the table was created (88b11ff6272a) but nothing ever read or wrote
    # it, so every account in production sits at False. This deploy is the one
    # that gives the column meaning (auth.current_verified_user), and without
    # this one-off UPDATE it would lock the founder/superuser account and every
    # paying subscriber out of checkout and the Snipe Board on the spot.
    #
    # Grandfathering is the honest call, not just the safe one: these accounts
    # were never asked to confirm an address, so treating them as "failed to
    # verify" would be inventing a fact about them. The 2026-08-04 created_at
    # migration made the same kind of choice in the other direction -- refusing
    # to backfill a signup date it couldn't know -- and for the same reason:
    # don't fabricate history.
    #
    # Safe to run exactly once and only here. It runs inside the same
    # `alembic upgrade head` that first mounts the verification routers
    # (docker-entrypoint.sh, migrations-before-serve), so there is no window in
    # which a genuinely-unverified new registration could exist yet and get
    # wrongly flipped. Re-running it later would silently verify every
    # unconfirmed account, which is why it must never be folded into a
    # repeatable/idempotent maintenance step.
    #
    # "user" is double-quoted: it's a reserved word in Postgres and an
    # unquoted UPDATE user would be a syntax error.
    op.execute('UPDATE "user" SET is_verified = true')


def downgrade() -> None:
    """Downgrade schema."""
    # Intentionally a no-op. This migration records a real-world fact ("these
    # accounts predate email verification"), and by the time anyone downgrades,
    # genuinely-verified accounts are indistinguishable from grandfathered ones
    # -- so the only reversals available are to un-verify everybody (locking out
    # real confirmed users) or to guess. Doing nothing is the one option that
    # can't destroy correct data.
    pass
