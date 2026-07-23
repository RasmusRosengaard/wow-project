# PROGRESS — WoW AH Snipe Validator

Living status doc: what's built, what's not, what's next. `CLAUDE.md` is
still the authoritative brief (architecture, conventions, full roadmap,
API facts) — this file is the scannable summary, kept in sync with it.
Replaces the old one-off `HANDOFF.md` snapshot (removed 2026-07-23).

Last updated: 2026-07-23.

## Hosted SaaS pivot (active initiative)

Turning the local single-user tool into a hosted product: email
login/register, €5/month Stripe subscription gating the sniper page, and
deep collection expanded to all ~100 EU realms so subscribers pick their own
sell realm. Full design in `~/.claude/plans/unified-nibbling-simon.md`
(Railway host, FastAPI-Users auth, GitHub Actions CI + Railway native CD).
Staged deliberately — each stage ships and gets verified before the next.

| Stage | Status | Notes |
|---|---|---|
| 1 — GitHub repo, CI, branch protection | **Done** | Repo: `github.com/RasmusRosengaard/wow-project` (private). `.github/workflows/ci.yml` runs `pytest -q` on every push/PR to `main`; branch protection requires the `test` check to pass before merge. |
| 2 — Auth (FastAPI-Users, Postgres) | **Done** | `db.py` (async SQLAlchemy, `User` model), `auth.py` (cookie-session backend), `static/login.html`/`register.html`, `dashboard.py`'s API routes gated behind `current_active_user`. Alembic migrations (`alembic/`) — one so far, creates the `user` table. Test-safe `SECRET`/`COOKIE_SECURE` defaults live in root `conftest.py` (not just the CI workflow), so any environment without a `.env` — CI, a fresh clone — works out of the box. Found and fixed two real bugs: `CookieTransport` defaults `cookie_secure=True`, which silently drops the session cookie over plain `http://`; and `auth.py`'s hard `SystemExit` on missing `SECRET` crashed pytest collection entirely in CI (no `.env` there). |
| 3 — Stripe subscription | Not started | Test-mode Stripe keys + the €4.99/mo Price (`price_1TwN5SCz0OKW695K4KryA1wA`) already in `.env` and set on Railway. Still needed: `billing.py` itself, `STRIPE_WEBHOOK_SECRET` (now that a real URL exists — see webhook setup notes), and per Stripe's current guidance, swap the full secret key for a **restricted key** (`rk_`) scoped to Checkout/Customers/Subscriptions/Webhooks. ToS re-read already confirmed by the human 2026-07-23 — payments are clear to build. |
| 4 — Server-side collection + sell-realm picker | **Done (backend, live); picker UI not started** | Scope revised 2026-07-23: deep-collects **FULL/HIGH population realms only**, not literally all ~100 EU realms (human's call — less overhead, more sniping-relevant liquidity; region-wide listings sweep stays unscoped since cheap-listing sources can be any realm). `collect_all.py`, cached population lookup via new `blizz.connected_realm_population()`. Runs as an hourly in-process background loop in `dashboard.py`, confirmed actually starting in Railway's logs after fixing a real bug (no logging was configured, so `log.info()` calls — including this one — were silently dropped). **The local Windows Task Scheduler job (`AHSnipePipeline`) is now Disabled** (not removed) — Railway is the sole collection path going forward; local dev Postgres stopped too, both easily restarted if ever needed. Still missing: a sell-realm dropdown in the dashboard UI (`/api/realms` endpoint) — the server has data now, but the frontend still takes a free-typed realm id. |
| 5 — CD (Railway auto-deploy + migrations-on-deploy) | **Done** | **Live at `https://wow-project-production.up.railway.app`.** Project `valiant-peace` on Railway: `wow-project` service (built from our `Dockerfile`, not an auto-detected builder) + a `Postgres` service, `DATABASE_URL` wired via Railway's private internal networking (`postgres.railway.internal`) with the `postgresql+asyncpg://` scheme. `docker-entrypoint.sh` runs `alembic upgrade head` before serving, confirmed working against the real database. Verified end to end: register → login → authenticated `/api/me` all succeed against the live URL. Railway's native GitHub integration auto-deploys `main` on push. A persistent Volume is attached to the web service at `/app/data` (was missing initially — the AH data lives there, entirely separate from Postgres's own 5GB volume, which only holds the tiny `user` table; the two aren't related limits). **Note on tooling**: the Railway CLI's Windows binary is blocked by this machine's Smart App Control (a real, semi-irreversible-to-disable Windows 11 feature) — worked around by running the CLI inside a Docker container instead (Linux binary, never touches that policy), rather than disabling a system security feature for one tool. |

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
7. ~~CI/CD~~ — Stage 1 done, see "Hosted SaaS pivot" above. Full CD (auto-deploy) is Stage 5, still pending the Railway project.
8. ~~Web: login/subscriptions/all-realm collection~~ — see "Hosted SaaS pivot" above (Stages 2-4).

**Immediate blocker on the pivot's own next step (Stage 2)**: none — Stage 2
(auth) can be built and tested against a local/Dockerized Postgres without
needing the Railway project to exist yet. Railway/Stripe account creation
can happen in parallel, whenever the human gets to it.

## Where to look for more

- `CLAUDE.md` — architecture, conventions, full roadmap, Blizzard API facts (authoritative).
- `README.md` — human-facing setup/usage.
- `git log` — commit-level history.
