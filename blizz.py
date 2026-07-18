"""Blizzard API helpers: .env loading, OAuth client-credentials token, authenticated GET.

Free developer client: https://develop.battle.net -> Create Client (see README).
"""
import os
import time
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent


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
    """GET https://{region}.api.blizzard.com{path} with namespace '{namespace}-{region}'."""
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
