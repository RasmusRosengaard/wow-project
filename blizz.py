"""Blizzard API helpers: .env loading, OAuth client-credentials token, authenticated GET.

Free developer client: https://develop.battle.net -> Create Client (see README).
"""
import os
import re
import threading
import time
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent


class _TokenBucket:
    """Thread-safe token bucket -- shared, process-wide rate limiting for
    every Blizzard API call, added 2026-08-01 after a real incident (see
    .claude/docs/history.md): an ad-hoc diagnostic script called
    item_names.NameCache.ensure_many() with no coordination with anything
    else in the process, firing a burst of thousands of concurrent requests
    that starved collect_all.py's background collector of its share of the
    budget -- confirmed live via production logs (`HTTP 429 from auctions
    endpoint`, several realms' collection cycles failing outright) -- which
    cascaded into slower, longer-held requests elsewhere in the app.

    Before this, api_get() had no rate awareness at all: fetch_snapshot.py
    has its own local retry/backoff for 429s on the one endpoint it calls,
    but scan_region.py/item_names.py/appearance.py all call api_get()
    directly with none, and none of these independent callers had any
    shared notion of "how much of the budget is already spent right now by
    something else in this same process." A burst from any one of them
    (background collection, on-demand NameCache resolution for a
    never-before-seen realm, a future ad-hoc script) could starve the
    others -- not a hypothetical, this is exactly what happened.

    This is deliberately a rate limiter, not just a concurrency cap: a
    concurrency cap alone (e.g. a semaphore on in-flight requests) doesn't
    bound throughput if individual requests are fast -- a handful of
    concurrent slots could still sustain well over 100 req/s if each
    request completes in tens of milliseconds. A token bucket directly
    enforces "no more than N requests per second," which is what Blizzard's
    limit actually is.

    acquire() blocks (sleeping, not busy-waiting) until a token is
    available -- callers don't need their own throttling logic, they just
    get slowed down transparently and safely instead of hitting 429s."""

    def __init__(self, capacity: float, refill_per_second: float):
        self._capacity = capacity
        self._tokens = capacity
        self._refill_per_second = refill_per_second
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity,
                                   self._tokens + (now - self._last) * self._refill_per_second)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._refill_per_second
            time.sleep(wait)


# Two independent buckets, both consulted on every call -- see "Blizzard API
# facts" in CLAUDE.md: 100 req/s *and* 36,000 req/h (which is only ~10 req/s
# sustained -- the real binding constraint over any window longer than a few
# seconds, not the per-second figure). Capacities set a bit under Blizzard's
# published limits (90/34,000, not 100/36,000) as headroom -- polite margin,
# not an invitation to run right up against the wall. The per-second bucket
# smooths bursts to a safe rate; the hourly bucket is what actually prevents
# a sustained burst (e.g. resolving thousands of never-before-cached items)
# from consuming the whole hour's budget in a couple of minutes the way it
# did in the incident this was added for.
_burst_limiter = _TokenBucket(capacity=90, refill_per_second=90)
_hourly_limiter = _TokenBucket(capacity=34000, refill_per_second=34000 / 3600)


def _load_env() -> None:
    """Tiny .env loader so we don't need python-dotenv."""
    env = _ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

REGION = os.environ.get("BLIZZ_REGION", "eu").lower()
TOKEN_URL = "https://oauth.battle.net/token"
_tok = {"value": None, "expires": 0.0}


def get_token() -> str:
    """Client-credentials token, cached in-process (valid ~24h)."""
    if _tok["value"] and time.time() < _tok["expires"] - 120:
        return _tok["value"]
    cid = os.environ.get("BLIZZ_CLIENT_ID")
    sec = os.environ.get("BLIZZ_CLIENT_SECRET")
    if not cid or not sec:
        raise SystemExit("Set BLIZZ_CLIENT_ID / BLIZZ_CLIENT_SECRET in .env (see README).")
    r = requests.post(TOKEN_URL, auth=(cid, sec),
                      data={"grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    j = r.json()
    _tok["value"] = j["access_token"]
    _tok["expires"] = time.time() + float(j.get("expires_in", 86000))
    return _tok["value"]


def api_get(path: str, namespace: str, params: dict | None = None,
            headers: dict | None = None) -> requests.Response:
    """GET https://{region}.api.blizzard.com{path} with namespace '{namespace}-{region}'.

    Rate-limited via _burst_limiter/_hourly_limiter (added 2026-08-01, see
    _TokenBucket's docstring) -- every caller in the process (the
    collector, the region scanner, NameCache, AppearanceCache, any manual
    script) shares the same two buckets, since this is the one function
    all of them already route every real Blizzard call through. A caller
    never needs to reason about the shared budget itself; acquire() just
    transparently slows it down when the budget's tight instead of the
    request going out and coming back a 429."""
    _burst_limiter.acquire()
    _hourly_limiter.acquire()
    url = f"https://{REGION}.api.blizzard.com{path}"
    p = {"namespace": f"{namespace}-{REGION}", "locale": "en_GB"}
    if params:
        p.update(params)
    h = {"Authorization": f"Bearer {get_token()}"}
    if headers:
        h.update(headers)
    return requests.get(url, params=p, headers=h, timeout=180)


def find_connected_realm(slug: str) -> list[tuple[int, list[str]]]:
    """Resolve a realm slug ('tarren-mill') to its connected-realm id(s)."""
    slug = slug.strip().lower().replace(" ", "-").replace("'", "")
    r = api_get("/data/wow/search/connected-realm", "dynamic",
                params={"realms.slug": slug, "_page": 1})
    r.raise_for_status()
    out = []
    for res in r.json().get("results", []):
        d = res.get("data", {})
        realms = [rl.get("slug", "?") for rl in d.get("realms", [])]
        out.append((d.get("id"), realms))
    return out


def connected_realm_population(cr_id: int) -> str | None:
    """Population tier ("FULL"/"HIGH"/"MEDIUM"/"LOW") for one connected
    realm, straight from the same connected-realm detail endpoint -- used to
    scope server-side collection to realms actually worth deep-collecting
    (collect_all.py), not the raw region-wide realm list."""
    r = api_get(f"/data/wow/connected-realm/{cr_id}", "dynamic")
    r.raise_for_status()
    return (r.json().get("population") or {}).get("type")


def connected_realm_realms(cr_id: int) -> list[dict]:
    """Member realm {"name", "slug", "category"} triples for one connected
    realm (usually one triple; a few connected realms merge several named
    realms under one auction house) -- name/slug/language category (e.g.
    "English"/"German"/"French"/"Italian"/"Russian"/"Spanish" -- confirmed
    live 2026-07-31 across all 92 EU connected realms, one language per
    connected realm, never mixed) so callers needing any of them don't make
    extra requests."""
    r = api_get(f"/data/wow/connected-realm/{cr_id}", "dynamic")
    r.raise_for_status()
    return [{"name": rl.get("name"), "slug": rl.get("slug"), "category": rl.get("category")}
            for rl in r.json().get("realms", []) if rl.get("slug")]


def list_connected_realms() -> list[int]:
    """Every connected-realm id in the region, for the buy-side region scanner."""
    r = api_get("/data/wow/connected-realm/index", "dynamic")
    r.raise_for_status()
    ids = []
    for entry in r.json().get("connected_realms", []):
        m = re.search(r"/connected-realm/(\d+)", entry.get("href", ""))
        if m:
            ids.append(int(m.group(1)))
    return ids
