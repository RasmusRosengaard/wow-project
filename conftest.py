# Puts the project root on sys.path so tests/ can import the top-level modules.

import os

# Test-safe defaults for env vars auth.py needs at import time (it hard-fails
# without SECRET, by design -- fail-fast is correct in production) and that
# would otherwise break the session cookie under TestClient's plain http://.
# setdefault() so a real .env value (local dev) still wins; this only fills
# gaps for environments with no .env at all (CI, a fresh checkout).
os.environ.setdefault("SECRET", "test-secret-not-used-for-anything-real")
os.environ.setdefault("COOKIE_SECURE", "false")
