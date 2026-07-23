#!/bin/sh
set -e

# Migrations-before-serve: every deploy brings the schema up to date
# automatically, so "database" is part of the same auto-deploy as
# "backend"/"web" rather than a separate manual step to remember.
alembic upgrade head

exec python dashboard.py --sell "${DEFAULT_SELL:-1403}" --host 0.0.0.0 --port "${PORT:-8000}"
