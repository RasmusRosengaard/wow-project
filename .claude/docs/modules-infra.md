# Infrastructure, tests and tooling

Packaging, migrations, dependencies, the test suite and the repo's own Claude
Code tooling.

## `tests/`

pytest suite. Real duckdb/pyarrow throughout, no mocking of the data layer;
live Blizzard calls are stubbed. **Two conftests**: root `conftest.py` puts
the project root on `sys.path`, sets test-safe `SECRET`/`COOKIE_SECURE`, and
forces `DATABASE_URL=""` to match CI (must be `""` not deleted —
`blizz._load_env()` uses `setdefault()` and would refill an absent key from
`.env`); `tests/conftest.py`'s `no_real_database` autouse fixture patches
`db._database_url()` to raise a named `RuntimeError`, so a missing DB override
fails in ~2s locally instead of hanging (`SystemExit` is a `BaseException` and
deadlocks TestClient's portal). Fix the test, never weaken the guard.
`isolate_item_names_cache`/`isolate_appearance_cache`/`isolate_tsm_cache`
autouse fixtures redirect cache paths into `tmp_path` so tests never touch the
real gitignored caches or make live TSM calls — patch `CACHE_PATH` directly,
not `DATA` (it's bound at import time). **Locally run only the tests covering
the change** (`tests/test_<module>.py`; full suite for
`db.py`/`auth.py`/`conftest.py`) — CI runs all of them on push and gates the
Railway deploy. History: see history.md.

## `Dockerfile` / `.dockerignore` / `docker-entrypoint.sh`

Packages the web app into a container; entrypoint runs `alembic upgrade head`
then `exec python dashboard.py`. Reads `PORT` (Railway-injected) and
`DEFAULT_SELL` (UI prefill only) from env.

## `alembic/`, `alembic.ini`

DB migrations. `env.py` reads `DATABASE_URL` from the environment.

## `requirements.txt`

`requests`, `pyarrow`, `duckdb`, `fastapi`, `uvicorn`, `httpx`, `fastapi-
users[sqlalchemy]`, `sqlalchemy[asyncio]`, `asyncpg`, `aiosqlite` (tests
only), `alembic`, `stripe`, `pytest-asyncio`, `python-multipart` (added
2026-07-29 for `forum.py`'s image upload form — FastAPI's `Form`/`File`
parsing needs it, not imported directly). `lupa` (added 2026-08-02 for
`tsm_import.py` — embeds a real Lua interpreter so TSM's own unmodified
`LibDeflate.lua`/`LibSerialize.lua` can run unmodified rather than being hand-
ported to Python; the first non-Python-ecosystem runtime dependency this
project has taken on, a deliberate human-approved tradeoff against
reimplementation risk, not a default reached for lightly — see that module's
row. Confirmed to ship prebuilt `manylinux`/`win_amd64` wheels for the Python
versions this project uses, so `pip install` needs no compiler on either the
Windows dev machine or the `python:3.12-slim` Docker image; `Dockerfile` was
updated to `COPY vendor ./vendor` so the two vendored `.lua` files actually
ship in the container).

## `.env.example`

`BLIZZ_CLIENT_ID`, `BLIZZ_CLIENT_SECRET`, `BLIZZ_REGION=eu`, `STRIPE_PUBLISHAB
LE_KEY`/`STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET`/`STRIPE_PRICE_ID`/`STRIPE_
PRODUCT_ID`, `SECRET`, `COOKIE_SECURE`, `DATABASE_URL`.

## `.claude/commands/`, `.claude/skills/project-review/`

Reusable Claude Code tooling (added 2026-07-25): `/railway-status`
(deploy/CI/volume status, read-only), `/railway-debug <command>` (runs a
command against live production data via `railway ssh`), `/ship` (test both
envs → commit → push → watch CI → confirm Railway deploy → optionally live-
verify), `project-review` skill (a repo-specific pre-push checklist —
market_key Python/SQL parity, copper-vs-gold units, the CI-env test mismatch
class of bug, ToS/secrets/Stripe-key guardrails, frontend-verify-in-a-real-
browser convention). Keep these current the same way as this file — if the
Railway CLI invocation changes, update the command file, not just this note.
