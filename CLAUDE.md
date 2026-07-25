# CLAUDE.md — WoW AH Snipe Validator (EU retail)

Agent-facing project brief. `README.md` is the human-facing version — keep both in
sync when architecture or commands change. `HISTORY.md` holds the full
session-by-session incident log this file used to carry inline — this file
is the **current-state reference only**: what's true now, not how we got
here. If you're debugging something and want the "why" behind a constant,
threshold, or design choice, search `HISTORY.md` for the item id or file
name mentioned in a comment near it.

## What this project is

A **validation layer for WoW auction-house sniping**, not another sniper.
Existing tools (TSM Sniper, Auctionator) flag items cheap relative to
*listing* prices. This project deep-collects one or more sell realms'
auction history and separately scans every other EU realm's current
listings, flagging a listing elsewhere that's cheap relative to your sell
realm's own current cheapest listing for that item/variant, net of the 5%
AH cut.

**Pricing model note (2026-07-25, see HISTORY.md for the full incident
chain)**: sell price used to be an *inferred sold-price percentile* from
snapshot-diffing (the product's original differentiator — "does this item
actually sell," not just "is it listed cheap"). After three separate live
production pricing bugs traced to the same design flaw (troll/camped
listings corrupting the inference), the human made an explicit, discussed
decision to replace it: sell price is now simply the sell realm's own
current cheapest live listing — directly observable, no classification, no
misclassification possible by construction. This makes a "snipe" a
listing-to-listing comparison, the same thing TSM Sniper/Auctionator
already do — a deliberate, accepted tradeoff, not an oversight. The
underlying snapshot-diff classification engine (`diff_snapshots.py`) still
runs and is unaffected; it could back a future liquidity/confidence signal
without being on the pricing path again.

Business model (decided, don't revisit without the human): free in-game addon
(Blizzard requires addons to be free) + paid external data service — Discord
alerts, dashboards, custom filters. Competitors: TSM (coarse regional sale
rates, hostile UX), Saddlebag Exchange (alerts). Our edge: cross-realm snipe
routing + (eventually) appearance-level intelligence.

**Free tier on the web dashboard** (added 2026-07-25): a logged-in-but-
unsubscribed account can see the dashboard with real, capped data instead of
being bounced to `/subscribe` with zero preview — see `dashboard.py`'s
`SNIPE_TIER_CAPS` in the file table below.

## Market structure (Warbands — core to the product)

Since The War Within, the warband bank makes **gold account-wide** and lets
**unsoulbound BoE items move between a player's characters on any realm**. The
gear/transmog AH is still per connected realm (separate listings, buyer pools,
prices) — so cheap listings rot on low-pop realms while hubs pay full price.
That asymmetry IS the product:

- The user picks **sell realms** (their high/full-pop hub realms). We
  deep-collect hourly snapshots there.
- A lightweight **region scanner** sweeps every other EU connected realm's
  current listings (no history needed on the buy side).
- **Validated snipe** = a listing on any realm priced well below the sell
  realm's own current cheapest listing for that item/variant, net of the 5%
  AH cut.
- An AH listing is guaranteed unsoulbound — BoP items cannot be listed, full
  stop. The only remaining transfer risk is *usage*, not per-item data: don't
  equip/use an item before moving it through the warband bank, or it locks to
  that character. No per-item Warbound/BoP/Unique-equipped flag is needed or
  planned — every item this tool surfaces is by definition AH-listed, so
  there's no non-AH item in scope for such a flag to ever matter (closed,
  not just deferred, 2026-07-24).

Rate-limit math holds: deep collection ~6 req/h per sell realm; scanning all
~100 EU connected realms hourly is ~100 dumps/h against a 36,000 req/h limit.

**Current phase: 0 — the sale-inference signal's validation gate was
explicitly skipped** (human decision, 2026-07-20) to build ahead; the
classification engine (still real, still running) has never been checked
against real seller behavior. See "Roadmap" below.

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
- **Never rotate/swap the live Stripe secret key without the human present**,
  even when otherwise told to keep working autonomously (human-only task,
  see below).
- No "WoW"/"Warcraft" in product branding; "for World of Warcraft" as a
  description is the accepted form.

## Current state

| File | Purpose |
|---|---|
| `blizz.py` | `.env` loader, OAuth client-credentials token, `api_get()`, realm-slug↔id lookups: `find_connected_realm`, `list_connected_realms()`, `connected_realm_population()`, `connected_realm_slugs()`/`connected_realm_realms()` |
| `fetch_snapshot.py` | Sell-realm collector CLI: polls one connected realm, writes hourly parquet snapshots (If-Modified-Since aware); backs off on 429/5xx, skips malformed-JSON bodies. Owns `bonus_key()` (canonical variant string, stored/displayed as-is), `parse_bonus_key(bk) -> dict` (read-only tokenizer — `{"bonus_ids": [...], "mods": {type: value}}` — shared by `market_key()`'s own type-28 check and `dashboard._parse_variant()`'s display logic), and `market_key(bk, base_level=None, noise_bonus_ids=None)` (coarser matching-only key — see "Inference logic" below). `ilvl_plausible(claimed, base_level)` — `claimed <= base_level * ILVL_PLAUSIBILITY_MULTIPLE (3) and claimed <= ILVL_ABSOLUTE_MAX (1000)`, both guards required (ratio catches low-base-level junk, absolute cap catches high-base-level junk — see HISTORY.md for the two real cases, items 237468 and 164353, that motivated each). `BONUS_NOISE_*` constants tune `snipe_check._detect_noise_bonus_ids()`'s structural noise test (see "Inference logic"). |
| `scan_region.py` | Region scanner: sweeps every EU connected realm's *current* listings (`--exclude` to skip sell realms already deep-collected) into `data/listings/{cr_id}.parquet`, overwritten each sweep via a temp-file + `os.replace()` atomic rename (fixed 2026-07-25 after a real production crash — a reader could open the file mid-write). |
| `snipe_check.py` | Joins `data/listings/*.parquet` against the sell realm's own **current cheapest live listing** (not a sold-price percentile — see "What this project is" above) on `(item_id, market_key(bonus_key), pet_species_id, pet_quality_id)`. `find_snipes()` params: `items`/`min_discount`/`min_gold`/`max_gold`/`min_sell_now`/`max_appearance_sources`/`max_per_item`/`top`/`sort`. `check_data_ready(sell) -> str \| None` is the shared "events exist + listings swept" precondition, used by both the CLI and `dashboard.py`'s `/api/snipes`. `_populate_market_keys(con)` is a thin orchestrator over three helpers: `_detect_noise_bonus_ids(con)` (structural per-craft-noise detection in `b:` bonus ids — see "Inference logic"), `_resolve_base_levels(con)` (Blizzard API lookups for type-28 plausibility, capped at `MAX_BASE_LEVEL_LOOKUPS_PER_CALL=500` per call, prioritizing the sell realm's own items — see "Real production outage" below), and `_load_market_key_table(con, ...)` (bulk Arrow load of the computed `market_key` per distinct `(item_id, bonus_key)` pair — `executemany()` was live-timed unusably slow at ~700k rows, the Arrow path loads the same set in ~0.23s). `_filter_by_appearance(rows, max_appearance_sources)` applies the transmog-rarity filter (via `appearance.py`) and excludes `NON_TRANSMOG_INVENTORY_TYPES` (profession tool/accessory slots). Every returned row carries `market_key` (dashboard groups by this, not the exact `bonus_key`, so listings that price identically as one pooled market render as one table row) and `appearance_sources`. Prints `snipe_check.CAVEAT` every CLI run (the equip-before-transfer warning). |
| `collect_all.py` | The sole collection path (runs in-process inside `dashboard.py`'s background loop, `ENABLE_BACKGROUND_COLLECTION=true`, Railway only). Deep-collects FULL/HIGH-population EU realms (`deep_collect_realm_ids()`, cached in-process), then an unscoped `scan_region.sweep()` (every EU realm's listings). Runs every ~10 minutes, not hourly (Blizzard publishes at no fixed clock time). `prune_old_snapshots(cr, retention_days)` keeps 2+ snapshots and anything within `retention_days`. `_effective_retention_days(total_bytes, target_days=RETENTION_DAYS(14), safety_bytes=SAFETY_BYTES(~4.5GB), min_days=MIN_RETENTION_DAYS(2))` — added 2026-07-25 after confirming `RETENTION_DAYS=14` alone projected past Railway's ~4.9GB volume cap: below the safety budget, prunes at the full 14-day target; above it, shrinks the effective day count proportionally (e.g. 2x over budget → ~half the target days), floored at 2 days (matching `prune_old_snapshots()`'s own always-keep-2 floor). Measured once per `collect_all()` cycle via `_total_snapshot_bytes()`, applied uniformly to every realm pruned that cycle. `_prewarm_item_base_levels()` resolves up to `PREWARM_BASE_LEVEL_CAP=1000` more type-28 item base levels per cycle in the background, regardless of user traffic, so `snipe_check._resolve_base_levels()`'s cache converges over a few hours instead of depending on dashboard loads. |
| `dashboard.py` | FastAPI app, read-only web layer over `snipe_check.find_snipes()`. `GET /api/snipes` mirrors the CLI's flags as query params, returns JSON rows + raw-copper prices (`buy_copper`/`sell_copper`) + `market_key` (unconditional) + (when `names=true`) `icon`/`quality_color`/`item_class`/`item_subclass`/`is_profession_item`/a smart `variant` summary (`ilvl NNN` only when `_variant_label()`'s ilvl-plausibility check passes, via `fetch_snapshot.ilvl_plausible()`/`parse_bonus_key()`, else a bonus-count fallback — `variant_raw` always carries the raw string). Runs the whole query via `await asyncio.to_thread(...)` (see "Real production outage" below — `_resolve_base_levels()` can make blocking Blizzard calls mid-query). **Auth/tiers**: `current_active_user` gates every `/api/*` route (not `current_subscribed_user` — free tier, see below). `SNIPE_TIER_CAPS`/`_snipe_cap(user)`: 250 (logged in, no subscription) / 2000 (active subscription) / 5000 (superuser). `_enforce_realm_lock(user, sell, session)`: a free-tier (non-subscribed, non-superuser) account is locked to the *first* sell realm it ever queries (`db.User.locked_sell_realm`, written once); querying a different realm afterward is a 403. `GET /api/realms`/`GET /api/status`/`GET /api/me` (the last exposes `locked_sell_realm`). `GET /api/log/realms`/`GET /api/log?sell={cr}` and `GET /pricing`/`GET /log` are the deliberately unauthenticated routes (realm names/retrieval timestamps/pricing aren't the paid product). `GET /` serves `static/dashboard.html`. `python dashboard.py --sell 1403` runs it locally on `127.0.0.1:8000` — leave `ENABLE_BACKGROUND_COLLECTION` unset locally. |
| `static/dashboard.html` | Single static file, vanilla JS, no build step. Light "assay ledger" visual identity with a dark-mode toggle (`localStorage`-backed, shared pre-paint `<head>` script across all six pages) — see `HISTORY.md`'s "UI design pass" for how this was chosen. Quality-colored item-icon rings, gold/silver/copper coin-icon formatting, a hover tooltip, click-icon-for-Undermine-Exchange-link. **Client-side filtering architecture** (2026-07-24): fetches one loose batch per sell realm (`fetchBatch()`, `BATCH_TOP=5000` — the ceiling across every tier, the server clamps down to the real cap via `_snipe_cap()`) and re-filters/re-sorts entirely in the browser (`applyFilters()`/`renderTable()`/`compareRows()`) — only a sell-realm switch, an item-id change, the 60s auto-refresh timer, or an explicit Refresh click re-fetch. `checkForUpdates()` (auto-refresh + initial load) checks the cheap `/api/status` timestamp first and only re-fetches `/api/snipes` when it's actually advanced; the last-fetched batch is cached in `localStorage` (keyed per sell realm + item filter, cleared on logout) and painted instantly on load. Filter rail: discount%/gold range/sell-now/max-per-item/unique-transmog/8-way item-class checkboxes (OR'd together), all client-side. Rows group by `market_key` (`groupKey()`), best-discount first, with a `▾ N` expand toggle. Free-tier accounts get a `requirePick` realm dropdown (no default pre-selected — an earlier version silently locked new accounts to the server's site-wide default realm on first load, fixed 2026-07-25). |
| `Dockerfile` / `.dockerignore` / `docker-entrypoint.sh` | Packages the web app into a container; entrypoint runs `alembic upgrade head` then `exec python dashboard.py`. Reads `PORT` (Railway-injected) and `DEFAULT_SELL` (UI prefill only) from env. |
| `tests/` | pytest suite (`pytest -q`; root `conftest.py` makes top-level modules importable). Real duckdb/pyarrow throughout, no mocking of the data layer; live Blizzard calls are stubbed. `isolate_item_names_cache`/`isolate_appearance_cache` autouse fixtures redirect cache paths into `tmp_path` so tests never touch the real gitignored caches. **Always run both** `pytest -q` and `env -u DATABASE_URL pytest -q` before pushing — CI has no `DATABASE_URL` set, and a local `.env` can mask a missing test-fixture override (see "CI incident" note under `dashboard.py`'s history in `HISTORY.md`). |
| `diff_snapshots.py` | **Core IP**, the snapshot-diff classification engine — still runs, no longer drives pricing (see "What this project is" above). `relist_key(r)` — non-price identity `(item_id, market_key(bonus_key), pet_species_id, pet_quality_id, quantity)`; price is matched separately via `_find_relist_match()` within `RELIST_PRICE_TOLERANCE` (±15%, added 2026-07-25) instead of requiring exact equality, so a troll reposting at a nearby joke price still counts as a relist rather than a fake `inferred_sale`. See "Inference logic" below for the full classification rules. |
| `analyze.py` | DuckDB CLI: liquidity summary + per-item sold-price distribution / percentile check / per-auction trace (manual debugging only, not on the pricing path). `connect()` registers `market_key(bk, base_level := NULL)` as a DuckDB macro (three helper macros: `_ilvl28_value`/`_ilvl28_implausible`/`_strip_type`) — the SQL-side mirror of `fetch_snapshot.market_key()`'s base_level behavior, kept honest by `tests/test_market_key.py`'s parity check. `noise_bonus_ids` is Python-only, not mirrored in SQL (see `snipe_check.py`'s helpers). |
| `appearance.py` | `AppearanceCache`: itemId → transmog-appearance rarity (`source_count` — how many distinct item ids grant the same appearance region-wide), cached at `data/appearances.json`, source is wago.tools' `ItemModifiedAppearance` DB2 export (`python appearance.py --refresh`, manual/periodic — not wired into the Railway background loop, since wago.tools is outside this project's Blizzard rate-limit budget). Display/filter-only, never-raises. A v1 rarity proxy, not a real obtainability model. |
| `item_names.py` | `NameCache`: display/filter-only, never-raises lookups backed by the static item/pet API, cached at `data/item_names.json`. `.get()` name, `.icon()`, `.quality_color()`, `.base_level()`, `.inventory_type()`, `.item_class()`/`.item_subclass()` (Blizzard's official ids, confirmed live via `GET /data/wow/item-class/index`). `.ensure_many(ids, max_workers, limit)` resolves concurrently, used by `snipe_check._resolve_base_levels()` and `collect_all._prewarm_item_base_levels()`. All fields backfill onto an older cache entry missing them, self-healing an old cache file instead of returning `None` forever. |
| `db.py` | Async SQLAlchemy for the *relational* data only (users/sessions/subscription state) — separate from the parquet+DuckDB AH data layer. `User` model: FastAPI-Users base fields + `stripe_customer_id`/`stripe_subscription_id`/`subscription_status`/`subscription_current_period_end` (written only by `billing.py`'s webhook) + `locked_sell_realm` (nullable, written only by `dashboard._enforce_realm_lock`). Tests override the session dependency with SQLite. |
| `auth.py` | FastAPI-Users wiring: email/password register+login, cookie-based sessions. `current_active_user` gates login-only routes. `has_active_subscription(user)` — single source of truth for "unrestricted" (active subscription OR superuser), used by both `_enforce_realm_lock` and anywhere else that needs the same check. `current_subscribed_user` (402 if not subscribed) is defined but currently unused by any route (free tier superseded it) — kept as a legitimate dependency for a future genuinely-subscriber-only route. `COOKIE_SECURE` env toggle (`false` in dev `.env`, unset/secure in production). |
| `billing.py` | **Live Stripe mode** (human decision — deployed straight to live, no test-mode verification pass). `POST /billing/checkout` creates a Checkout Session for the single €4.99/mo price; `POST /billing/webhook` verifies the Stripe signature and handles `checkout.session.completed`/`customer.subscription.updated`/`customer.subscription.deleted`, the only writer of the user's subscription fields. Still on the full `sk_live_...` secret key, not a restricted key (see "Roadmap" — human-only to change). |
| `alembic/`, `alembic.ini` | DB migrations. `env.py` reads `DATABASE_URL` from the environment. |
| `static/login.html`, `register.html`, `subscribe.html`, `profile.html`, `log.html`, `pricing.html` | Plain HTML/JS, same no-build-step convention, same visual identity/dark-mode as `dashboard.html`. `profile.html` shows subscription status + Stripe customer portal link. `subscribe.html` explains what a subscription changes vs the free tier, links to `/pricing`. `log.html` — public retrieval-time log. `pricing.html` — public Free-vs-Subscriber comparison + FAQ, doesn't advertise the internal superuser/5000 tier (founder/admin headroom, not purchasable). |
| `requirements.txt` | `requests`, `pyarrow`, `duckdb`, `fastapi`, `uvicorn`, `httpx`, `fastapi-users[sqlalchemy]`, `sqlalchemy[asyncio]`, `asyncpg`, `aiosqlite` (tests only), `alembic`, `stripe`, `pytest-asyncio`. |
| `.env.example` | `BLIZZ_CLIENT_ID`, `BLIZZ_CLIENT_SECRET`, `BLIZZ_REGION=eu`, `STRIPE_PUBLISHABLE_KEY`/`STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET`/`STRIPE_PRICE_ID`/`STRIPE_PRODUCT_ID`, `SECRET`, `COOKIE_SECURE`, `DATABASE_URL`. |
| `.claude/commands/`, `.claude/skills/project-review/` | Reusable Claude Code tooling (added 2026-07-25): `/railway-status` (deploy/CI/volume status, read-only), `/railway-debug <command>` (runs a command against live production data via `railway ssh`), `/ship` (test both envs → commit → push → watch CI → confirm Railway deploy → optionally live-verify), `project-review` skill (a repo-specific pre-push checklist — market_key Python/SQL parity, copper-vs-gold units, the CI-env test mismatch class of bug, ToS/secrets/Stripe-key guardrails, frontend-verify-in-a-real-browser convention). Keep these current the same way as this file — if the Railway CLI invocation changes, update the command file, not just this note. |

Verified: `pytest -q` green (see individual test files for current counts —
run it, don't trust a number written here, it goes stale immediately).
Full history of every session's test-count progression is in `HISTORY.md`
if you want it; not tracked here on purpose.

## Process deviations from the roadmap (all human decisions, not silent drift)

The roadmap below says "execute top to bottom." That hasn't happened
literally — each skip-ahead was a deliberate, human-directed call, not
drift, and none of them reversed a guardrail. Summary (full reasoning for
each is in `HISTORY.md` if you need it):

- **Phase 0's 48h validation gate was skipped** (2026-07-20) to build ahead;
  risk accepted indefinitely unless the human decides to run the
  verification protocol later. The classification engine (`diff_snapshots.py`)
  remains unvalidated against real seller behavior.
- **Phase 5's dashboard was pulled forward** (2026-07-23) ahead of Phase 3;
  viable because the dashboard surfaces the same `snipe_check.CAVEAT` the
  CLI already prints, no new transferability guarantees needed.
- **The hosted multi-tenant pivot happened in one day** (2026-07-23):
  auth, live Stripe billing (skipping test-mode verification), scoped
  server-side collection, and CD all shipped same-day — a higher-risk-
  tolerance path than the roadmap implied, deliberate and human-directed.
- **Phase 3 groundwork (appearance rarity) started ahead of Phase 1's
  remaining hardening** (2026-07-23) — human asked for it directly.
- **Phase 2 (commodities feed) is explicitly out of scope** (2026-07-24),
  not just deprioritized — no current intent to build it.
- **The sold-price-percentile pricing model was replaced** (2026-07-25) —
  see "What this project is" above; this is a product-shape change, not a
  roadmap skip, but is the single biggest deviation from the original
  design in the project's history.

Not yet present: sell/scan realm config split (manual `--exclude`/`--items`
flags stand in), `--since` incremental diffing, `VALIDATION.md`.

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
`scan_region.py`. Changing any must handle previously written files
(regenerate, or read with `union_by_name`) — globs assume uniform schema.

## Blizzard API facts (trust these, don't guess)

- OAuth: `POST https://oauth.battle.net/token`, HTTP basic auth with client
  id/secret, `grant_type=client_credentials`. Token lasts ~24h; cached in-process.
- Base `https://eu.api.blizzard.com`; namespace param `dynamic-eu` (auctions,
  realms) or `static-eu` (items, appearances, media).
- Non-commodity AH: `GET /data/wow/connected-realm/{crId}/auctions`. Updates
  roughly hourly, at no fixed clock time. Honor `If-Modified-Since` /
  `Last-Modified` (implemented; the Last-Modified timestamp is the canonical
  `snapshot_ts`).
- Commodities (region-wide): `GET /data/wow/auctions/commodities` —
  **Phase 2, explicitly out of scope**, not implemented.
- Realm lookup: `GET /data/wow/search/connected-realm?realms.slug={slug}`.
- Item class/subclass: `GET /data/wow/item-class/index` + per-class
  `itemSubclasses` — confirmed live 2026-07-24: 2=Weapon, 4=Armor,
  1=Container, 19=Profession, 20=Housing, 17=Battle Pets, 12=Quest,
  15=Miscellaneous with subclass 5=Mount.
- Rate limit 36,000 req/h, 100 req/s. Headroom is not an invitation — stay
  polite. `snipe_check._resolve_base_levels()`/`collect_all._prewarm_item_base_levels()`
  are the two places this pipeline makes bulk Blizzard calls; both are
  explicitly capped per call (see their file-table entries above).
- `time_left` buckets: SHORT <30m, MEDIUM 30m–2h, LONG 2–12h, VERY_LONG 12–48h.
  Players list at 12/24/48h durations.
- Prices are **copper** (10,000 = 1 gold) end to end; only format as gold at
  display boundaries.
- Battle pets: item_id 82800 cages + `pet_species_id` / `pet_quality_id` /
  `pet_level` fields. `bonus_key`/`market_key` are empty for pets — matching
  uses the pet identity fields instead.
- `auction_id` is stable for a listing's lifetime → it is the diff key. Seller
  identity is never exposed by the API.

## Inference logic (change only with tests proving equivalence or improvement)

For each auction present in snapshot N but missing in N+1, `classify_pair()`:

1. `buyout IS NULL` → `bid_only_gone` (can't be insta-bought; excluded).
2. `time_left == SHORT` → `likely_expired`.
3. A matching `(item_id, market_key(bonus_key), pet_species_id,
   pet_quality_id, quantity)` listing appears among brand-new auction ids in
   N+1, **at a buyout within ±15% of the vanished listing's price**
   (`RELIST_PRICE_TOLERANCE`, see `diff_snapshots.py`) → `likely_relisted`
   (consumed from a per-key candidate list, so N identical vanished listings
   need N matching relists).
4. `gap_seconds >= MIN_REMAINING[time_left]` → `ambiguous` (could have expired;
   also absorbs collector-downtime gaps).
5. Else → `inferred_sale`.

**Known blind spot**: a cancel *without* relist is indistinguishable from a
sale. Never formally validated against real seller behavior (Phase 0's gate
was skipped). This is one of several reasons the pricing model no longer
depends on this classification (see "What this project is" above) — the
classification itself still runs and is still real, useful signal for
`analyze.py`'s manual debugging commands.

### `market_key()` — the matching-only coarsening of `bonus_key`

`bonus_key()` is pure and canonical — never changes what's stored/displayed.
`market_key(bk, base_level=None, noise_bonus_ids=None)` is a *separate*,
coarser key used only for matching/grouping (relist detection, the
buy/sell join in `snipe_check.py`) — real crafted-item variance and
Blizzard's undocumented per-craft/per-instance ids otherwise fragment one
liquid market into dozens of near-unique buckets.

Three independent things it strips, **unconditionally except type 28**:
- `MARKET_IGNORE_MODIFIER_TYPES = {9, 42, 44}` — always stripped. 42 is a
  continuous per-craft stat roll, 44 a per-instance serial (confirmed
  sequential in live data). Type 9 was confirmed by a human not to affect
  transmog appearance before being added (2026-07-24) — unlike 42/44, this
  wasn't self-evident from the data alone.
- Modifier type 28 (claimed "item level") — **conditionally** stripped, only
  when `base_level` is supplied AND the claimed value fails
  `ilvl_plausible()`. A plausible value on current-content ilvl-scaling gear
  is genuinely a different market and is left untouched. `base_level=None`
  (the default, and what `diff_snapshots.relist_key()` still passes) always
  means "don't strip" — never "assume junk."
- `noise_bonus_ids` (a per-item `frozenset[int]` of `b:` bonus-list ids) —
  Python-only, computed by `snipe_check._detect_noise_bonus_ids()` via a
  **structural** test (not a frequency threshold — a flat cutoff was tried
  and live-disproven, see `HISTORY.md`): a bonus-list value is treated as
  real only if it has a *partner* — reliably co-occurring with another
  specific value (a companion pair), or belonging to a small mutually-
  exclusive set that jointly covers most of an item's listings (a
  partition). Per-craft noise has neither shape.

Any **new** modifier type discovered to be junk needs either strong
corroborating evidence from real data (e.g. identical troll price across
different values) or explicit human confirmation it doesn't affect the
thing it might affect (transmog appearance) before being added to the
unconditional ignore set — don't assume.

Mirrored as a SQL macro in `analyze.connect()` (`MARKET_KEY_MACRO_SQL`,
three helper macros) for DuckDB-side grouping — two independent
implementations kept honest by `tests/test_market_key.py`'s parity check
(runs the same real-item vectors through both, asserts identical results).
`noise_bonus_ids` is **not** mirrored in SQL (Python-only, see
`snipe_check.py`'s helpers) — the parity test only covers the base_level
argument shape.

**If you touch any of this**: update both implementations, add a real
(not invented) test vector to `tests/test_market_key.py`, and check the
`project-review` skill's matching-logic checklist before shipping.

## Real production outage, lesson for next time (2026-07-25)

`snipe_check._resolve_base_levels()` can make blocking Blizzard API calls.
`dashboard.py`'s `api_snipes()` is an `async def` route but was calling it
directly on the event loop thread — on a cold cache, hundreds of sequential
blocking calls froze the *entire* single-process server, including
unrelated routes, for the call's full duration. Fixed via
`asyncio.to_thread(...)`. **Full incident in `HISTORY.md`.**

**Lesson for next time a route gains a synchronous, possibly-slow
dependency** (a new network call, a large one-time computation): ask
whether it can block *other* requests, not just whether it's correct or
fast on a warm cache. An `async def` FastAPI route does not protect you
from this by itself — it only helps if the blocking work is actually
offloaded. There is still no test coverage for "does this route block the
event loop" — worth a regression test (a slow stub swapped into
`_resolve_base_levels()`, asserting a concurrent lightweight request still
completes quickly) if this class of bug recurs.

## Conventions

- Python 3.10+, stdlib `argparse` CLIs, minimal deps. No pandas, no ORM.
  DuckDB does the analytics. FastAPI + uvicorn is the one web framework in
  use (three focused deps, no Node/npm toolchain — the frontend is static
  HTML + vanilla JS, no build step). Every other CLI tool stays
  framework-free.
- Small modules, pure functions where possible (`classify_pair`, `bonus_key`,
  `parse_bonus_key`, `rows` are deliberately pure — keep them testable).
- Derived data (`data/events/`) is always recomputed from scratch; never make it
  incrementally stateful without also keeping the idempotent path.
- Collector loop must survive any exception (it guards a multi-day run).
- Update this file and `README.md` whenever commands, schemas, or architecture
  change. Update `PROGRESS.md` (the scannable done/not-done status summary)
  whenever a feature ships or a phase's status changes. Put detailed
  incident narrative in `HISTORY.md`, not inline here — that's the whole
  point of the 2026-07-25 docs restructure that produced this file's current
  shape.

## Commands

Standalone debugging/inspection tools — none of them are how the product
actually runs (`collect_all.py` inside the deployed app is).

```
python fetch_snapshot.py --find silvermoon          # realm slug -> cr-id
python fetch_snapshot.py --cr-id 1096 --loop        # collect (48h+), local debugging only
python diff_snapshots.py --cr-id 1096               # build events
python analyze.py --cr-id 1096 summary --top 30
python analyze.py --cr-id 1096 item 152510 --price 2500000   # copper
python analyze.py --cr-id 1096 trace 152510   # per-auction classifications (verification)
python scan_region.py --exclude 1403          # one sweep of all EU realms except your sell realm(s)
python snipe_check.py --sell 1403             # flag discounted listings vs sell-realm's current cheapest
python snipe_check.py --sell 1403 --items-file watchlist.txt --min-discount 0.3
python dashboard.py --sell 1403               # local dev server on http://127.0.0.1:8000
                                               # (leave ENABLE_BACKGROUND_COLLECTION unset locally)
```

## Human-only tasks (never attempt; ask and wait)

- Creating the Battle.net API client and filling `.env`.
- All in-game actions, including the verification protocol in `README.md`
  (posting, cancelling, expiring, and buying test auctions) and reporting results.
- Any monetization/ToS decision.
- Rotating/swapping the live Stripe secret key (`sk_live_...`), even a
  planned improvement like a restricted key — ask and wait for the human to
  be present.

## Roadmap — execute top to bottom, don't skip ahead

(See "Process deviations" above for where this hasn't happened literally,
and why each skip was a deliberate call.)

### Phase 0 — validate the signal
**Gated, skipped** (2026-07-20). Formalized the synthetic fixture into
`tests/test_diff.py` (done). The 48h collection + in-game verification
protocol + `VALIDATION.md` write-up were never run — see `README.md`'s
"Verification protocol" for what running it would involve.

### Phase 1 — cross-realm engine + hardening
**Mostly done.** Region scanner and snipe-check CLI both shipped and are
the end-to-end product. Remaining: sell/scan realm config file (manual
`--exclude`/`--items` flags stand in), `--since` incremental diffing if
event rebuilds get slow.

### Phase 2 — commodities feed
**Out of scope** (human decision, 2026-07-24) — not being pursued.

### Phase 3 — appearance layer
**Started ahead of Phase 1's remaining hardening** (2026-07-23). Done:
itemId → appearanceId mapping + source-item count (`appearance.py`), wired
into `snipe_check.py`/`dashboard.py` as a "unique transmog" filter. Not
done: static-API fallback, real obtainability flags (`source_count` is a
rarity proxy, not a farmability check — known to diverge from Wowhead's own
"same model as" data on at least one item), region-wide AH scarcity *of
currently listed* appearances. The originally planned "warband
transferability flag" is **closed, no flag will be built** — see "Market
structure" above.

### Phase 4 — deal score + Discord alerts (first paid feature)
Not started — blocked on Phase 3 data. Score = f(discount vs current
cheapest listing, appearance scarcity), attached to a route: buy realm →
sell realm. Payments require the human's explicit ToS sign-off first
(already read once, 2026-07-23 — re-check before turning on any *new*
billing surface if time has passed).

### Phase 5 — free companion addon + web dashboard
**The dashboard half is done** (pulled forward 2026-07-23 — hosted,
multi-tenant, auth, live Stripe subscriptions, free tier, all shipped).
Only the free in-game addon itself remains, not started.

## Definition of done for the current milestone

- `pytest -q` **and** `env -u DATABASE_URL pytest -q` both green.
- Any change to `market_key()`/`bonus_key()`/`relist_key()` has a matching
  SQL-macro update (if applicable) and a real test vector.
- Any frontend change verified in an actual browser (see the
  `project-review` skill's checklist), not just by reading the diff.
- `CLAUDE.md`/`PROGRESS.md` updated for anything that changes current
  state; detailed narrative goes in `HISTORY.md`, not inline here.
