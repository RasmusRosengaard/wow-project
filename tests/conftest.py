"""Suite-wide guard: no test may build a real database engine.

The root conftest.py already sets DATABASE_URL="" so the ambient env matches
CI. That alone is enough to *stop* a real connection, but not enough to fail
usefully: db._database_url() raises SystemExit, which is a BaseException, and
a BaseException raised inside a route deadlocks Starlette's TestClient portal
instead of propagating -- confirmed live 2026-08-04 by deleting
test_dashboard.py's bypass_get_async_session autouse marker, which hung
pytest indefinitely rather than failing. CI (no DATABASE_URL, same code path)
would hang the same way, burning the job's full timeout for what should be an
obvious red.

So this replaces the chokepoint with a plain RuntimeError naming the test.
db._database_url() is the single function every real-engine path funnels
through -- engine(), sessionmaker() (via engine()), and isolated_session() --
so one patch covers all three seams.

Deliberately function-scoped: request.node.nodeid is the whole diagnostic
point, and a session-scoped guard could only say "some test". Cost is one
setattr per test.

Note: monkeypatch is shared with each test module's own fixtures, so they all
record onto one undo stack. Never call monkeypatch.undo() in a test -- it
would take this guard down as collateral.
"""

import pytest

import db


@pytest.fixture(autouse=True)
def no_real_database(request, monkeypatch):
    def _forbidden() -> str:
        raise RuntimeError(
            f"{request.node.nodeid} tried to build a REAL database engine via "
            "db._database_url(). This suite must never touch a real database -- "
            "CI has no DATABASE_URL, so this fails there too (that exact "
            "asymmetry cost 17 red CI tests on 2026-08-01). Fix the test, not "
            "this guard: point the DB seam at throwaway SQLite. For a route, "
            "dashboard.app.dependency_overrides[db.get_async_session]; for a "
            "direct call, monkeypatch db.sessionmaker (auth.py, dashboard.py) "
            "and/or db.isolated_session (watchlist.py). Canonical pattern: "
            "tests/test_dashboard.py's bypass_get_async_session."
        )

    monkeypatch.setattr(db, "_database_url", _forbidden)
