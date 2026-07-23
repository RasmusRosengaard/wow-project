# CLAUDE.md — WoW AH Snipe Validator (EU retail)

Agent-facing project brief. `README.md` is the human-facing version — keep both in
sync when architecture or commands change. Handed off from a Claude.ai planning
session on 2026-07-18.

## What this project is

A **validation layer for WoW auction-house sniping**, not another sniper.
Existing tools (TSM Sniper, Auctionator) flag items cheap relative to *listing*
prices — which are fiction. The unsolved half is validation: does this item
actually sell, at what real prices, how fast, and is its transmog appearance
genuinely rare? We infer **actual sales** from Blizzard's hourly AH snapshots and
(later) layer appearance scarcity on top, producing a Deal Score.

Business model (decided, don't revisit without the human): free in-game addon
(Blizzard requires addons to be free) + paid external data service — Discord
alerts, dashboards, custom filters. The TSM / Raider.IO pattern. Competitors: TSM
(coarse regional sale rates, hostile UX), Saddlebag Exchange (alerts). Our edge:
validated sell-realm liquidity + cross-realm snipe routing + appearance-level
intelligence.

## Market structure (Warbands — decided 2026-07-18, core to the product)

Since The War Within, the warband bank makes **gold account-wide** and lets
**unsoulbound BoE items move between a player's characters on any realm**. The
gear/transmog AH is still per connected realm (separate listings, buyer pools,
prices) — so cheap listings rot on low-pop realms while hubs pay full price.
That asymmetry IS the product:

- The user picks **sell realms** (their high/full-pop hub realms). We run full
  snapshot-diff sale inference there → real sold-price percentiles + sales/day.
- A lightweight **region scanner** sweeps every other EU connected realm's
  current listings (no sale inference needed on the buy side).
- **Validated snipe** = a listing on any realm priced well below the sell
  realm's sold-price percentile for that item/variant, on an item that is
  liquid there — net of the 5% AH cut and transfer friction.
- Correction (2026-07-23, human): an AH listing is guaranteed unsoulbound —
  BoP items cannot be listed, full stop — so "Warbound vs BoP" was never the
  right check. The actual remaining risk is narrower: don't equip/use an item
  before moving it through the warband bank, or it locks to that character.
  Whether the appearance layer still needs a per-item flag for that (or any
  other real transfer restriction, e.g. Unique-equipped edge cases) is now
  open — revisit when Phase 3 starts rather than assuming the original framing.

Rate-limit math holds: deep collection ~6 req/h per sell realm; scanning all
~100 EU connected realms hourly is ~100 dumps/h against a 36,000 req/h limit.
Buy-side scans don't need snapshot history — latest listings per realm suffice.

**Current phase: 0 — prove the sale-inference signal is real before building more.**

## Non-negotiable guardrails

- **Decision support only.** Never write code that automates in-game actions: no
  auction posting/buying automation, no input simulation, no game-client memory
  reading, no packet interception. ToS compliance is a product requirement, not a
  preference. If a task drifts that way, stop and flag it.
- The future in-game addon must be **free**; monetization lives in the external
  service only.
- Before any payment/monetization feature ships, the human must re-read the
  current Blizzard Developer API Terms of Use. Do not ship payments autonomously.
- Secrets live in `.env`, never in code, never committed.
- No "WoW"/"Warcraft" in product branding; "for World of Warcraft" as a
  description is the accepted form.

## Current state (updated 2026-07-20)

| File | Purpose |
|---|---|
| `blizz.py` | `.env` loader, OAuth client-credentials token, `api_get()`, realm-slug↔id lookups: `find_connected_realm`, `list_connected_realms()` (region index, for the scanner), `connected_realm_slugs()`/`connected_realm_realms()` (name+slug together, one call — added for `dashboard.py`'s realm-name display and Undermine Exchange links) |
| `fetch_snapshot.py` | Sell-realm collector CLI: polls one connected realm, writes hourly parquet snapshots (If-Modified-Since aware); logs to console + rotating `data/logs/collector.log`, backs off on 429/5xx, skips malformed-JSON bodies |
| `scan_region.py` | **Phase 1.** Region scanner: sweeps every EU connected realm's *current* listings (no history, `--exclude` to skip sell realms already deep-collected) into `data/listings/{cr_id}.parquet`, overwritten each sweep. Reuses `bonus_key`/`get_auctions_with_backoff` from `fetch_snapshot.py`. Logs to `data/logs/scanner.log` |
| `snipe_check.py` | **Phase 1.** Joins `data/listings/*.parquet` (any realm but the sell realm) against the sell realm's `sales` view (from `analyze.connect`) on `(item_id, bonus_key)`; flags listings below a sold-price percentile net of the 5% AH cut, above a liquidity floor. `--items`/`--items-file` restrict to a hand-curated watchlist. Prints `snipe_check.CAVEAT` every run: an AH listing is guaranteed unsoulbound (BoP can't be listed), so it can always ride the warband bank — just don't equip/use it before moving it |
| `run_cycle.py` | **Phase 1.** One full pipeline pass: poll sell realm → scan region → (once 2+ sell-realm snapshots exist) rebuild events → snipe-check. Does one pass and exits; meant to be re-run roughly hourly (Blizzard's dump cadence), not looped internally |
| `run_cycle_task.ps1` | Windows Task Scheduler wrapper around `run_cycle.py --sell 1403`; appends output to `data/logs/run_cycle_task.log` (no console in a scheduled task). Registered as task **AHSnipePipeline**, hourly, indefinitely — see "Scheduled automation" below |
| `dashboard.py` | **Dashboard (pulled forward from Phase 5, see Process deviation below).** FastAPI app, read-only web layer over `snipe_check.find_snipes()` — does not run the pipeline itself. `GET /api/snipes` mirrors `snipe_check.py`'s CLI flags as query params, returns JSON rows + the shared `snipe_check.CAVEAT` string (never omitted, even when empty), plus raw-copper prices (`buy_copper`/`sell_copper` — formatting happens client-side per the "copper end to end" convention), `buy_realm_name` (via `blizz.connected_realm_realms()`, in-process cached), and a smarter `variant` summary — `ilvl NNN` parsed from bonus-list modifier type 28 **only when `names=true` and the claimed value is within `ILVL_PLAUSIBILITY_MULTIPLE` (5x) of the item's own catalog level** (`item_names.NameCache.base_level()`); otherwise falls back to a bonus-count summary. This plausibility check exists because the modifier isn't Blizzard-documented and produced nonsense for non-scaling items (e.g. a classic wand showed "ilvl 1112" against a real base level of ~35, caught 2026-07-23) — full raw `bonus_key` still included as `variant_raw` regardless. **The CLI (`snipe_check.py`/`print_snipes`) intentionally does NOT get this treatment** — it keeps printing the raw `bonus_key` string as-is; the smarter variant display, quality colors, icons, and plausibility check are dashboard-only, by design, not a gap to close. When `names=true`, rows also carry `icon`/`quality_color` from `item_names.NameCache`, and the response carries top-level `region`/`sell_realm_slug` for building an Undermine Exchange link (`https://undermine.exchange/#{region}-{sell_realm_slug}/{item_id}`, confirmed against a real example). `GET /api/status` surfaces `data/state/{cr}.json`'s collector `last_modified` + listings-sweep freshness, so a stalled scheduled task is visible in the UI, not silently hidden. `GET /` serves `static/dashboard.html`. `python dashboard.py --sell 1403` runs it on `127.0.0.1:8000` |
| `static/dashboard.html` | Single static file, vanilla JS, no build step/Node/npm. Sortable/filterable table over `/api/snipes` with WoW-flavored presentation: quality-colored item names, gold/silver/copper coin-icon money formatting, a custom mouse-hover tooltip (icon, colored name, prices — informational only) mimicking an in-game item tooltip. **The item icon itself (not the tooltip) is the Undermine Exchange link** — the tooltip repositions on every `mousemove` to follow the cursor, so a link inside it was unreachable; clicking the stable table-row icon opens the item's Undermine Exchange page in a new tab instead (fixed 2026-07-23). Persistent caveat banner, freshness indicators, ~60s client-side polling auto-refresh |
| `Dockerfile` / `.dockerignore` | Packages `dashboard.py` only (not the collection pipeline) into a container; `data/`/`.env` stay host-local, mounted as a volume at run time. Makes the dashboard *deployable*, not *deployed* — see note below |
| `tests/` | pytest suite (`pytest -q`; root `conftest.py` makes top-level modules importable). `test_diff.py`: all five classifications + gap/relist edges. `test_fetch.py`: `bonus_key`/`rows` purity, backoff, malformed-JSON skip. `test_pipeline.py`: snapshots-on-disk → `diff_snapshots.main()` → analyze commands, incl. idempotent rebuild. `test_scan_region.py`: listing `rows()` purity, malformed-JSON skip, sweep survives a per-realm failure and honors `--exclude`. `test_blizz.py`: `list_connected_realms()` href parsing, `connected_realm_slugs()`/`connected_realm_realms()`. `test_snipe_check.py`: discount/liquidity/item filtering, sell-realm-self exclusion, `--items`/`--items-file` merge. `test_item_names.py`: `NameCache` name/icon/quality/`base_level` resolution and caching, incl. backfilling quality/level onto a cache file written before those fields existed. `test_dashboard.py`: `/api/snipes`/`/api/status`/`/api/config` against the same synthetic-pipeline fixtures via FastAPI's `TestClient` (real duckdb/pyarrow, no mocking; live Blizzard calls for realm info are stubbed), the ilvl-plausibility fallback (both the legitimate and bogus cases), and pure `_parse_variant()`/`_realm_info()` caching tests. **`isolate_item_names_cache` is an autouse fixture redirecting `item_names.CACHE_PATH` into `tmp_path`** — added after `names=true` tests were caught writing fake stub data into the real, gitignored `data/item_names.json` production cache (found and cleaned 2026-07-23); any new test touching `NameCache` inherits the isolation automatically, nothing to remember per-test |
| `diff_snapshots.py` | **Core IP.** Diffs consecutive sell-realm snapshots, classifies every vanished auction, writes events parquet |
| `analyze.py` | DuckDB CLI: liquidity summary + per-item sold-price distribution / percentile check / per-auction trace |
| `item_names.py` | `NameCache`: display-only, never-raises lookups backed by the static item/pet API, cached at `data/item_names.json`. `.get()` name, `.icon()` render-CDN icon URL (`/media/item\|pet/{id}`), `.quality_color()` hex color from the item's `quality.type` (or a positional guess for pet rarity, since Blizzard doesn't document `pet_quality_id`'s exact enum), `.base_level()` the item's own catalog level (used by `dashboard.py` to sanity-check the modifier-28 ilvl claim) — all fail soft to a neutral fallback, never break a snipe_check/dashboard run. Internally, `_ensure_item_details()` fetches name+quality+level in one API call and backfills whichever of the three cache dicts a pre-existing entry is missing, so an old cache file (name-only) self-heals instead of silently returning `None` forever for the newer fields |
| `db.py` | **Hosted SaaS pivot, Stage 2.** Async SQLAlchemy setup for the *relational* data only (users/sessions/subscription state) — deliberately separate from the parquet+DuckDB AH data layer, which is unchanged. `User` model (FastAPI-Users base fields + `stripe_customer_id`/`stripe_subscription_id`/`subscription_status`/`subscription_current_period_end`, written only by Stage 3's webhook handler). `DATABASE_URL`-driven; tests override the session dependency with SQLite (`aiosqlite`), so the suite needs no real Postgres |
| `auth.py` | FastAPI-Users wiring: email/password register+login, **cookie-based** sessions (not bearer/JWT-in-header — matches the static-HTML-no-SPA frontend). `current_active_user` gates `dashboard.py`'s API routes. `COOKIE_SECURE` env toggle — `CookieTransport` defaults `cookie_secure=True`, which silently drops the cookie over local `http://`; `false` in dev `.env`, left unset (secure) in production since Railway serves HTTPS |
| `alembic/`, `alembic.ini` | DB migrations (async template). One migration so far: creates the `user` table. `alembic/env.py` reads `DATABASE_URL` from the environment (same source of truth `db.py` uses) rather than a hardcoded `alembic.ini` URL |
| `docker-entrypoint.sh` | Container startup: `alembic upgrade head` then `exec python dashboard.py` — migrations run automatically on every deploy, so "database" is part of the same auto-deploy as "backend"/"web" (Stage 5's CD goal). Reads `PORT` (Railway-injected) and `DEFAULT_SELL` (UI prefill only) from env |
| `static/login.html`, `static/register.html` | Plain HTML/JS forms hitting FastAPI-Users' `/auth/login` (form-urlencoded) and `/auth/register` (JSON) routes, same no-build-step convention as `dashboard.html` |
| `requirements.txt` | `requests`, `pyarrow`, `duckdb`, `fastapi`, `uvicorn`, `httpx`, `fastapi-users[sqlalchemy]`, `sqlalchemy[asyncio]`, `asyncpg`, `aiosqlite` (tests only), `alembic`, `stripe`, `pytest-asyncio` (Python 3.10+) |
| `.env.example` | `BLIZZ_CLIENT_ID`, `BLIZZ_CLIENT_SECRET`, `BLIZZ_REGION=eu`, `STRIPE_PUBLISHABLE_KEY`/`STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET`/`STRIPE_PRICE_ID`/`STRIPE_PRODUCT_ID`, `SECRET`, `COOKIE_SECURE`, `DATABASE_URL` |

Verified: `pytest -q` green (82 tests as of Stage 2 auth, 2026-07-23; `test_auth.py` + updated `test_dashboard.py`, all against SQLite — no external DB needed in CI). Live API confirmed working end to end:
`fetch_snapshot.py --find draenor` resolved cr-id 1403, `scan_region.py`
completed a full 91-realm sweep, `run_cycle.py` ran a real cycle against both.
One real Draenor snapshot exists as of 2026-07-20; the deep collector is not
running continuously (see Process deviation below) — `run_cycle.py` is
intended to be re-run roughly hourly by the human (e.g. via `/loop`), not left
as an unattended `--loop` process.

**Process deviation from the roadmap (human decision, 2026-07-20):** the human
chose not to block Phase 1 work on the Phase 0 go/no-go gate — assume the
sale-inference signal is correct for now, build ahead, and correct later if
proven wrong. This also replaced the 48h validation *collection window*
itself: rather than one continuous 48h `fetch_snapshot.py --loop` run plus the
in-game verification protocol, the human wants the full pipeline (poll → scan
→ diff → snipe-check) re-run roughly hourly via `run_cycle.py`, matching
Blizzard's real dump cadence, indefinitely — i.e. this is now the intended
*ongoing* operating mode, not a bounded validation run. `VALIDATION.md` and
the in-game verification protocol are deprioritized, not scheduled. Risk this
accepts: `diff_snapshots.py` classification (`inferred_sale` especially) is
unvalidated against real seller behavior — no test auctions have confirmed the
cancel-without-relist false-positive rate — so every sold-price percentile
`snipe_check.py` flags things against, and eventually Deal Score, inherits
that risk indefinitely unless the human decides to run the verification
protocol later.

Not yet present: sell/scan realm config split (manual `--exclude`/`--items`
flags stand in for now), retention, CI, `VALIDATION.md`. Whether Phase 3 still
needs a per-item transferability flag at all is open (see the 2026-07-23
correction above — AH listings are guaranteed unsoulbound, so the original
"Warbound vs BoP" framing for that flag was wrong).

**Second process deviation (human decision, 2026-07-23):** `dashboard.py` was
pulled forward from Phase 5 (web dashboard) to now, ahead of Phase 3
(appearance/transferability layer) and Phase 4 (Deal Score) — same pattern as
the Phase 0→1 deviation above: deliberate, documented, not silent drift. This
is viable without Phase 3 existing first because the dashboard surfaces the
exact same `snipe_check.CAVEAT` the CLI already prints; it doesn't add or
remove any transferability guarantees. **Deployable, not deployed:**
`dashboard.py` + its `Dockerfile` make the web layer *deployable* — a real
ASGI app (FastAPI/uvicorn), containerizable, API and data-access cleanly
separated — but it is *not deployed or hosted for other users* in this pass.
It still runs locally, single-user, no auth, reading the same local `data/`
directory the CLI tools read. The reasoning CLAUDE.md already gives for
rejecting a cloud-hosted collector (no access to local `.env`/accumulated
`data/` history) applies just as much to the dashboard as currently
configured. Multi-tenancy, real hosting, login, and subscriptions are a
future design pass, gated on the existing payments/ToS guardrail above — no
auth or payment code was added in this pass.

### Scheduled automation

`run_cycle.py --sell 1403` runs hourly via Windows Task Scheduler, task name
**AHSnipePipeline** (created 2026-07-20), through the `run_cycle_task.ps1`
wrapper. Output/errors append to `data/logs/run_cycle_task.log` (separate from
`collector.log`/`scanner.log`, which only get the internal logging module's
messages, not print() output or tracebacks from the run itself).

A **cloud** scheduled agent (Claude routine) was considered and rejected: it
would spin up a fresh isolated checkout with no access to local `.env`
credentials or the local `data/` directory (gitignored by design), so it could
never accumulate the snapshot history `run_cycle.py` depends on. This has to
run on the human's own machine.

Manage the task from an elevated or normal PowerShell:
```
Get-ScheduledTask -TaskName AHSnipePipeline | Get-ScheduledTaskInfo   # status / next run
Start-ScheduledTask -TaskName AHSnipePipeline                         # run now
Disable-ScheduledTask -TaskName AHSnipePipeline                       # pause
Unregister-ScheduledTask -TaskName AHSnipePipeline -Confirm:$false    # remove
```

## Architecture & data layout

```
Blizzard API ──> data/snapshots/{cr_id}/{epoch_ts}.parquet   (immutable, hourly, sell realms only)
                        │ diff consecutive pairs
                        v
                 data/events/{cr_id}.parquet   (derived; recomputed from scratch
                        │                       each run — always safe to delete)
                        v
                 analyze.py DuckDB views (snaps, ev, sales, span)
data/state/{cr_id}.json  — Last-Modified cursor for the sell-realm collector

Blizzard API ──> data/listings/{cr_id}.parquet   (region scanner, ALL EU realms;
                                                    latest sweep only, overwritten,
                                                    no history — buy side)
```

Snapshot schema is `SCHEMA` in `fetch_snapshot.py`; event schema is
`EVENT_SCHEMA` in `diff_snapshots.py`; listing schema is `LISTING_SCHEMA` in
`scan_region.py`. Changing any must handle previously written files (regenerate,
or read with `union_by_name`) — globs assume uniform schema.

## Blizzard API facts (trust these, don't guess)

- OAuth: `POST https://oauth.battle.net/token`, HTTP basic auth with client
  id/secret, `grant_type=client_credentials`. Token lasts ~24h; cached in-process.
- Base `https://eu.api.blizzard.com`; namespace param `dynamic-eu` (auctions,
  realms) or `static-eu` (items, appearances, media).
- Non-commodity AH: `GET /data/wow/connected-realm/{crId}/auctions`. Updates
  roughly hourly. Honor `If-Modified-Since` / `Last-Modified` (implemented; the
  Last-Modified timestamp is the canonical `snapshot_ts`).
- Commodities (region-wide, Phase 2): `GET /data/wow/auctions/commodities`.
  **Different physics:** commodity auctions can be partially bought — the same
  `auction_id` persists with reduced `quantity`. Inference there = quantity
  deltas on surviving ids + disappearances, NOT the gear logic below.
- Realm lookup: `GET /data/wow/search/connected-realm?realms.slug={slug}`.
- Rate limit 36,000 req/h, 100 req/s. Collector uses ~6/h. Headroom is not an
  invitation — stay polite.
- `time_left` buckets: SHORT <30m, MEDIUM 30m–2h, LONG 2–12h, VERY_LONG 12–48h.
  Players list at 12/24/48h durations.
- Prices are **copper** (10,000 = 1 gold) end to end; only format as gold at
  display boundaries.
- Battle pets: item_id 82800 cages + `pet_species_id` / `pet_quality_id` /
  `pet_level` fields.
- `auction_id` is stable for a listing's lifetime → it is the diff key. Seller
  identity is never exposed by the API.

## Inference logic (change only with tests proving equivalence or improvement)

For each auction present in snapshot N but missing in N+1, `classify_pair()`:

1. `buyout IS NULL` → `bid_only_gone` (can't be insta-bought; excluded).
2. `time_left == SHORT` → `likely_expired`.
3. Identical `(item_id, bonus_key, buyout, quantity)` appears among *brand-new*
   auction ids in N+1 → `likely_relisted` (consumed from a Counter, so two
   identical listings need two relists).
4. `gap_seconds >= MIN_REMAINING[time_left]` → `ambiguous` (could have expired;
   also absorbs collector-downtime gaps).
5. Else → `inferred_sale`.

`bonus_key` canonicalizes `bonus_lists` + `modifiers` so identical gear variants
compare equal. **Known blind spot:** a cancel *without* relist is
indistinguishable from a sale; the README verification protocol measures that
noise floor empirically. Ideas to shrink it later (seller-behavior priors,
multi-interval relist windows) belong after Phase 0, not during.

## Conventions

- Python 3.10+, stdlib `argparse` CLIs, minimal deps. No pandas, no ORM.
  DuckDB does the analytics. As of `dashboard.py` (2026-07-23), the project's
  first web framework is in use — FastAPI + uvicorn, three focused deps, no
  Node/npm toolchain (the frontend is one static HTML file + vanilla JS, no
  build step). Every other CLI tool stays framework-free.
- Small modules, pure functions where possible (`classify_pair`, `bonus_key`,
  `rows` are deliberately pure — keep them testable).
- Derived data (`data/events/`) is always recomputed from scratch; never make it
  incrementally stateful without also keeping the idempotent path.
- Collector loop must survive any exception (it guards a multi-day run).
- Update this file and `README.md` whenever commands, schemas, or architecture
  change. Update `PROGRESS.md` (the scannable done/not-done status summary)
  whenever a feature ships or a phase's status changes — it replaced a
  one-off `HANDOFF.md` snapshot precisely because that went stale within days.

## Commands

```
python fetch_snapshot.py --find silvermoon          # realm slug -> cr-id
python fetch_snapshot.py --cr-id 1096 --loop        # collect (48h+)
python diff_snapshots.py --cr-id 1096               # build events
python analyze.py --cr-id 1096 summary --top 30
python analyze.py --cr-id 1096 item 152510 --price 2500000   # copper
python analyze.py --cr-id 1096 trace 152510   # per-auction classifications (verification)
python scan_region.py --exclude 1403          # one sweep of all EU realms except your sell realm(s)
python scan_region.py --exclude 1403 --loop   # sweep hourly, forever
python snipe_check.py --sell 1403             # flag discounted listings vs sell-realm sold prices
python snipe_check.py --sell 1403 --items-file watchlist.txt --min-discount 0.3
python run_cycle.py --sell 1403               # one full pass: poll+scan+diff+snipe-check
                                               # re-run hourly, e.g. `/loop 1h python run_cycle.py --sell 1403`
python dashboard.py --sell 1403               # live web dashboard on http://127.0.0.1:8000
                                               # (read-only; run_cycle.py/Task Scheduler must still
                                               # be producing data for it to show anything fresh)
```

## Human-only tasks (never attempt; ask and wait)

- Creating the Battle.net API client and filling `.env`.
- Keeping the collector running on their machine for the 48h window.
- All in-game actions, including the verification protocol in `README.md`
  (posting, cancelling, expiring, and buying test auctions) and reporting results.
- Any monetization/ToS decision.

## Roadmap — execute top to bottom, don't skip ahead

### Phase 0 — validate the signal (NOW)
1. `git init`; add `.gitignore` (`.env`, `data/`, `__pycache__/`, `.venv/`,
   `*.pyc`); initial commit.
2. Formalize the synthetic fixture into `tests/test_diff.py` (pytest). Two
   snapshots, gap 3600s, expected results exactly:
   - VERY_LONG with buyout vanishes, no relist → `inferred_sale`
   - LONG vanishes, identical listing reappears under new auction_id → `likely_relisted`
   - SHORT vanishes → `likely_expired`
   - MEDIUM vanishes (3600 ≥ 1800) → `ambiguous`
   - bid-only VERY_LONG vanishes → `bid_only_gone`
   - a surviving auction produces no event
   Add edge cases: oversized gap downgrades LONG/VERY_LONG to `ambiguous`; two
   identical vanishing listings + one relist → one `likely_relisted` + one
   `inferred_sale`.
3. Robustness pass on the collector: std-lib `logging` to console + rotating
   file, backoff on 429/5xx, catch malformed-JSON responses. Keep it small.
4. After the human's 48h run and in-game protocol: analyze their reported
   results, write `VALIDATION.md` — observed false-positive class, relist-catch
   success, per_day vs TSM regional for 3 liquid items.
   **Gate: only proceed to Phase 1 if liquid-item per_day lands within ~2x of
   TSM and the relist heuristic caught the test repost.**

### Phase 1 — cross-realm engine + hardening
Started ahead of the Phase 0 gate (human decision, 2026-07-20 — see Current
state). The Warbands market structure (see section above) drives this phase:
- Config split: **sell realms** (deep hourly snapshots + sale inference, as
  today) vs **scan realms** (everything else in the region). *Not yet done* —
  `scan_region.py --exclude` is a manual stand-in for now.
- ~~Region scanner~~ **done** (`scan_region.py`): sweeps all EU connected
  realms, keeps only the *latest* listings per realm
  (`data/listings/{cr_id}.parquet`, overwritten each sweep — no history, no
  diffing on the buy side).
- ~~First snipe-check CLI~~ **done** (`snipe_check.py`): joins scan-realm
  listings against sell-realm sold-price percentiles + sales/day; flags
  listings below a discount threshold net of the 5% AH cut. This is the
  end-to-end "validated snipe" proof, with the caveat that it can't yet tell
  Warbound/BoE items from BoP ones (needs Phase 3's transferability flag) —
  `--items`/`--items-file` are the manual-curation workaround until then.
- Hardening carried over: retention on sell-realm snapshots (compact >7 days
  into daily per-item aggregates before deleting), systemd unit / Windows Task
  Scheduler examples in README, `--since` flag for incremental diffing if event
  rebuilds get slow.

### Phase 2 — commodities feed
Region-wide collector + quantity-delta inference (see API facts). Separate
schema; do not force gear and commodities into one table.

### Phase 3 — appearance layer
itemId → appearanceId via wago.tools DB2 exports (`ItemModifiedAppearance`),
cached locally; static API as fallback. Per appearance: count of source items,
obtainability flag (manual curation acceptable at first), region-wide AH
scarcity. This is the differentiator — design the schema carefully.
**Transferability flag, reconsidered (2026-07-23):** the original plan was a
per-item "warband transferability flag" (warbound vs BoP). That framing was
wrong — AH listings are guaranteed unsoulbound (BoP can't be listed), so
warband-bank transfer isn't item-dependent; the only real risk is equipping/
using the item before moving it, which is a *usage* caveat, not a per-item
data flag. Decide at Phase 3 start whether any per-item flag is still needed
(e.g. for genuine edge cases like Unique-equipped or quest-bound items) or
whether the CAVEAT text alone covers it.

### Phase 4 — deal score + Discord alerts (first paid feature)
Score = f(discount vs the *sell realm's* sold-price percentile net of 5% AH
cut, sell-realm sales_per_day, appearance scarcity), attached to a route: buy
realm → sell realm. Webhook alert engine with per-user config (their sell
realms + watchlist). Payments require the human's explicit ToS sign-off first.

### Phase 5 — free companion addon + web dashboard
Addon overlays Deal Score on tooltips/sniper results (free, per Blizzard
policy); dashboard is the premium surface. **The dashboard's local, read-only
form (`dashboard.py`) was pulled forward to 2026-07-23** — see the second
process deviation note above. What's still Phase 5, not done: the free addon,
and turning the dashboard into an actual multi-tenant premium surface (auth,
subscriptions, hosting) rather than a local single-user tool.

## Definition of done for the current milestone

- pytest suite green in CI-less local runs (`pytest -q`).
- 48h of live snapshots collected without collector death.
- `VALIDATION.md` written with real numbers and a clear go/no-go call.
