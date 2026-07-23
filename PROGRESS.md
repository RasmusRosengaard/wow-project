# PROGRESS — WoW AH Snipe Validator

Living status doc: what's built, what's not, what's next. `CLAUDE.md` is
still the authoritative brief (architecture, conventions, full roadmap,
API facts) — this file is the scannable summary, kept in sync with it.
Replaces the old one-off `HANDOFF.md` snapshot (removed 2026-07-23).

Last updated: 2026-07-23.

## Phase status

| Phase | Status | Notes |
|---|---|---|
| 0 — Validate the sale-inference signal | **Gated, skipped** | Human decision 2026-07-20: build ahead without waiting for the 48h validation gate. Signal remains unvalidated against real seller behavior; no `VALIDATION.md` written. |
| 1 — Cross-realm engine + hardening | **Mostly done** | Region scanner, snipe-check CLI, and full-cycle orchestration all built and running hourly. Remaining: sell/scan realm config split, snapshot retention, `--since` incremental diff. |
| 2 — Commodities feed | Not started | Region-wide collector + quantity-delta inference, separate schema from gear. |
| 3 — Appearance layer | Not started | Scope note 2026-07-23: the originally planned "warband transferability flag" was based on a wrong assumption (BoP items can't be AH-listed at all) — re-decide at Phase 3 start whether a per-item flag is still needed. |
| 4 — Deal score + Discord alerts (first paid feature) | Not started | Blocked on Phase 3 appearance scarcity data; payments blocked on human's ToS re-read. |
| 5 — Free addon + web dashboard | **Partially done** | The dashboard's local, read-only form (`dashboard.py`) was pulled forward to 2026-07-23 — see below. Free in-game addon and turning the dashboard into an actual hosted multi-tenant product (auth, subscriptions) are still not started. |

## What's built

| Component | File(s) | Status |
|---|---|---|
| Sale-inference engine | `diff_snapshots.py` | Done — classifies vanished auctions into `inferred_sale`/`likely_relisted`/`ambiguous`/`likely_expired`/`bid_only_gone`. Unvalidated against real seller behavior (Phase 0 gate skipped). |
| Sell-realm collector | `fetch_snapshot.py` | Done — hourly snapshots, `If-Modified-Since` aware, backoff on 429/5xx. |
| Region scanner | `scan_region.py` | Done — sweeps all EU connected realms' current listings. |
| Snipe-check CLI | `snipe_check.py` | Done — flags discounted listings vs sell-realm sold-price percentiles. |
| Pipeline orchestration | `run_cycle.py` | Done — one full pass (poll → scan → diff → snipe-check), scheduled hourly via Windows Task Scheduler (`AHSnipePipeline`). |
| Live web dashboard | `dashboard.py`, `static/dashboard.html` | Done (local, single-user, no auth) — WoW-styled UI: quality-colored names, gold/silver/copper coin pricing, hover tooltip, clickable icon → Undermine Exchange link, realm names instead of raw ids, ilvl-plausibility-checked variant display. Deployable (FastAPI/uvicorn, Dockerized) but not deployed/hosted. |
| Item name/icon/quality/level cache | `item_names.py` | Done — `NameCache`, backed by Blizzard's static API, self-healing on schema growth. |
| Docker packaging | `Dockerfile`, `.dockerignore` | Written, **build unverified** — Docker Desktop wasn't running when this was created; needs a real `docker build` check. |
| Test suite | `tests/` | 75 tests passing (`pytest -q`). |

## Known gaps / risks

- Sale-inference classification (`inferred_sale` especially) has never been checked against real seller behavior — no test auctions posted/cancelled/bought to confirm the false-positive rate.
- No sell/scan realm config file — `--exclude`/`--items` CLI flags are the manual stand-in.
- No retention policy on sell-realm snapshots (grows unbounded).
- The AH auction `modifiers` type-28 field ("item level") isn't Blizzard-documented; the dashboard now sanity-checks it against the item's catalog level, but the underlying meaning is still inferred from community usage + one human-confirmed example, not official docs.
- Docker image build is unverified end to end.

## Next steps (rough order)

1. Verify `docker build` actually works (Docker Desktop needs to be running).
2. Phase 1 hardening: sell/scan realm config split, snapshot retention, `--since` incremental diff.
3. Decide whether Phase 3 still needs a per-item transferability flag, or whether the existing CLI/dashboard NOTE text already covers the real risk (see Phase 3 row above).
4. Phase 2: commodities feed (region-wide, separate schema — do not merge with gear).
5. Phase 3: appearance layer (`ItemModifiedAppearance` mapping via wago.tools DB2 exports + static API fallback).
6. Optional: revisit the Phase 0 validation protocol (`VALIDATION.md`) if the sale-inference signal's accuracy becomes a live concern.
7. CI/CD, so every update to API/python/database/web actually published and deploys.
8. Web: 1. Login/Register with authentication (email), 2. Subscriptions with stripe (for now only 1 for 5 euros/month), 3. Subs can acces the sniper page (for now only supports draenor as seller realm), 4. Start collecting data for all realms, so the user  can choose seller realms themself.

## Where to look for more

- `CLAUDE.md` — architecture, conventions, full roadmap, Blizzard API facts (authoritative).
- `README.md` — human-facing setup/usage.
- `git log` — commit-level history.
