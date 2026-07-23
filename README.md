# AH Snipe Validator (EU retail)

A validation layer for WoW auction-house sniping: instead of flagging items
cheap relative to fictional *listing* prices (what every other sniper does),
this infers **actual sales** from Blizzard's hourly auction snapshots and
validates discounted listings against real sold-price percentiles on a
liquid sell realm.

**Live at: https://wow-project-production.up.railway.app** — register, log
in, and use the dashboard there. This project is **not meant to be run
locally as a product** — it's a hosted service. The sections below are for
people changing the code, not people wanting their own instance.

## How it works

Deep-collects one or more high/full-population EU realms' auctions hourly →
parquet snapshots → diffs consecutive snapshots → classifies every vanished
auction (`inferred_sale`, `likely_relisted`, `ambiguous`, `likely_expired`,
`bid_only_gone`) → sold-price percentiles + sales/day per item variant.
Separately, a region-wide scanner sweeps *every* EU realm's current listings
(no history needed on the buy side). A listing priced well below the sell
realm's sold-price percentile, net of the 5% AH cut, is a validated snipe.

## Architecture

- **App**: FastAPI (`dashboard.py`) — serves the web UI, the JSON API, and
  auth (`auth.py`, FastAPI-Users, cookie sessions). One process.
- **AH data**: parquet + DuckDB (`fetch_snapshot.py`, `scan_region.py`,
  `diff_snapshots.py`, `analyze.py`, `snipe_check.py`) — not in Postgres.
  This is the core inference engine; see `CLAUDE.md` for the classification
  logic and its known limits.
- **Relational data**: Postgres, via async SQLAlchemy (`db.py`) — users,
  sessions, subscription state only.
- **Collection**: `collect_all.py` runs hourly *inside* the deployed app
  (`ENABLE_BACKGROUND_COLLECTION=true` on Railway), deep-collecting
  FULL/HIGH-population realms and sweeping the whole region. There is no
  separate collector process or local scheduled task — this used to run via
  a human's local machine and Windows Task Scheduler; it doesn't anymore.
- **Hosting**: Railway. `Dockerfile`/`docker-entrypoint.sh` build the image
  and run migrations (`alembic upgrade head`) before serving. Railway's
  GitHub integration auto-deploys `main` on push.

See `CLAUDE.md` for full architecture detail, Blizzard API facts, and the
project's guardrails (decision-support only, no in-game automation).

## The deploy flow

The point of this setup: an agent (or a human) changes code, tests catch
regressions, and a passing change ships without anyone touching the
production system by hand.

1. Push to a branch, open a PR (or push straight to `main` for now, though a
   PR is the safer habit) — `.github/workflows/ci.yml` runs `pytest -q`.
2. Branch protection on `main` requires that check to pass before a PR can
   merge.
3. Railway watches `main` and rebuilds/redeploys automatically on every push
   — no separate deploy job, no manual `docker push`.
4. The container's entrypoint runs pending Alembic migrations before
   starting the server, so schema changes ship in the same step.

**Known gap, worth closing**: Railway's own deploy trigger doesn't currently
wait on the GitHub Actions check — it deploys on push regardless of CI
status. Railway has a **"Wait for CI"** toggle for exactly this
(Service → Settings → Deploy), not yet enabled here (CLI doesn't expose it,
has to be flipped in the dashboard). Until then, the actual safety net is
discipline: don't push code you haven't run `pytest -q` against locally, and
prefer PRs over direct pushes to `main` so the required check has a chance
to matter.

## Local development

For changing code, not for running your own instance:

1. **Blizzard API client (free):** https://develop.battle.net → API Access →
   *Create Client*. Redirect URL `https://localhost` (unused).
2. **Python 3.12+, deps:**
   ```
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. `cp .env.example .env`, fill in Blizzard creds + a `SECRET`. `DATABASE_URL`
   only matters if you're testing something that touches Postgres (auth,
   billing) — the test suite itself uses SQLite and doesn't need it.
4. `pytest -q` — full suite, no live network calls, no Postgres required.
5. To manually exercise the app locally (e.g. testing an auth/billing
   change end to end): `docker run -d -e POSTGRES_PASSWORD=devpassword
   -e POSTGRES_DB=wowproject -p 5432:5432 postgres:16-alpine`, then
   `python dashboard.py --sell <a realm you've collected>`. Leave
   `ENABLE_BACKGROUND_COLLECTION` unset — you don't want a local process
   also deep-collecting realms.
6. Commit, push, let CI/Railway take it from there.

The individual CLI tools (`fetch_snapshot.py`, `scan_region.py`,
`diff_snapshots.py`, `analyze.py`, `snipe_check.py`) are still runnable
standalone for debugging/inspecting data — see their `--help` — but none of
them are how the product actually runs anymore; `collect_all.py` inside the
deployed app is.

## How inference works — and its limits

- `time_left` buckets: SHORT <30m, MEDIUM 30m–2h, LONG 2–12h, VERY_LONG 12–48h.
  An auction that vanishes while LONG/VERY_LONG, within a gap shorter than the
  bucket's minimum remaining time, **cannot have expired** → sold or cancelled.
- **Cancel–relist:** an identical (item, bonuses, buyout, qty) listing
  reappearing under a new auction id in the same interval → `likely_relisted`,
  excluded from sales.
- **Bid-only auctions** can't be insta-bought → excluded.
- **Known blind spot:** a cancel *without* a relist is indistinguishable from a
  sale. Every AH data service shares some version of this problem. Sellers
  who cancel mostly relist (that's why they cancelled), so the hypothesis is
  that the residual noise is small — this was never formally validated
  against real seller behavior (Phase 0's validation gate was explicitly
  skipped, see `CLAUDE.md`); treat sold-price percentiles with that in mind.

## Dashboard

Sortable/filterable table styled to feel like the game itself: item names
colored by rarity, gold/silver/copper coin icons for prices, realm *names*
instead of raw connected-realm ids, and a mouse-hover tooltip per item
(icon, colored name, buy/sell price, discount, sales/day). Click an item's
**icon** to open its page on [Undermine Exchange](https://undermine.exchange/)
filtered to your sell realm, so you can eyeball an independent price history
next to the inferred one — the link lives on the icon rather than inside the
tooltip, since the tooltip follows the cursor and a link inside it would be
unreachable.

The variant column shows `ilvl NNN` when the listing's bonus-list data
includes an item-level modifier *and* that value is plausible relative to the
item's own catalog level — otherwise it falls back to a bonus-count summary
(the raw `b:.../m:...` string is always available on hover). That guard
exists because the modifier isn't officially documented by Blizzard and
produced nonsense for items outside the modern scaling system (a classic
wand once showed "ilvl 1112" against a real level of ~35). This smarter
display is dashboard-only — `snipe_check.py`'s terminal output (still usable
for local debugging) keeps printing the raw variant string.

Access requires an account (register/login — see `auth.py`); a subscription
gate (Stripe) is planned but not built yet, see `CLAUDE.md`/`PROGRESS.md`
for current status.

## Verification protocol (algorithm validation, not a setup step)

This is how you'd independently check the sale-inference classification is
right, not something you need to do to use the product. From a character on
a sell realm the deployment is actively collecting, post distinctive cheap
items with a buyout, 48h duration, then check what the pipeline says:

1. **Cancel two** at noted times, don't repost → these will show up as
   `inferred_sale`. This is the false-positive class; note it happened.
2. **Cancel one and instantly repost** at the same price → should classify as
   `likely_relisted`.
3. **Post one at 12h duration and let it expire** → must NOT appear as
   `inferred_sale`.
4. **Have a guildmate / second account buy one** → must appear as
   `inferred_sale` at the right price.

Then sanity-check scale: pick a famously liquid item and compare its
`per_day` (via `analyze.py item`/`trace`) against TSM's regional sale rate.
Same order of magnitude = signal is real. This was never actually run
(Phase 0's gate was skipped by deliberate decision) — see `CLAUDE.md`.

## Roadmap

Cross-realm snipe engine (done, this is it) → region commodity feed
(`/data/wow/auctions/commodities`) → appearance-scarcity layer
(ItemModifiedAppearance mappings via wago.tools + the static item API) →
deal score with buy-realm → sell-realm routing → Discord webhook alerts →
Stripe subscription gating the dashboard (in progress) → free companion
addon. See `PROGRESS.md` for the current staged status of the hosted-SaaS
pivot specifically.

## Notes

- The auction data belongs to Blizzard. Before charging anyone for anything
  built on it, read the current **Blizzard Developer API Terms of Use** — the
  free-addon / paid-external-service pattern (TSM, Raider.IO) is established,
  but confirm the fine print yourself. (Already done once, 2026-07-23 — but
  re-check before actually turning on billing if time has passed.)
- Keep `WoW`/`Warcraft` out of any product name; "for World of Warcraft" as a
  description is the accepted form.
