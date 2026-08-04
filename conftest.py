# Puts the project root on sys.path so tests/ can import the top-level modules.

import os

# Test-safe defaults for env vars auth.py needs at import time (it hard-fails
# without SECRET, by design -- fail-fast is correct in production) and that
# would otherwise break the session cookie under TestClient's plain http://.
# setdefault() so a real .env value (local dev) still wins; this only fills
# gaps for environments with no .env at all (CI, a fresh checkout).
os.environ.setdefault("SECRET", "test-secret-not-used-for-anything-real")
os.environ.setdefault("COOKIE_SECURE", "false")

# Match CI, which sets no DATABASE_URL at all -- so a missing DB-override
# fixture fails HERE instead of only in CI (the 2026-08-01 incident: 17 tests
# green locally, red in CI). This replaces the old `env -u DATABASE_URL pytest`
# second run, which had silently become a no-op: blizz._load_env() does
# os.environ.setdefault() at import time and .env has a DATABASE_URL line, so
# the shell unsetting it was undone the moment anything imported blizz.
#
# Three details, all load-bearing:
#   - "" not `del`: an ABSENT key is exactly what setdefault() refills from
#     .env. A present-but-empty one is left alone, and still trips
#     db._database_url()'s `if not url` guard.
#   - a hard assignment, not setdefault(): the whole point is to beat .env.
#   - this file, not tests/conftest.py: the root conftest is imported before
#     any test module, therefore before `import db` -> `import blizz`.
#
# If a test dies on db._database_url()'s SystemExit, it is missing a DB
# override -- point the seam at throwaway SQLite (see test_dashboard.py's
# bypass_get_async_session). Do NOT "fix" it by setting DATABASE_URL.
os.environ["DATABASE_URL"] = ""
