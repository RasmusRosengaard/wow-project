# AH Snipe Validator (EU retail)

A cross-realm AH sniping tool: a listing on any EU realm priced well below
what your chosen sell realm's own current cheapest listing is, net of the
5% AH cut, is a validated snipe.

**Pricing model changed 2026-07-25** (human product decision): sell price
is the sell realm's own current cheapest live listing for that item/
variant — directly observable, zero inference. The original design instead
inferred a historical sold-price percentile from Blizzard's hourly
snapshots, which repeatedly produced wrong prices in production (a single
troll/decoy listing becoming the entire percentile, a camped relist
misclassified as a real sale) despite successive patches. See `CLAUDE.md`
and `snipe_check.find_snipes()`'s docstring for the full incident history
and the tradeoff this accepted: the tool now compares listing price to
listing price, the same as other AH sniper tools, rather than trying to
validate against real sales — a validated-sales signal could be
reintroduced later as a secondary liquidity indicator, not built now.

**Live at: https://wow-project-production.up.railway.app** — register, log
in, and use the dashboard there. This project is **not meant to be run
locally as a product** — it's a hosted service. The sections below are for
people changing the code, not people wanting their own instance.

## How it works

Deep-collects one or more high/full-population EU realms' auctions hourly →
parquet snapshots, giving a live view of what's currently listed on your
sell realm. Separately, a region-wide scanner sweeps *every* EU realm's
current listings (no history needed on the buy side). A listing on another
realm priced well below your sell realm's own current cheapest listing for
that item/variant, net of the 5% AH cut, is a validated snipe.

The snapshot pipeline also diffs consecutive snapshots and classifies every
vanished auction (`inferred_sale`, `likely_relisted`, `ambiguous`,
`likely_expired`, `bid_only_gone`) — this classification engine still runs
and is still real, useful "core IP" (see `CLAUDE.md`'s "Inference logic"),
but as of 2026-07-25 it no longer drives the sell price shown to users; see
above.

## Architecture

- **App**: FastAPI (`dashboard.py`) — serves the web UI, the JSON API, and
  auth (`auth.py`, FastAPI-Users, cookie sessions). One process.
- **AH data**: parquet + DuckDB (`fetch_snapshot.py`, `scan_region.py`,
  `diff_snapshots.py`, `analyze.py`, `snipe_check.py`) — not in Postgres.
  This is the core inference engine; see `CLAUDE.md` for the classification
  logic and its known limits.
- **Relational data**: Postgres, via async SQLAlchemy (`db.py`) — users,
  sessions, subscription state only.
- **Billing**: Stripe (`billing.py`), **live mode** — Checkout Session per
  subscription, webhook-driven access gating (`auth.current_subscribed_user`).
  Deployed straight to live rather than verified against test mode first
  (human decision); see `CLAUDE.md`/`PROGRESS.md` for what that traded off.
- **Collection**: `collect_all.py` polls every ~10 minutes *inside* the
  deployed app (`ENABLE_BACKGROUND_COLLECTION=true` on Railway) — not
  hourly, since Blizzard republishes at no fixed clock time and a fixed
  hourly poll could drift out of phase with the real update by up to an
  hour; `fetch_once()`'s `If-Modified-Since` check keeps the no-op polls
  cheap, and `diff_snapshots` only re-runs when a realm actually got new
  data. Deep-collects FULL/HIGH-population realms, sweeps the whole region.
  There is no separate collector process or local scheduled task — this
  used to run via a human's local machine and Windows Task Scheduler; it
  doesn't anymore.
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

Railway's **"Wait for CI"** setting (Service → Settings → Deploy) is enabled,
so deploys wait on the GitHub Actions check passing rather than firing on
every push regardless of CI status (fixed 2026-07-23 — CLI doesn't expose
this toggle, had to be flipped in the dashboard directly). Still worth the
habit of running `pytest -q` locally before pushing and preferring PRs over
direct pushes to `main`, so branch protection's required check has a chance
to matter too, not just Railway's gate.

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

## How the classification engine works — and its limits

**As of 2026-07-25 this classification does not drive the sell price shown
to users** (see above) — it's kept as a real, working signal that could
back a future liquidity/confidence indicator, and `analyze.py`'s CLI
commands (`item`/`trace`/`summary`) still use it for manual debugging.

- `time_left` buckets: SHORT <30m, MEDIUM 30m–2h, LONG 2–12h, VERY_LONG 12–48h.
  An auction that vanishes while LONG/VERY_LONG, within a gap shorter than the
  bucket's minimum remaining time, **cannot have expired** → sold or cancelled.
- **Cancel–relist:** an identical (item, bonuses, buyout, qty) listing
  reappearing under a new auction id in the same interval → `likely_relisted`,
  excluded from sales. The relist-matching window requires an *exact* price
  match, so a troll who tweaks their joke price between relists can still
  slip through misclassified as two separate "sales" — this is exactly what
  motivated dropping sold-price inference from the pricing model (item
  13051, see `CLAUDE.md`).
- **Bid-only auctions** can't be insta-bought → excluded.
- **Crafted items pool by market, not exact roll:** Blizzard attaches
  several undocumented per-craft identifiers to crafted/scaling gear (both
  `m:` modifiers and `b:` bonus-list ids) that otherwise make almost every
  listing its own "variant." `market_key()` pools these into one market
  instead of fragmenting into dozens of near-unique buckets, using
  structural detection (companion pairs, N-way partitions) rather than a
  blind frequency cutoff — see `CLAUDE.md`'s "Inference logic" for the full
  history, including two earlier, simpler approaches that were tried and
  found insufficient before this one.
- **Known blind spot:** a cancel *without* a relist is indistinguishable from a
  sale. Every AH data service shares some version of this problem. This was
  never formally validated against real seller behavior (Phase 0's
  validation gate was explicitly skipped, see `CLAUDE.md`) — one of several
  reasons the pricing model no longer depends on this classification.

## Dashboard

Sortable/filterable table styled to feel like the game itself: item names
colored by rarity, gold/silver/copper coin icons for prices, realm *names*
instead of raw connected-realm ids, and a mouse-hover tooltip per item
(icon, colored name, buy/sell price, discount). Click an item's
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

Access requires only an account (register/login) — a free tier (added
2026-07-25) lets any logged-in account use the dashboard with real, capped
data (250 rows, locked to the first sell realm queried). An active €4.99/mo
Stripe subscription (`/subscribe` — `billing.py`) raises the cap to 2,000
rows and unlocks switching sell realms freely; see `/pricing` for the full
comparison. Sell realm is picked from a dropdown (`GET /api/realms`), not a
free-typed id. See `CLAUDE.md`/`PROGRESS.md` for current status.

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
(Phase 0's gate was skipped by deliberate decision) — see `CLAUDE.md`. This
protocol validates the classification engine itself, which (as of
2026-07-25) no longer drives what price users see — still worth running if
the classification is ever reintroduced as a liquidity signal.

## Roadmap

Cross-realm snipe engine (done) → hosted multi-tenant product with email
auth and a live Stripe subscription gating the dashboard (**done**,
2026-07-23, free tier added 2026-07-25) → region commodity feed
(**out of scope**, not being pursued) → appearance-scarcity layer
(ItemModifiedAppearance mappings via wago.tools + the static item API,
**groundwork started** 2026-07-23 — `appearance.py`, a "unique transmog"
filter) → deal score with buy-realm → sell-realm routing → Discord webhook
alerts → free companion addon. See `PROGRESS.md`/`CLAUDE.md` for current
staged status and immediate next steps (a restricted Stripe key is the main
thing ahead, human-only to change). The full visual redesign (light "assay
ledger" theme, dark-mode toggle) and dashboard QoL pass (sell-realm picker,
budget filter, grouped duplicates, client-side filtering/sorting) are both
done, across all six pages.

A public, no-login `/log` page (added 2026-07-23) shows every timestamp new
auction-house data was actually retrieved per realm, for anyone to verify
independently -- `GET /api/log`/`GET /api/log/realms` are the only
unauthenticated `/api/*` routes in the app, by design.

## Notes

- The auction data belongs to Blizzard. Before charging anyone for anything
  built on it, read the current **Blizzard Developer API Terms of Use** — the
  free-addon / paid-external-service pattern (TSM, Raider.IO) is established,
  but confirm the fine print yourself. (Already done once, 2026-07-23 — but
  re-check before actually turning on billing if time has passed.)
- Keep `WoW`/`Warcraft` out of any product name; "for World of Warcraft" as a
  description is the accepted form.
