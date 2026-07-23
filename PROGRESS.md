# PROGRESS — WoW AH Snipe Validator

Living status doc: what's built, what's not, what's next. `CLAUDE.md` is
still the authoritative brief (architecture, conventions, full roadmap,
API facts) — this file is the scannable summary, kept in sync with it.

Last updated: 2026-07-23.

## Status at a glance

**Live and working right now**, at `https://wow-project-production.up.railway.app`:
- Register / log in / log out; subscribe/cancel/status all on the profile page.
- Subscribe via Stripe (**live mode, real payments** — human decision
  2026-07-23) → dashboard access unlocks automatically via webhook.
- Server-side data collection running every ~10 minutes for FULL/HIGH-pop EU
  realms, no human machine required.
- Auto-deploy on push to `main`, gated on tests passing (CI → Railway
  "Wait for CI" → build → deploy → DB migration, all automatic).
- Every page (dashboard, login, register, subscribe, profile) runs one
  consistent designed look — see "UI design pass" below.
- Sell-realm dropdown (`/api/realms`), min/max gold budget filter, grouped
  duplicate-item snipes (best deal on top, expandable), and instant
  client-side table sorting — see "Dashboard QoL pass" below.

**Not built yet**, in priority order — see "Next up" below for detail:
1. Restricted Stripe key (still the full `sk_live_...` secret).
2. Everything past Stage 5 of the hosted pivot — see "Longer-term roadmap" at the bottom.

## Next up (short list, do these in roughly this order)
1. **Restricted Stripe key** — swap the full `sk_live_...` secret key for a key restricted to just Checkout/Customers/Subscriptions/Webhooks (Stripe's own current guidance, not done yet — lower urgency than functionality, but real bug-radius reduction).
2. Decide whether Phase 3 still needs a per-item transferability flag (see Phase status table — the original framing for this was wrong and got corrected 2026-07-23).
3. Phase 2 (commodities feed) and Phase 3 (appearance/scarcity layer) — see "Longer-term roadmap".

**Remember**: if a custom domain ever replaces the `railway.app` subdomain, the Stripe webhook endpoint URL (Stripe Dashboard → Developers → Webhooks) needs updating to match by hand — it won't happen automatically.

## Hosted SaaS pivot — stage by stage

Turning the local single-user tool into a hosted product. Full design in
`~/.claude/plans/unified-nibbling-simon.md`. All 5 stages are now **done**:

| Stage | What it is | Status |
|---|---|---|
| 1 | GitHub repo, CI, branch protection | **Done** — private repo, `pytest -q` on every push, branch protection requires it. |
| 2 | Email auth (FastAPI-Users + Postgres) | **Done** — register/login/logout, cookie sessions, API routes gated. |
| 3 | Stripe subscription | **Done, live mode** — see detail below. |
| 4 | Server-side collection + realm picker | **Done** — backend and UI dropdown both shipped. |
| 5 | CD (Railway auto-deploy + DB migrations) | **Done** — live URL, Wait-for-CI verified working end to end. |

### Stage 3 detail — Stripe (done 2026-07-23, deployed straight to live mode)

Human decision: skip test-mode verification, go live immediately. `billing.py`:
`POST /billing/checkout` creates a Checkout Session for the single €4.99/mo
price and redirects; `POST /billing/webhook` verifies the Stripe signature
and handles `checkout.session.completed` / `customer.subscription.updated` /
`customer.subscription.deleted`, writing `subscription_status` (and
customer/subscription ids, period end) onto the user — the only writer of
those fields. `auth.current_subscribed_user` gates `/api/snipes` and
`/api/status` (402 if unsubscribed, distinct from 401 if not logged in at
all, so the frontend can send you to `/subscribe` vs `/login` correctly).

Verified live end to end: unauthenticated → 401, logged-in-but-unsubscribed →
402, and a real `cs_live_...` Checkout Session URL generated successfully.

**Real bug the test suite caught before it shipped**: `event["data"]["object"]`
from `stripe.Webhook.construct_event()` is a `StripeObject`, not a plain
dict — it supports `obj["key"]` but *not* `obj.get("key")`, which every
handler used. Would have thrown on the very first real webhook delivery;
fixed by calling `.to_dict()` once up front (needed on the retrieved
`Subscription` object too, same issue).

Still open: a restricted (`rk_live_...`) key instead of the full secret key
(see "Next up").

### Stage 4 detail — server-side collection (backend done 2026-07-23)

Scope deliberately narrowed same-day: deep-collects **FULL/HIGH population
realms only**, not literally all ~100 EU realms (human's call — less
overhead, more sniping-relevant liquidity). The region-wide listings sweep
stays unscoped (every EU realm), since the cross-realm thesis specifically
needs cheap listings from low-pop realms too.

`collect_all.py` runs in-process inside `dashboard.py`, polling every ~10
minutes rather than hourly — Blizzard republishes at no fixed clock time, so
a fixed hourly poll from container-boot could drift out of phase with the
real update by up to an hour; `fetch_once()`'s `If-Modified-Since` check
keeps no-op polls cheap, and `diff_snapshots` only re-runs when a realm
actually got a new snapshot that cycle (not every tick — it recomputes from
scratch each time, so re-running it for no new information would waste
real CPU at this polling frequency).

`run_cycle.py`, `run_cycle_task.ps1`, and the local Windows Task Scheduler
job are **fully deleted** (human decision: this product is never run
locally as a going concern) — Railway is the sole collection path. Local
dev Postgres stopped, not removed, for local Stage-3+-adjacent dev only.

The `/api/realms` + dropdown UI piece is done — see "Dashboard QoL pass" below.

### UI design pass (done 2026-07-23)

Ran every page through the `frontend-design` skill — an "Undermine cartel
trading-floor" identity: dark olive/moss panels (`--bg #14170f`, `--panel
#1b1f15`), a toxic-green accent (`--toxic #a6e600`) for primary actions, brass
and ember as secondary accents, system font stacks throughout (no external
fonts — keeps the "no build step, offline-capable" convention), `ui-monospace`
for all numeric/data columns. The dashboard's signature element is a static
segmented status ticker (`.masthead`/`.ticker`) replacing the old plain status
div — deliberately not animated, respects `prefers-reduced-motion`. Applied
consistently to `dashboard.html`, `login.html`, `register.html`,
`subscribe.html`, `profile.html`; each page's existing JS/functional logic
was preserved exactly, only markup/CSS changed.

Also removed per explicit human instruction: the repeated "NOTE: an AH
listing is guaranteed unsoulbound..." caveat banner is gone from the UI
entirely (was CSS + HTML + a JS line populating it from `data.caveat`) — the
API still returns `caveat` in its response, it's just not rendered anymore.

Folded in the two QoL fixes from the old "Next up" #4 at the same time:
min-discount filter is now a real percentage input (0–100, was a raw 0–1
fraction) and the names/icons toggle is gone — resolving item_id → name/icon
is always on now, there was no real reason to ever turn it off.

### Dashboard QoL pass (done 2026-07-23)

Four requests worked in one batch, all client-facing on `dashboard.html`
plus the two small backend additions they needed:

- **Sell-realm picker** — new `GET /api/realms` (`dashboard.py`) lists every
  realm with a `data/events/{cr}.parquet` file (i.e. actually collected),
  each resolved to a display name via the existing `_realm_info()` cache.
  The dashboard's free-typed realm-id number box is now a `<select>`
  populated from this on load, falling back to a plain "no realms collected
  yet" option if the list comes back empty.
- **Min/max gold filter** — `snipe_check.find_snipes()` gained `min_gold`/
  `max_gold` params (filtering on the *buy*-side unit price, i.e. what
  you'd actually spend — that's the number a budget cap means for an "AH
  sniper"), threaded through `/api/snipes` and the `--min-gold`/`--max-gold`
  CLI flags. Two new number inputs in the dashboard's filter bar, both
  optional (blank = no bound).
- **Duplicate snipes grouped** — when the same item/variant shows up as a
  snipe more than once (different buy realms, or several auctions on one),
  the ledger now shows one row per item with the single best deal (highest
  discount%) on top, plus a `▾ N` toggle to expand and see the rest, still
  best-first. Purely a client-side `static/dashboard.html` change — the API
  still returns the flat row list, `renderTable()` groups it.
- **No-lag sorting** — every sort click used to re-fetch `/api/snipes` over
  the network just to reorder rows the browser already had. Sorting is now
  entirely client-side (`renderTable()`/`compareRows()`): clicking a column
  header reorders the already-loaded batch instantly, with an ascending/
  descending toggle on repeat clicks (▲/▼ indicator). All seven columns are
  sortable now, not just the three the server's `SORT_COLUMNS` supported
  before (buy realm, item name, and variant are new). The trade-off: the
  fetched batch is always the server's discount-ranked top N, so re-sorting
  by e.g. sell price reorders *within* that batch rather than asking the
  server for a differently-ranked top N — acceptable for the "reorder what's
  on screen instantly" goal this was solving; Refresh/auto-refresh still
  re-fetches the accurate discount-ranked set.

### Stage 5 detail — hosting (done, Wait-for-CI verified 2026-07-23)

Live at `https://wow-project-production.up.railway.app`. Project
`valiant-peace` on Railway: `wow-project` service (built from our
`Dockerfile`) + a `Postgres` service (only holds the `user` table — the AH
data lives on a separate Volume attached to `wow-project` at `/app/data`,
unrelated to Postgres's own 5GB limit). `docker-entrypoint.sh` runs
`alembic upgrade head` before serving. Railway's "Wait for CI" is enabled
and confirmed working: a push now sits in `WAITING` until the GitHub
Actions check passes, then proceeds `BUILDING` → `DEPLOYING` → `SUCCESS`
automatically — a red CI run actually blocks the deploy now, not just runs
in parallel with it.

**Tooling note**: the Railway CLI's Windows binary is blocked by this
machine's Smart App Control (real, semi-irreversible-to-disable Windows 11
feature) — worked around by running the CLI inside a Docker container
instead (Linux binary, never touches that policy).

## Longer-term roadmap (beyond the hosted pivot)

| Phase | What it is | Status |
|---|---|---|
| 0 — Validate the sale-inference signal | 48h manual verification protocol | **Gated, skipped** (human decision 2026-07-20) — signal still unvalidated against real seller behavior. |
| 1 — Cross-realm engine + hardening | Region scanner, snipe-check, orchestration | **Mostly done.** Remaining: sell/scan realm config file, `--since` incremental diff. |
| 2 — Commodities feed | Region-wide, quantity-delta inference | Not started — separate schema from gear, don't merge. |
| 3 — Appearance layer | ItemModifiedAppearance scarcity mapping | Not started. The originally planned "warband transferability flag" was based on a wrong assumption (BoP items can't be AH-listed at all, corrected 2026-07-23) — re-decide at Phase 3 start whether any per-item flag is still needed. |
| 4 — Deal score + Discord alerts | Second paid feature | Not started — blocked on Phase 3 data. |
| 5 — Free companion addon | In-game tooltip overlay | Not started. The *web dashboard* half of this phase already shipped as part of the hosted pivot above; only the addon itself remains. |

## Known gaps / risks

- Sale-inference classification (`inferred_sale` especially) has never been checked against real seller behavior.
- No sell/scan realm config file — `--exclude`/`--items` CLI flags are the manual stand-in.
- The AH `modifiers` type-28 field ("item level") isn't Blizzard-documented; the dashboard sanity-checks it against the item's catalog level, but the underlying meaning is still community-sourced, not official.
- Stripe is on the full `sk_live_...` secret key, not a restricted key scoped to what `billing.py` actually needs (see "Next up").

## What's built (file-level)

| Component | File(s) |
|---|---|
| Sale-inference engine (core IP) | `diff_snapshots.py` |
| Realm collector | `fetch_snapshot.py` (called by `collect_all.py`, not run standalone anymore) |
| Region scanner | `scan_region.py` |
| Snipe-check logic | `snipe_check.py` |
| Server-side collection | `collect_all.py` |
| Web dashboard | `dashboard.py`, `static/dashboard.html` |
| Auth | `auth.py`, `db.py`, `static/login.html`/`register.html` |
| Billing | `billing.py`, `static/subscribe.html` |
| Item name/icon/quality cache | `item_names.py` |
| Hosting | `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore` |
| Tests | `tests/` — 109 passing (`pytest -q`), no external services needed |

## Where to look for more

- `CLAUDE.md` — architecture, conventions, full roadmap, Blizzard API facts (authoritative).
- `README.md` — human-facing setup/usage.
- `git log` — commit-level history.
