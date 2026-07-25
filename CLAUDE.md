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

**Free tier added to the web dashboard itself, 2026-07-25** (human
decision) — a wrinkle on the model above, not a reversal of it: a
logged-in-but-unsubscribed account can now see the dashboard with real
(capped) data instead of being bounced straight to `/subscribe` with zero
preview. See `dashboard.py`'s `SNIPE_TIER_CAPS` entry below for the exact
tiers and rationale.

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
| `fetch_snapshot.py` | Sell-realm collector CLI: polls one connected realm, writes hourly parquet snapshots (If-Modified-Since aware); logs to console + rotating `data/logs/collector.log`, backs off on 429/5xx, skips malformed-JSON bodies. Also owns `bonus_key()` (raw canonical variant string, stored/displayed as-is) and `market_key()` (added 2026-07-23, coarser matching-only key, ignore-list extended to modifier type 9 on 2026-07-24 — see "Inference logic" below). **`ilvl_plausible()`/`ILVL_PLAUSIBILITY_MULTIPLE`/`ILVL_ABSOLUTE_MAX` moved here from `dashboard.py` (2026-07-25)** — single source of truth now that `market_key()` also needs the plausibility check (an optional `base_level` arg conditionally pools implausible type-28 values, not just unconditionally-ignored types — see "Inference logic" below for item 164353, the real case that motivated this and dropped the multiplier from 5x to 3x) |
| `scan_region.py` | **Phase 1.** Region scanner: sweeps every EU connected realm's *current* listings (no history, `--exclude` to skip sell realms already deep-collected) into `data/listings/{cr_id}.parquet`, overwritten each sweep. Reuses `bonus_key`/`get_auctions_with_backoff` from `fetch_snapshot.py`. Logs to `data/logs/scanner.log` |
| `snipe_check.py` | **Phase 1.** Joins `data/listings/*.parquet` (any realm but the sell realm) against the sell realm's `sales` view (from `analyze.connect`) on `(item_id, market_key(bonus_key))`; flags listings below a sold-price percentile net of the 5% AH cut, above a liquidity floor. `--items`/`--items-file` restrict to a hand-curated watchlist. `--min-gold`/`--max-gold` (added 2026-07-23) filter on the *buy*-side unit price — what you'd actually spend — since that's the number a budget cap means for an AH sniper. `--min-sales` (added 2026-07-23, default 2) is a data-quality floor distinct from `--min-per-day`: requires at least N inferred sales before trusting the percentile at all, since a single sample can be an unverified cancel-without-relist false positive (see "Inference logic" below for the real production case this was caught from). **Sell-price cap (added 2026-07-23, same day):** even `min_sales >= 2` wasn't enough — item 206477 (Warsword of Caer Darrow) had 4 inferred sales but 2 of them were the *same* 149,379g troll listing, dragging the percentile to 75,074g against a real ~700g item. `find_snipes()` now computes `sell_price` as `LEAST(sold_percentile, current_cheapest_live_listing_on_sell_realm)` (from the latest snapshot in the `snaps` view, no inference involved) — you can't realistically sell above what's already listed cheaper on your own sell realm, so this is both a sanity bound on bad inference and a more honest achievable-resale estimate. Only ever pulls the estimate down, never up; a no-op when nothing is currently listed. Exposed as the new `sell_now_g`/`sell_now_copper` fields (CLI and `/api/snipes` both) — the CLI prints the capped `sell_p_g` as before, `sell_now_copper` was added 2026-07-23 alongside the dashboard's own "Sell realm low" column so the frontend has a raw-copper value to format, matching the "copper end to end" convention. **`--max-appearance-sources N`** (Phase 3, added 2026-07-23) filters to items whose transmog appearance (via the new `appearance.py`) is shared by at most N distinct items region-wide — 1 means a genuinely unique look. Applied in Python after the SQL query (the appearance cache is a local JSON file, not a DB table to join against), with the SQL candidate pool widened internally so post-filtering doesn't starve the requested `--top` count. Every row also carries `appearance_sources` (None if the item isn't in the cache) regardless of whether the filter is active, so it's visible for display either way; an item missing from the cache is *excluded* when the filter is actively requested (can't prove something rare that isn't known). **`--max-per-item N`** (added 2026-07-23) caps how many listings of the same item/variant (by `market_key`, not exact `bonus_key`) can appear in the results via a `ROW_NUMBER()` window before the outer `ORDER BY`/`LIMIT`, keeping the highest-discount ones — combine with `--top` for e.g. `--top 500 --max-per-item 1` = up to 500 distinct items, so one popular item's many listings can't crowd out variety. **Matching itself now uses `market_key(bonus_key)`, not exact `bonus_key`** (2026-07-23, see "Inference logic" below) for the `sell_stats`/`sell_now`/buy-sell join — the exact `bonus_key` is still carried on each output row for display. **`--min-sell-now` (added 2026-07-23)** filters on the sell realm's current cheapest live listing (`sell_now_g`), distinct from `--min-gold`/`--max-gold` which filter the *buy*-side price — excludes low-value junk that happens to clear the discount% threshold anyway (e.g. an item that only ever sells for a few silver on the sell realm); a row with no current sell-realm listing is excluded when this is set. **`--max-appearance-sources` now also excludes profession tool/accessory items (added 2026-07-23)** — `NON_TRANSMOG_INVENTORY_TYPES = {"PROFESSION_TOOL", "PROFESSION_GEAR"}`, checked via a new `item_names.NameCache.inventory_type()` lookup (confirmed live against Blizzard's item API: Mining Pick, Blacksmith Hammer, Fishing Pole all return `PROFESSION_TOOL`). Human's reasoning: profession tool/accessory slots aren't part of the visible paperdoll model at all, so a low `appearance_sources` for one of them (trivially "unique" just because few items share that slot) is meaningless noise for a transmog-rarity filter. This makes `--max-appearance-sources` filtering able to incur a Blizzard API call per never-before-seen item (cached after, same cost profile as dashboard's `names=true`). Prints `snipe_check.CAVEAT` every run: an AH listing is guaranteed unsoulbound (BoP can't be listed), so it can always ride the warband bank — just don't equip/use it before moving it. **`_populate_base_levels()`, added 2026-07-25** — `market_key()`'s conditional type-28 pooling needs each candidate item's catalog base level, which requires a Blizzard lookup `find_snipes()` couldn't previously make mid-SQL-query; this runs first, gathers every distinct `item_id` whose `bonus_key` contains a type-28 modifier (across `sales`/`snaps`/`listings`, deliberately ignoring `--items` since `sell_now` is itself unfiltered by design), resolves each via `NameCache.base_level()` (cached after first lookup, same cost profile as `names=true` elsewhere), and populates a `item_base_levels` temp table that `sell_now`/`sell_stats`/`buy` all `LEFT JOIN` against. This is the one place `find_snipes()` can now make a network call where it previously never did — see "Inference logic" below for the full item 164353 writeup. **Saves the `NameCache` incrementally (every 50 items), added 2026-07-25 same day** — after this caused a real production outage (see CLAUDE.md's "Real production outage" section), an interrupted cold-cache run no longer loses all its progress |
| `collect_all.py` | **Stage 4 of the hosted pivot; the sole collection path as of 2026-07-23.** Replaced the local `run_cycle.py` + Windows Task Scheduler (`AHSnipePipeline`), both removed — see "Scheduled automation" below. Deep-collects every FULL/HIGH-population EU realm, not one hand-picked sell realm (scope decided 2026-07-23, not literally all ~100 realms). `deep_collect_realm_ids()` caches the population-filtered realm list in-process (via the new `blizz.connected_realm_population()`); `collect_all()` polls+diffs+prunes each of those, then runs an **unscoped** `scan_region.sweep()` (every EU realm's listings, regardless of population — the cross-realm thesis needs cheap listings from low-pop realms too). `prune_old_snapshots()` — 14-day retention, always keeps 2+ — is no longer a someday-TODO now that this runs indefinitely on a server. Called every ~10 minutes by `dashboard.py`'s background loop (`ENABLE_BACKGROUND_COLLECTION=true`, Railway only) — not hourly, deliberately: Blizzard republishes at no fixed clock time, so polling only once an hour from container boot could sit out of phase with the real update by up to an hour. `fetch_once()`'s `If-Modified-Since` check keeps no-op polls cheap; `collect_all()` only re-diffs a realm when a *new* snapshot actually arrived that cycle, not on every tick |
| `dashboard.py` | **Dashboard (pulled forward from Phase 5, see Process deviation below).** FastAPI app, read-only web layer over `snipe_check.find_snipes()` — does not run the pipeline itself. `GET /api/snipes` mirrors `snipe_check.py`'s CLI flags as query params (including `min_gold`/`max_gold`/`min_sales`, added 2026-07-23), returns JSON rows + the shared `snipe_check.CAVEAT` string (never omitted, even when empty), plus raw-copper prices (`buy_copper`/`sell_copper` — formatting happens client-side per the "copper end to end" convention), `sell_now_g` (added 2026-07-23, the current-lowest-listing cap described in the `snipe_check.py` entry above), `appearance_sources`/`max_appearance_sources` query param (added 2026-07-23, mirrors `snipe_check.py`'s new flag — see its entry above), `max_per_item` query param (added 2026-07-23, same mirroring), `min_sell_now` query param (added 2026-07-23, mirrors `snipe_check.py`'s new flag), `buy_realm_name` (via `blizz.connected_realm_realms()`, in-process cached), and a smarter `variant` summary — `ilvl NNN` parsed from bonus-list modifier type 28 **only when `names=true` and `fetch_snapshot.ilvl_plausible()` accepts the claimed value against the item's own catalog level** (`item_names.NameCache.base_level()`; `ILVL_PLAUSIBILITY_MULTIPLE` is 3x as of 2026-07-25, see "Inference logic" below); otherwise falls back to a bonus-count summary. This plausibility check exists because the modifier isn't Blizzard-documented and produced nonsense for non-scaling items (e.g. a classic wand showed "ilvl 1112" against a real base level of ~35, caught 2026-07-23) — full raw `bonus_key` still included as `variant_raw` regardless. **The CLI (`snipe_check.py`/`print_snipes`) intentionally does NOT get this treatment** — it keeps printing the raw `bonus_key` string as-is; the smarter variant display, quality colors, icons, and plausibility check are dashboard-only, by design, not a gap to close. When `names=true`, rows also carry `icon`/`quality_color` from `item_names.NameCache`, and the response carries top-level `region`/`sell_realm_slug` for building an Undermine Exchange link (`https://undermine.exchange/#{region}-{sell_realm_slug}/{item_id}`, confirmed against a real example). `GET /api/realms` (added 2026-07-23) lists every realm with a `data/events/{cr}.parquet` file (i.e. actually collected), name-resolved, for the dashboard's sell-realm dropdown. `GET /api/status` surfaces `data/state/{cr}.json`'s collector `last_modified` + listings-sweep freshness, so a stalled scheduled task is visible in the UI, not silently hidden. `GET /` serves `static/dashboard.html`. **`GET /api/log/realms` and `GET /api/log?sell={cr}` (added 2026-07-23) are deliberately unauthenticated** — no `current_active_user`/`current_subscribed_user` dependency, unlike every other `/api/*` route — backing the public `static/log.html` page (human's explicit call: realm names and raw retrieval timestamps aren't sensitive, they're not the paid product's data). `/api/log` reads `data/snapshots/{cr}/*.parquet` filenames directly (each is the epoch second of that snapshot's `Last-Modified` header) rather than a separate log table — `fetch_snapshot.py`'s `If-Modified-Since` check means a file only gets written when Blizzard actually published something new, so the file list already *is* a complete, honest retrieval log with zero new logging infrastructure. Realm-list logic factored into `_realms_payload()`, shared with the existing (subscriber-gated) `/api/realms`; realm eligibility for the log uses a separate `_list_snapshotted_realms()` (checks `data/snapshots/{cr}/` directly) rather than `_list_collected_realms()` (checks `data/events/{cr}.parquet`, i.e. requires `diff_snapshots.py` to have run) — the log is about *retrieval*, not inference, so it shouldn't depend on the diff pipeline having run yet. **Background collection poll interval, tightened 2026-07-23 evening**: `COLLECTION_INTERVAL_SECONDS` (10 min) is now the *fallback* only — real production data from `/log` itself (7 consecutive Draenor retrievals all landing within ~1.5 min of each other, around :19-:20 past the hour) showed the original "no fixed clock time" assumption was overly cautious for at least this realm. `_next_poll_interval_seconds()` polls every `TIGHT_INTERVAL_SECONDS` (45s) when the wall-clock minute is inside `[TIGHT_WINDOW_START_MINUTE, TIGHT_WINDOW_END_MINUTE)` (12-28, a 16-min window deliberately wider than the ~1.5-min observed band since this schedule is shared across every deep-collected realm, not tuned per-realm — other realms likely publish at a slightly different offset), falling back to the normal 10-min cadence the rest of the hour so total request volume for a quiet 44 minutes/hour barely changes. `python dashboard.py --sell 1403` runs it on `127.0.0.1:8000`. **`GET /api/snipes` rows also carry `item_class`/`item_subclass`/`is_profession_item` when `names=true` (added 2026-07-24)** — see "Client-side snipe filtering" below for why and how the dashboard uses them. **Free tier, added 2026-07-25**: `/api/snipes`, `/api/realms`, `/api/status` switched from `current_subscribed_user` to `current_active_user` — login alone is the gate now. `SNIPE_TIER_CAPS`/`_snipe_cap(user)` clamp the effective `top` server-side regardless of what's requested: 250 (logged in, no subscription) / 2000 (active subscription) / 5000 (superuser, founder/admin headroom, not a real subscription tier) — see "Tiered batch caps" below. **`_enforce_realm_lock()`/`GET /pricing`, added 2026-07-25** — free-tier accounts locked to the first sell realm queried (`db.User.locked_sell_realm`); `/api/me` exposes it; `/pricing` serves a new public comparison page — see "Free-tier single-realm lock + /pricing page" below. **`api_snipes()` now runs `find_snipes()` via `await asyncio.to_thread(...)`, added 2026-07-25 same day** — `find_snipes()` can make blocking Blizzard API calls mid-query since the type-28 fix above; calling it directly in this `async def` route froze the entire single-process server on a cold cache (real outage, see CLAUDE.md's "Real production outage" section) — `to_thread` keeps every other request responsive during a slow first-time call |
| `static/dashboard.html` | **Column/filter additions, 2026-07-23**: a "Sell realm low" column (`sell_now_copper`, right after "Sell p25") shows the sell realm's current cheapest live listing next to the historical sold-price percentile, sortable like every other numeric column (nulls — no current listing — always sort last regardless of direction); a "Unique transmog only" checkbox sends `max_appearance_sources=1` (the backend flag shipped first without this control, then got wired in same-day once noticed missing); a "Max per item" input sends `max_per_item`. Both also show in the row tooltip. **Status ticker simplified twice, same day (2026-07-23)**: first the "Polled" segment was removed (it was just the browser's render-time wall clock, `new Date().toLocaleTimeString()`, re-stamped every 60s regardless of whether Blizzard had actually published anything new), then — per direct human feedback that even the reduced 3-segment version ("Snapshot"/"Listings swept"/"Ledger") was still overlapping/unclear — collapsed further to one segment, "Last auction data" (`s.last_modified`, the real Blizzard `Last-Modified` header), styled stale/ok exactly as "Snapshot" was. `fmtTs()` was deleted as dead code once nothing called it. `/api/status` itself still returns `listings_updated`/`events_exist` unchanged — only the frontend display was cut down, not the API shape or its tests. Single static file, vanilla JS, no build step/Node/npm. Sortable/filterable table over `/api/snipes` with WoW-flavored presentation: quality-colored item names, gold/silver/copper coin-icon money formatting, a custom mouse-hover tooltip (icon, colored name, prices — informational only) mimicking an in-game item tooltip. **The item icon itself (not the tooltip) is the Undermine Exchange link** — the tooltip repositions on every `mousemove` to follow the cursor, so a link inside it was unreachable; clicking the stable table-row icon opens the item's Undermine Exchange page in a new tab instead (fixed 2026-07-23). Freshness indicators, ~60s client-side polling auto-refresh. Min-discount filter is a real 0–100 percentage input (was a raw 0–1 fraction); names/icons resolution is always on (the toggle to disable it was removed — no real reason to ever turn it off). The `caveat`/NOTE banner is **intentionally not rendered** (human decision, 2026-07-23) — `/api/snipes` still returns `caveat` in its JSON, the frontend just ignores it now. Redesigned 2026-07-23 (see "UI design pass" below). **QoL pass, same day** (see "Dashboard QoL pass" in `PROGRESS.md` for full detail): sell realm is now a `<select>` populated from `/api/realms` instead of a free-typed id box; min/max buy-price (gold) filter inputs; duplicate item/variant snipes collapse into one row (best discount% on top) with a `▾ N` expand toggle, sorted best-first inside the group via `buildGroups()`; column sorting (`renderTable()`/`compareRows()`) is entirely client-side now on all seven columns with an asc/desc toggle, so clicking a header no longer re-fetches `/api/snipes` over the network — the earlier version's re-fetch-on-every-sort-click was the reported "lag". **Client-side filtering architecture + item-class filter, added 2026-07-24** — see "Client-side snipe filtering" below for the full design: `cachedRows` holds one loose, generously-sized batch (`fetchBatch()`, `BATCH_TOP`); every filter-rail threshold (`applyFilters()`) and the 8-checkbox item-class filter (`ITEM_CLASS_FILTERS`) re-filter it in the browser via `renderTable()`, with zero network round-trip. Only a sell-realm switch, an items-csv change, the 60s auto-refresh timer, or an explicit Refresh click still fetches. **`localStorage` batch cache + status-gated auto-refresh, added later the same day** — see "localStorage batch cache" below. **Tiered batch caps + free tier, added 2026-07-25**: `BATCH_TOP` raised 2000→5000 (the ceiling across every account tier — the server clamps down to the caller's real tier, see `dashboard.py`'s `SNIPE_TIER_CAPS`); `init()`'s subscribe-redirect gate removed entirely (any logged-in account reaches the dashboard now); the "Top" display-cap input/control removed — see "Tiered batch caps" below for both |
| `Dockerfile` / `.dockerignore` / `docker-entrypoint.sh` | Packages the web app (not the local collection pipeline) into a container; runs `alembic upgrade head` before serving. **Deployed** as of 2026-07-23 — see the Railway section below |
| `tests/` | pytest suite (`pytest -q`; root `conftest.py` makes top-level modules importable). `test_diff.py`: all five classifications + gap/relist edges. `test_fetch.py`: `bonus_key`/`rows` purity, backoff, malformed-JSON skip. `test_pipeline.py`: snapshots-on-disk → `diff_snapshots.main()` → analyze commands, incl. idempotent rebuild. `test_scan_region.py`: listing `rows()` purity, malformed-JSON skip, sweep survives a per-realm failure and honors `--exclude`. `test_blizz.py`: `list_connected_realms()` href parsing, `connected_realm_slugs()`/`connected_realm_realms()`. `test_snipe_check.py`: discount/liquidity/item filtering, sell-realm-self exclusion, `--items`/`--items-file` merge. `test_item_names.py`: `NameCache` name/icon/quality/`base_level` resolution and caching, incl. backfilling quality/level onto a cache file written before those fields existed. `test_dashboard.py`: `/api/snipes`/`/api/status`/`/api/config` against the same synthetic-pipeline fixtures via FastAPI's `TestClient` (real duckdb/pyarrow, no mocking; live Blizzard calls for realm info are stubbed), the ilvl-plausibility fallback (both the legitimate and bogus cases), and pure `_parse_variant()`/`_realm_info()` caching tests. **`isolate_item_names_cache` is an autouse fixture redirecting `item_names.CACHE_PATH` into `tmp_path`** — added after `names=true` tests were caught writing fake stub data into the real, gitignored `data/item_names.json` production cache (found and cleaned 2026-07-23); any new test touching `NameCache` inherits the isolation automatically, nothing to remember per-test |
| `diff_snapshots.py` | **Core IP.** Diffs consecutive sell-realm snapshots, classifies every vanished auction, writes events parquet. Relist matching (`relist_key()`) uses `market_key()`, not the raw `bonus_key` (2026-07-23) — see "Inference logic" below |
| `analyze.py` | DuckDB CLI: liquidity summary + per-item sold-price distribution / percentile check / per-auction trace. `connect()` also registers `MARKET_KEY_MACRO_SQL` (2026-07-23) as a DuckDB macro — the SQL-side mirror of `fetch_snapshot.market_key()`, used by `snipe_check.py`'s grouping/joins. **Redesigned into 3 helper macros (2026-07-25)** — `_ilvl28_value`/`_ilvl28_implausible`/`_strip_type`/`market_key(bk, base_level := NULL)` — to mirror the Python side's conditional type-28 pooling; `market_key()` now takes an optional second arg (DuckDB default-parameter syntax), so both 1-arg and 2-arg calls work. See "Inference logic" below |
| `appearance.py` | **Phase 3 groundwork, added 2026-07-23.** `AppearanceCache`: display/filter-only, never-raises lookups mapping item_id → transmog-appearance rarity, cached at `data/appearances.json`. Source is wago.tools' public `ItemModifiedAppearance` DB2 export (`GET https://wago.tools/db2/ItemModifiedAppearance/csv`, no build param needed — confirmed live, defaults to current build, ~126k items / ~48k distinct appearances as of 2026-07-23) — **not** a Blizzard API; there's no per-item "how many other items share this look" endpoint, so this is a bulk batch fetch (`python appearance.py --refresh`), not a lazy per-item cache like `item_names.NameCache`. Rarity proxy = `source_count`: how many distinct item ids grant the same `ItemAppearanceID` region-wide (1 = unique look, e.g. confirmed against item 19019/Thunderfury). This is deliberately a v1 proxy, not a real obtainability model — doesn't know if a source is still farmable, BoE vs quest-locked, etc.; manual curation on top is still open per the roadmap. **Not wired into the Railway background collection loop** (human/architecture decision, not an oversight) — wago.tools is a third-party site outside the Blizzard rate-limit budget the rest of the project is scoped around, so refreshing this cache is a deliberate manual/periodic step, not automatic |
| `item_names.py` | `NameCache`: display/filter-only, never-raises lookups backed by the static item/pet API, cached at `data/item_names.json`. `.get()` name, `.icon()` render-CDN icon URL (`/media/item\|pet/{id}`), `.quality_color()` hex color from the item's `quality.type` (or a positional guess for pet rarity, since Blizzard doesn't document `pet_quality_id`'s exact enum), `.base_level()` the item's own catalog level (used by `dashboard.py` to sanity-check the modifier-28 ilvl claim), **`.inventory_type()` (added 2026-07-23)** the equipment slot type (e.g. `"HEAD"`, `"PROFESSION_TOOL"` — confirmed live against real items, used by `snipe_check.py` to exclude profession gear from the unique-transmog filter) — all fail soft to a neutral fallback, never break a snipe_check/dashboard run. Internally, `_ensure_item_details()` fetches name+quality+level+inventory_type in one API call and backfills whichever cache dict a pre-existing entry is missing, so an old cache file (name-only) self-heals instead of silently returning `None` forever for newer fields. **`inventory_type` is cached even when `None`** (unlike quality/level, which are only cached when truthy) — most items genuinely have no inventory_type at all (reagents, consumables, quest items), so a truthy-only write would mean those items refetch on every single call forever instead of caching the real, permanent "not equippable" answer. **`.item_class()`/`.item_subclass()` (added 2026-07-24)** — Blizzard's official `item_class`/`item_subclass` ids (confirmed live via `GET /data/wow/item-class/index` and per-class `itemSubclasses`, 2026-07-24: 2=Weapon, 4=Armor, 1=Container, 19=Profession, 20=Housing, 17=Battle Pets, 12=Quest, 15=Miscellaneous with subclass 5=Mount), fetched/cached/backfilled the same way as `inventory_type` (same underlying API call, zero extra cost) — backs `dashboard.html`'s item-class filter, see "Client-side snipe filtering" below |
| `db.py` | **Hosted SaaS pivot, Stage 2.** Async SQLAlchemy setup for the *relational* data only (users/sessions/subscription state) — deliberately separate from the parquet+DuckDB AH data layer, which is unchanged. `User` model (FastAPI-Users base fields + `stripe_customer_id`/`stripe_subscription_id`/`subscription_status`/`subscription_current_period_end`, written only by Stage 3's webhook handler; `locked_sell_realm`, added 2026-07-25, written only by `dashboard.py`'s `_enforce_realm_lock` — see "Free-tier single-realm lock" below). `DATABASE_URL`-driven; tests override the session dependency with SQLite (`aiosqlite`), so the suite needs no real Postgres |
| `auth.py` | FastAPI-Users wiring: email/password register+login, **cookie-based** sessions (not bearer/JWT-in-header — matches the static-HTML-no-SPA frontend). `current_active_user` gates login-only routes; `current_subscribed_user` (builds on it) additionally requires `subscription_status == "active"`, raising **402** (not 401/403) so the frontend can tell "not logged in" apart from "logged in but not subscribed" and redirect to `/login` vs `/subscribe` correctly. **Not used by `dashboard.py`'s routes as of 2026-07-25** (free tier — see its entry below) — kept defined, still a legitimate reusable dependency for any future genuinely-subscriber-only route (e.g. Phase 4's Discord alerts), just not currently wired to anything. `COOKIE_SECURE` env toggle — `CookieTransport` defaults `cookie_secure=True`, which silently drops the cookie over local `http://`; `false` in dev `.env`, left unset (secure) in production since Railway serves HTTPS |
| `billing.py` | **Stage 3, done 2026-07-23 — deployed straight to Stripe live mode** (human decision, no test-mode verification pass first). `POST /billing/checkout` creates a Checkout Session for the single €4.99/mo price, `client_reference_id` ties it to the logged-in user, redirects to Stripe. `POST /billing/webhook` verifies the Stripe signature (never trust an unverified body) and handles exactly `checkout.session.completed`/`customer.subscription.updated`/`customer.subscription.deleted`, writing `subscription_status`/`stripe_customer_id`/`stripe_subscription_id`/`subscription_current_period_end` onto `db.User` — the only writer of those fields. **Real bug the test suite caught before shipping**: `event["data"]["object"]` from `stripe.Webhook.construct_event()` is a `StripeObject`, not a plain dict — supports `obj["key"]` but not `obj.get("key")`, which every handler used; would have thrown on the very first real webhook delivery. Fixed via `.to_dict()` (needed on the retrieved `Subscription` object too, same issue) |
| `alembic/`, `alembic.ini` | DB migrations (async template). Two migrations: creates the `user` table, then adds `locked_sell_realm` (2026-07-25, `d73d88b42ac7`, hand-written not autogenerated). `alembic/env.py` reads `DATABASE_URL` from the environment (same source of truth `db.py` uses) rather than a hardcoded `alembic.ini` URL |
| `docker-entrypoint.sh` | Container startup: `alembic upgrade head` then `exec python dashboard.py` — migrations run automatically on every deploy, so "database" is part of the same auto-deploy as "backend"/"web" (Stage 5's CD goal). Reads `PORT` (Railway-injected) and `DEFAULT_SELL` (UI prefill only) from env |
| `static/login.html`, `static/register.html`, `static/subscribe.html`, `static/profile.html` | Plain HTML/JS forms/pages hitting FastAPI-Users' `/auth/login` (form-urlencoded), `/auth/register` (JSON), and `billing.py`'s `/billing/checkout`/`/billing/portal` routes — same no-build-step convention as `dashboard.html`. `profile.html` shows subscription status (with an admin-access badge for superusers) and links to the Stripe customer portal for self-service cancel/manage. `subscribe.html` has a real explainer pitch (headline, 3-step "how it works", feature list, funding note) as of 2026-07-23, replacing the earlier bare feature list. **Rebuilt 2026-07-24 to the light "assay ledger" tokens** (same palette/dark-mode toggle as `dashboard.html`, see "Full visual rethink" below) — `login.html`/`register.html` are a minimal centered card with the seal/wordmark above and no nav; `profile.html` gained a full `.topbar` (Dashboard/Log links) replacing its old single back-link. JS/functional logic unchanged from the first pass. **`subscribe.html`'s feature-bullet list rewritten 2026-07-25** (see "Free-tier single-realm lock + /pricing page" below) — describes what a subscription actually changes now that a free tier exists (2,000 vs 250 results, any realm vs one locked) instead of baseline features the free tier also has; links to the new `/pricing` page instead of duplicating the comparison. All topbar-style pages (`dashboard.html`/`profile.html`/`log.html`) and the `login.html`/`register.html` switch line gained a `/pricing` nav link the same day |
| `static/log.html` | **Public "Auction house API log" page, added 2026-07-23 (human ask, explicitly public — see below).** Realm picker (from `GET /api/log/realms`) + a reverse-chronological list of every timestamp `GET /api/log?sell={cr}` reports the collector actually received *new* data for that realm — no auth, no account. **Rebuilt 2026-07-24** to the same light "assay ledger" tokens/dark-mode toggle as `dashboard.html`, with a full `.topbar` (Dashboard link) replacing the old back-link — see "Full visual rethink" below |
| `static/pricing.html` | **Added 2026-07-25.** Public, unauthenticated (`GET /pricing`, like `/log`) — a free-vs-subscriber comparison page plus an FAQ, same "assay ledger" token system/dark-mode toggle as every other page. See "Free-tier single-realm lock + /pricing page" below for why it exists as its own route rather than folded into `subscribe.html` (confirmed with the human via `AskUserQuestion` rather than assumed) |
| `requirements.txt` | `requests`, `pyarrow`, `duckdb`, `fastapi`, `uvicorn`, `httpx`, `fastapi-users[sqlalchemy]`, `sqlalchemy[asyncio]`, `asyncpg`, `aiosqlite` (tests only), `alembic`, `stripe`, `pytest-asyncio` (Python 3.10+) |
| `.env.example` | `BLIZZ_CLIENT_ID`, `BLIZZ_CLIENT_SECRET`, `BLIZZ_REGION=eu`, `STRIPE_PUBLISHABLE_KEY`/`STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET`/`STRIPE_PRICE_ID`/`STRIPE_PRODUCT_ID`, `SECRET`, `COOKIE_SECURE`, `DATABASE_URL` |

Verified: `pytest -q` green (110 tests as of the min_sales fix, 2026-07-23; all against SQLite/mocked externals — no real Postgres or Stripe account needed in CI). Live API confirmed working end to end:
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
flags stand in for now), retention, CI, `VALIDATION.md`. The per-item
transferability flag question is now **resolved, not open** (human decision,
2026-07-24): this tool only ever surfaces items that are actually listed on
the AH, and an AH listing is unconditionally unsoulbound — nothing outside
the AH is in scope for a snipe check to begin with, so there's no case where
a per-item Warbound/BoP/Unique-equipped flag could change a recommendation.
No flag will be built for this.

**Second process deviation (human decision, 2026-07-23):** `dashboard.py` was
pulled forward from Phase 5 (web dashboard) to now, ahead of Phase 3
(appearance/transferability layer) and Phase 4 (Deal Score) — same pattern as
the Phase 0→1 deviation above: deliberate, documented, not silent drift. This
is viable without Phase 3 existing first because the dashboard surfaces the
exact same `snipe_check.CAVEAT` the CLI already prints; it doesn't add or
remove any transferability guarantees.

**Third process deviation (human decision, 2026-07-23, same day): hosted
multi-tenant pivot.** What was "deployable, not deployed" a few hours earlier
in this same document became actually deployed the same day — and by the
end of that day, all 5 stages of the hosted pivot shipped: auth (Stage 2),
Stripe billing deployed straight to **live mode** rather than test mode
first (Stage 3), server-side collection scoped to FULL/HIGH-pop realms
rather than literally all ~100 (Stage 4), and CD with Railway's "Wait for
CI" verified actually gating deploys (Stage 5). See "Hosted deployment
(Railway)" below and `PROGRESS.md` for the live URL and full details. This
is a much faster, higher-risk-tolerance path than the roadmap originally
implied (e.g. going straight to live payments without a test-mode
verification pass) — deliberate, human-directed, not silent drift.

**Fourth process deviation (human decision, 2026-07-23, same day): Phase 3
groundwork started ahead of Phase 1's remaining hardening.** The roadmap says
"execute top to bottom" and Phase 1's config split (sell realms vs scan
realms) is still not done — but the human asked for transmog-appearance
filtering directly, so `appearance.py` (itemId → appearance rarity, backed by
wago.tools' DB2 export) and `snipe_check.py --max-appearance-sources` shipped
same-day, same pattern as the Phase 0→1 and Phase 1→5 deviations above:
deliberate, documented, not silent drift. Scope was also narrowed on the fly:
the human's initial ask conflated "BoE-only" with "unique transmog" — those
turned out to be the same request in the human's head, and the BoE half was
a non-issue anyway (already covered by the existing unsoulbound-listing
guarantee, see the 2026-07-23 correction earlier in this doc), so only the
appearance-rarity filter was built, not a redundant BoE filter.

### UI design pass (done 2026-07-23)

All five static pages (`dashboard.html`, `login.html`, `register.html`,
`subscribe.html`, `profile.html`) were run through the `frontend-design`
skill and rewritten with one shared token system — "Undermine cartel
trading-floor": `--bg #14170f`, `--panel #1b1f15`, `--panel-raised #23281a`,
`--border #3a4029`/`--border-bright #565f38`, `--text #eef0e6`/`--text-dim
#97a084`, `--toxic #a6e600` (primary action color), `--brass #c9963e`,
`--ember #ff7a45` (errors), `--gold #ffd651` (price emphasis). System font
stacks only (`-apple-system, "Segoe UI", Roboto, sans-serif`) — no external
fonts, preserving the "no build step, offline-capable" convention; data/price
columns use `ui-monospace`. Dashboard's signature element is a static
segmented status ticker (`.masthead`/`.ticker`) replacing the old plain
status div — deliberately not animated, respects `prefers-reduced-motion`.
Each page's existing JS/functional logic was preserved exactly; only
markup/CSS changed. Two QoL fixes landed in the same pass: min-discount is
now a real 0–100 percentage input (was a raw 0–1 fraction), and the
names/icons toggle was removed (always on now). Per explicit human
instruction, the repeated caveat/NOTE banner was also removed from the
dashboard UI entirely — see the `dashboard.html` table entry above.

**Full visual rethink — in progress, dashboard.html done, other 5 pages not
started yet (2026-07-23).** Human wants to move away from the "Undermine
cartel" identity entirely, toward a "professional enterprise" feel, across
all six pages (the five above plus `static/log.html`) — noted because the
old dark-bg/toxic-green pattern is itself one of the three generic "AI
dashboard" looks the `frontend-design` skill warns against, so this was
never just a palette swap. **First proposal (dark "assay office /
commodities exchange") was rejected** — human's "professional enterprise"
meant light/white, not another dark theme with a different accent.
Revised and built direction, "assay ledger / certificate on paper":
`--paper #f6f7f5` (cool near-white base — deliberately not warm cream, to
avoid the OTHER generic AI-dashboard cliché), `--card #ffffff`, `--hairline
#d8dbd4`/`--hairline-strong #c3c8bd`, `--ink #14161a`/`--text-dim #5b6259`,
`--bullion #a67c2e` (signature muted gold — literal tie to the product
being about gold prices, deliberately darker/less "coin-bright" than a
first pass so it holds contrast on white), `--verified #2f7d72` (deep
verdigris-teal, reserved for the Validation Seal and "fresh" states only).
System-font stacks kept (no webfont), `ui-monospace` for data, unchanged
from before. **Validation Seal** signature element: one small hairline
circular stamp (checkmark ring, `--verified`) in the top bar next to the
wordmark — not per-row (a certificate has one seal, not one per line item)
— paired with a small "Validated Data" label; encodes the product's actual
thesis, not decoration. Layout: a left filter rail (`.filters`) replacing
the old horizontal control bar, top bar keeps nav (`/log`, `/profile`,
log out) — collapses to a stacked column under 900px width (same
`flex-direction: column` pattern the mobile table-overflow fix already
used elsewhere on this page).

**Real accessibility bug caught and fixed during the build, not after**:
Blizzard's in-game item-quality colors (`item_names.QUALITY_COLORS`) were
designed for dark UI panels — measured contrast against the new
`--paper` background before shipping, and every one of them fails WCAG AA
as *text* color except Rare/Epic (Common/white is literally invisible,
Uncommon green is ~1.3:1). First fix attempt (a small colored dot next to
the name) was rejected by the human — rarity should read the way it does
in-game or in the name, not as a disconnected swatch. **Revised fix**: an
inset colored ring around the item icon itself (`.item-icon`/`.tt-icon`,
via a `--q` CSS custom property set inline per row), matching how WoW's own
UI frames item art by quality. This also happens to fully dodge the
contrast problem rather than needing a workaround for it: the ring renders
against the icon's own artwork, not the page background, so even a white/
Common ring stays visible (verified against a real render.worldofwarcraft.com
icon in the local preview below — the white ring shows as a visible inset
edge against the icon's darker art). Item names render in plain `--ink`
throughout. This is the one deliberate JS change alongside the CSS/markup
rewrite (`iconStyle()` in `buildRowHtml`/`showTooltip`) — otherwise the
"only markup/CSS changed" convention from the first design pass held.

**Verification**: no backend changed (`pytest -q` stayed green throughout,
145 passing), but this was still checked in a real browser before shipping
— a throwaway local preview (auth-gated `init()` stubbed with sample rows,
served via a local static file server, screenshotted via the
`claude-in-chrome` skill, never committed) confirmed the seal, dots, coin
icons, and gold accent all render correctly together before deploying.

**Rollout to the other 5 pages — done 2026-07-24.** `login.html`,
`register.html`, `subscribe.html`, `profile.html`, `log.html` all now run the
same light "assay ledger" tokens as `dashboard.html`, including the dark-mode
toggle (theme choice is `localStorage`-backed and shared across all six pages
via the same pre-paint `<head>` script — switching theme on one page carries
across navigation). `login.html`/`register.html` use a minimal centered
layout (brand/seal above the form card, theme toggle fixed top-right, no nav
— there's nothing to navigate to before authenticating). `subscribe.html`
keeps its pitch/steps/price-card structure, restyled; the price figure now
uses `--bullion` instead of a separate gold token, matching the "gold is the
one signature accent" thesis. `profile.html` and `log.html` gained a full
`.topbar` matching `dashboard.html`'s (brand/seal, theme toggle, nav links)
instead of their old single "&larr; Back to dashboard" text link. All five
pages' JS/functional logic was preserved exactly — same element ids, same
fetch calls, same redirects — only markup/CSS changed, continuing the
convention from the first redesign pass. Verified via the same throwaway
local-preview + `claude-in-chrome` screenshot technique (light and dark, all
five pages) before considering this done; no backend touched, `pytest -q`
stayed green (154 passing) throughout since none of these files are covered
by the test suite (structure isn't asserted on, only the API layer is).

**Dark mode toggle, added 2026-07-23 (same evening).** The light "assay
ledger" palette above is the default, but a `#theme-toggle` button in the
top bar switches to a same-thesis dark variant via `:root[data-theme="dark"]`
overrides (not a different identity — same tokens, same signature Seal,
just `--paper #14161a`/`--card #1b1e24`/`--bullion #d4af61`/`--verified
#3fa9a0` etc., closely related to the original dark proposal that got
revised to light as the *default*). A small blocking `<script>` in `<head>`
(before `<style>`) sets `data-theme` pre-paint from `localStorage` (falling
back to `matchMedia("prefers-color-scheme: dark")` on a first-ever visit) so
there's no flash of the wrong theme; the click handler at the end of body
just toggles the attribute and persists the choice.

**Rarity ring size/thickness increased same evening** — the first pass
(24px icon, 2px inset ring) was hard to see in practice; bumped to 28px/3px
(`.item-icon`) and 40px/3px (`#tooltip .tt-icon`) after direct feedback.

**Loading indicator, added 2026-07-23.** `setLoading()` disables `#refresh`,
swaps its label to "Loading…", and dims `#table-wrap` (`opacity: 0.5` +
`pointer-events: none`) for the duration of `loadSnipes()`'s fetch, wrapped
in `try/finally` so it always clears even on an error response — covers
both the manual Refresh click and the 60s auto-refresh timer, since both
go through the same function. A silent refresh (manual or automatic) with
no feedback was the reported problem.

**"Min sell realm low (g)" filter, added 2026-07-23** — sends
`min_sell_now`, see the `snipe_check.py`/`dashboard.py` entries above.

### Client-side snipe filtering + item-class filter (added 2026-07-24)

Human-reported problem: every filter-rail threshold (discount%, sales/day,
gold range, sell-now, max-per-item, unique-transmog) was a server-side
`/api/snipes` query param — changing one did nothing until the user clicked
Refresh (or waited up to 60s for auto-refresh), each time round-tripping to
DuckDB. Same underlying shape as the sort-lag bug fixed earlier in "Dashboard
QoL pass", just not yet generalized to the rest of the rail.

**Fix: fetch one loose, generously-sized batch, filter it entirely in the
browser.** `dashboard.html`'s `fetchBatch()` (renamed from `loadSnipes()`)
calls `/api/snipes` with `min_discount=0`, `min_per_day=0`, no
`min_gold`/`max_gold`/`min_sell_now`/`max_appearance_sources`/`max_per_item`,
and `top=BATCH_TOP` (2000) — loose enough to include effectively every
row the sell realm's own data-quality floors (`min_sales`, fixed server-side
at 2, not user-adjustable) allow, generous enough to comfortably cover this
project's current data volume with headroom, but still a real, deliberate
cap: without ANY limit, a mature region-wide listings×sales join could
return an unbounded number of rows (every listing that merely breaks even,
across every scanned realm), which would make the page slower to load, not
faster — the literal "no limit at all" version of this idea was rejected for
that reason before implementing. The result lands in `cachedRows`.

Every filter-rail threshold is now a pure client-side predicate in
`applyFilters(rows)`, called from `renderTable()` on every re-render — no
network call. "Max per item" changed meaning slightly: it used to be a SQL
`ROW_NUMBER()` window trimming the *candidate pool* server-side; now it trims
each `buildGroups()` group's row list to its best N (still highest-discount-
first) client-side, so an item's best listing is never hidden outright, only
the "expand to see the rest" list shrinks. "Top" changed meaning entirely:
it's no longer the SQL `LIMIT`, it's a pure post-filter/post-sort display cap
applied in `renderTable()`.

**Only four things still trigger a real fetch**: switching the sell realm,
changing "Item ids (csv)" (both change the SQL candidate pool, not just
which of the fetched rows are shown), the 60s auto-refresh timer, and an
explicit Refresh click. (Realm-switch previously had no listener at all and
silently did nothing until the next Refresh click — a pre-existing gap,
fixed as a necessary part of this change, not scope creep: the new model
needs it to make sense.)

**"Unique transmog only" needed one new backend field to stay client-side
with full parity to `snipe_check.py --max-appearance-sources`**:
`is_profession_item` (`dashboard.py`'s `_row_to_json()`), the exact same
`NON_TRANSMOG_INVENTORY_TYPES` check `find_snipes()` already used, just
exposed unconditionally per-row instead of only when that server-side filter
was active. Zero extra Blizzard API calls — `inventory_type` was already
being fetched as a side effect of the same `NameCache` lookup that resolves
name/icon/quality for `names=true`, just not previously surfaced in the
response.

**Item-class filter (weapons/armor/containers/profession/housing/battle
pets/quest items/mounts), the human's original ask that started this
session's work.** Backed by Blizzard's *official*, documented `item_class`/
`item_subclass` ids (confirmed live via `GET /data/wow/item-class/index`,
2026-07-24 — not a guess like the undocumented bonus modifiers elsewhere in
this project): `item_names.NameCache.item_class()`/`.item_subclass()` (new,
same one-API-call-per-item cost as every other field there), threaded
through `dashboard.py`'s `_row_to_json()` when `names=true`, filtered
client-side via `ITEM_CLASS_FILTERS` (8 checkboxes, OR'd together when more
than one is checked; none checked = no class filtering). Mounts needed both
`item_class===15` (Miscellaneous) and `item_subclass===5` since they're not
their own top-level class. Notably, Housing (20) and Profession (19) already
exist as real Blizzard item classes as of this check — no heuristic needed
for either, unlike the older `inventory_type`-based profession-tool
exclusion above (kept as-is for the unique-transmog toggle specifically,
since it predates this and is already correct/tested).

**Verified in a real browser before shipping**, same throwaway-preview +
`claude-in-chrome` technique as the earlier redesigns: a stubbed
`dashboard.html` copy with 8 sample rows spanning every item class,
confirming (a) toggling any filter re-renders instantly with no loading
flicker, (b) the Mounts checkbox isolates exactly the one mount row, and (c)
"Unique transmog only" correctly excludes a profession-tool row even though
its `appearance_sources` looked "unique" (1) — the exact case
`NON_TRANSMOG_INVENTORY_TYPES` exists to catch. No backend behavior changed
for existing callers (`snipe_check.py`'s CLI flags, `--max-appearance-
sources`, etc. are all untouched) — only `dashboard.html`'s own fetch
strategy and three new always-present response fields
(`item_class`/`item_subclass`/`is_profession_item`, `names=true` only).
`pytest -q` stayed green throughout (160 passing, new tests for
`NameCache.item_class()`/`.item_subclass()` and the two new response
fields).

### localStorage batch cache + status-gated refresh (added 2026-07-24)

Same day, immediately after the client-side filtering change above. Human's
follow-up: a page refresh (or revisit) still re-ran the full `/api/snipes`
fetch every time, showing a blank table for the round trip; the 60s
auto-refresh timer also blindly re-ran that same expensive DuckDB query
every tick regardless of whether Blizzard had actually published anything
new since the last check (~60 checks for every 1 real hourly update).

**Fix, two parts, both in `dashboard.html`:**
1. **Instant paint from `localStorage`.** `fetchBatch()`'s successful result
   is cached to `localStorage` (`cacheKey()`: `snipe_cache_v{CACHE_VERSION}_
   {sell}_{items}`, scoped per sell realm *and* per item-id restriction since
   they're different underlying datasets) alongside the sell realm's
   `last_modified` from the same fetch's `/api/status` call. `renderFromCache()`
   paints whatever's cached the instant the page loads (or a realm switches)
   — no more blank table during the network round trip. `CACHE_VERSION` exists
   so a future row-shape change can't get rendered against code that expects
   different fields; bump it if that ever happens.
2. **`checkForUpdates()` replaces `fetchBatch()` as what the 60s auto-refresh
   timer (and the initial page load) actually calls.** It first does a cheap
   `/api/status` check (a file-mtime lookup, no DuckDB join) and compares the
   live `last_modified` against the cached one — only calling the expensive
   `fetchBatch()` when they differ (or the cache is missing, or older than
   `MAX_CACHE_AGE_MS` = 2h, a safety net in case the status signal itself
   ever gets stuck). Realm switch, an items-csv change, and the manual
   Refresh button all still call `fetchBatch()` directly, bypassing this gate
   — those are explicit "give me fresh/different data now" actions, not
   passive polling.

**Known, accepted tradeoff**: gating on the *sell realm's* `last_modified`
alone can miss a genuinely new region-wide *buy-side* listing that appears
between sell-side updates (`collect_all.py` sweeps every scanned realm's
listings every cycle regardless of whether any one sell realm's own snapshot
changed that cycle — see its entry above). Worst case, a fresh cheap listing
elsewhere in the region sits unseen until the next real sell-side update
(same order of magnitude as the ~hourly staleness this product already
tolerates everywhere else); the manual Refresh button is always available
for anyone who wants the always-fresh guarantee immediately. Not treated as
a bug, just a documented, deliberate tradeoff for a large reduction in real
query volume.

**Cache cleared on logout** (`clearAllCaches()`) — `localStorage` is
per-browser, not per-account, so a different user logging in on a shared
machine must never get an instant paint of the previous account's cached
snipe data, even briefly.

**Verified with a real browser + a mocked `window.fetch`** (not a stubbed
`init()` this time — the actual `fetchBatch()`/`loadStatus()`/
`checkForUpdates()` functions ran unmodified against canned `/api/*`
responses, so the real code paths were exercised, not a facsimile):
confirmed a first load does exactly one real `/api/snipes` call and writes
the cache; a reload with an unchanged mocked `last_modified` paints
instantly from `localStorage` with **zero** `/api/snipes` calls (only the
cheap status check); and flipping the mocked `last_modified` and re-running
`checkForUpdates()` correctly triggers exactly one fresh `/api/snipes` call.
`pytest -q` stayed green throughout (160 passing, no backend touched by this
change).

**UI fix, same pass**: human feedback on a live screenshot — the "Item
class" section heading sat right on top of the "Weapons" checkbox with
negative spacing (`margin-bottom: -0.35rem`, a leftover from an earlier
layout attempt). Fixed with a top hairline divider + real positive spacing
(`.filters-heading`), giving the item-class group visual separation from
"Unique transmog only" above it. (A second piece of feedback in the same
message — "only one togglable at a time" — was explicitly retracted by the
human before being acted on; the OR-together multi-select behavior described
above is unchanged and correct as designed.)

### Table grouping now matches backend pricing (added 2026-07-25)

Caught from a real user screenshot: 8 separate top-level table rows for one
item ("Dawnforged Edge"), one per buy realm, all with byte-identical Sell
p25/Sell realm low numbers — proof the backend already priced them as one
market, but the table wasn't showing them as one. Root cause:
`snipe_check.find_snipes()` already matches/prices rows by `market_key`
(the coarser, pooled key from the 42/44/9 fixes above), but the SQL
explicitly excluded that column from the output (`SELECT * EXCLUDE
(item_rank, market_key)`), and `dashboard.html`'s `groupKey()` grouped by
the exact `variant_raw`/`bonus_key` instead. Two real listings on different
realms routinely share a `market_key` (and, provably, the same sell price)
without ever sharing an exact `bonus_key` (per-instance modifiers) — so
grouping by the exact string was splitting an already-identically-priced
market into separate rows.

**Fix**: stopped excluding `market_key` from `find_snipes()`'s output;
`dashboard.py`'s `_row_to_json()` now includes it unconditionally (not
gated behind `names=true`, unlike `item_class`/`item_subclass` — it's not a
display nicety, it's the field grouping structurally depends on);
`dashboard.html`'s `groupKey()` groups by `market_key` (falling back to
`variant_raw` only if it's ever missing). The "Variant" column and tooltip
are unaffected — they still show the precise per-listing bonus string,
only the grouping changed. Pet rows are unaffected by this change either
way (market_key is empty for pets, same as bonus_key was — the existing,
separate, not-addressed-here pet-species grouping caveat is unchanged, not
made worse).

**Verified in a real browser**: a mocked preview reproducing the exact
reported shape (one item, 3 realms, identical sell-side numbers, different
exact bonus strings) confirmed the 3 rows now collapse into one group with
a working expand toggle, sorted best-discount-first, no console errors.
`pytest -q`: 169 passing (new: `find_snipes()` row carries `market_key`,
`/api/snipes` includes it even without `names=true`).

### Tiered batch caps + free dashboard tier (added 2026-07-25)

Same day, immediately after the item 7761 investigation above (which is what
prompted "how many snipes do we even load in" — the answer at the time was a
flat `BATCH_TOP=2000` for every account alike). Human's follow-up: make the
row budget tier-based instead of one-size-fits-all, and — the bigger piece —
let a logged-in-but-unsubscribed account preview the dashboard at all instead
of the previous hard `/subscribe` wall with zero preview.

**This is a paywall change, not just a batch-size tweak**, and was treated
as one: confirmed explicitly with the human before implementing (see
CLAUDE.md's own guardrail on monetization decisions needing deliberate
sign-off, not inference from "maybe"). Confirmed design: 250 rows for any
logged-in account (the new free tier), 2000 for an active subscription, 5000
for superuser (founder/admin headroom, not a real subscription concept).

**Backend** (`dashboard.py`): `/api/snipes`, `/api/realms`, `/api/status`
all switched from `current_subscribed_user` to `current_active_user` —
login is now the only gate; `SNIPE_TIER_CAPS`/`_snipe_cap(user)` clamp the
effective `top` server-side to the caller's real tier regardless of what
`top` value was requested. `auth.current_subscribed_user` itself is kept
defined (not deleted) since it's still a legitimate dependency for any
future genuinely-subscriber-only route — just unused for now.

**Frontend** (`dashboard.html`): `init()`'s old
`if (!is_superuser && subscription_status !== "active") redirect to
/subscribe` is gone entirely — any logged-in user now reaches the real
dashboard. `BATCH_TOP` raised 2000→5000 (the ceiling across every tier); the
frontend always requests that same ceiling regardless of the caller's own
tier, since the server clamps it down to the real amount anyway — the
frontend genuinely doesn't need to know its own tier ahead of time. The
now-impossible `402` branch in `fetchBatch()` (redirect-to-`/subscribe` on a
snipes-fetch failure) was removed as dead code, not left in "just in case."

**"Top" display-cap control removed entirely**, human's own follow-up
observation once tiering made the real row budget visible server-side: with
`renderTable()` already sorting client-side, an artificial "only show the
top N" display truncation on top of that added a control with no real
purpose — the table now always renders everything that passes the current
filters. Removed the `#top` input, `LIVE_FILTER_IDS` entry, and the
`cappedGroups`/`Number($("top").value)` slicing logic in `renderTable()`.

**Tests**: `tests/test_auth.py`'s `test_dashboard_api_routes_require_auth`
(renamed `test_dashboard_api_routes_require_login`) updated for the new
reachability — unauthenticated still 401, but logged-in-unsubscribed now
reaches real business logic (400 for an uncollected realm) instead of the
old 402. `tests/test_dashboard.py` gained `test_snipe_cap_by_tier` (pure
unit coverage of `_snipe_cap()` for all three tiers, including "superuser
wins even with no subscription") and a parametrized
`test_api_snipes_clamps_top_to_tier_cap` (spies on `snipe_check.find_snipes`
to assert the real `top` value it receives is clamped regardless of what
was requested in the query string) covering free/subscribed/superuser. One
now-obsolete regression test (`test_index_html_redirect_check_respects_
superuser`, guarding client-side redirect logic that no longer exists) was
deleted, not left disabled. `pytest -q`: 167 passing.

**Verified in a real browser**: a mocked-fetch preview simulating a
logged-in, non-superuser, non-subscribed account (`subscription_status:
null`) confirmed it reaches the dashboard and renders real rows instead of
being redirected — the actual behavior change this session was about —
plus confirmed the "Top" field is gone from the DOM and no console errors
fired.

### Free-tier single-realm lock + `/pricing` page (added 2026-07-25)

Same day, human's follow-up on the free tier above: "to minimize requests,"
a free (non-subscribed) account should only ever be able to query one sell
realm, not switch freely between every collected realm the way a subscriber
can — plus a request to explain the tier difference somewhere, which grew
into a dedicated public pricing page (confirmed via `AskUserQuestion` rather
than assumed: a new `/pricing` route + page, not folded into `/subscribe`).

**Backend** (`db.py`/`dashboard.py`): `db.User` gained `locked_sell_realm`
(nullable `Integer`, new Alembic migration `d73d88b42ac7`, hand-written
rather than autogenerated against a live DB — a single-column add doesn't
need it). `dashboard._enforce_realm_lock(user, sell, session)` runs at the
top of `/api/snipes`: a no-op for any account `auth.has_active_subscription`
already treats as unrestricted (active subscription or superuser — reuses
that check rather than re-deriving the same logic a second time); for
everyone else, the *first* `sell` value ever queried gets written to
`locked_sell_realm` and committed; every subsequent request for a
*different* `sell` gets a **403** with a clear upgrade message. The lock
check runs before the "is this realm even collected" business-logic check,
so a locked-but-uncollected realm still surfaces as 400, not silently
swallowed by the lock. `/api/me` now also returns `locked_sell_realm` (null
for an unrestricted account, or one that hasn't queried anything yet) so the
frontend can pre-emptively lock the UI instead of letting a free-tier user
discover the restriction via a failed request.

**A real subtlety caught while writing this**: `current_active_user`'s
returned `user` object and a directly-injected `session:
AsyncSession = Depends(get_async_session)` parameter share the *same*
underlying SQLAlchemy session within one request (FastAPI caches dependency
results per request by callable identity) — so mutating `user.
locked_sell_realm` and calling `await session.commit()` persists correctly
without an explicit `session.add()` first, the same pattern `billing.py`'s
webhook handler already relies on.

**Frontend** (`dashboard.html`): `init()` now reads `meData.
locked_sell_realm` and, for a free-tier account, calls
`applyFreeTierRealmLock()` — disables the `<select id="sell">`, forces its
value to the locked realm (overriding the server's generic `/api/config`
default, which loses to a real per-user lock), and injects the locked realm
as a `<select>` option even if it's missing from the fetched `/api/realms`
list (can happen if the lock was set against a realm with no visible
collected data yet — the UI must reflect what's *actually* locked
server-side, never silently show a different realm as selected). A short
`#realm-lock-hint` note explains the restriction either way (already locked,
or "your first choice will be locked in") and links to `/pricing`.

**`/pricing` page**: new `static/pricing.html` + `GET /pricing` in
`dashboard.py`, deliberately public/unauthenticated like `/log` — a pricing
page a visitor can't see before registering defeats its own purpose. Side-
by-side Free (€0, 250 results, one locked realm) vs Subscriber (€4.99/mo,
2,000 results, any realm) comparison plus an FAQ explaining *why* the lock
exists (bounding real per-query compute cost, not an arbitrary paywall) and
that both tiers read identical, equally-fresh validated data — only row
count and realm choice differ. Does **not** advertise the internal
superuser/5000 cap — that's founder/admin headroom, not a purchasable tier,
same reasoning `SNIPE_TIER_CAPS`'s own comment already gives. Linked from
the topbar nav on `dashboard.html`/`profile.html`/`log.html` and from
`login.html`/`register.html`'s switch line.

**`subscribe.html` copy rewritten**, per direct human instruction: the old
feature-bullet list (auto-refreshing dashboard, sold-price percentiles, pick
any realm, filter by budget) described things the free tier now also has —
replaced with what a subscription *actually* changes (2,000 vs 250 results,
any realm vs one locked) plus the funding-the-hosting narrative, and a link
to `/pricing` for the full comparison instead of duplicating it. The 3-step
"how it works" explainer above the price card is unchanged — that's the
product thesis, not the tier pitch, and wasn't what was flagged.

**Tests**: `tests/test_auth.py` gained three real-DB-persistence tests
(`test_free_tier_locks_to_first_sell_realm`,
`test_active_subscription_is_never_realm_locked`,
`test_superuser_is_never_realm_locked`) — deliberately in this file, not
`test_dashboard.py`, since its dependency-override pattern bypasses the real
session-backed `user` object the lock's persistence depends on (confirmed by
checking: the override tests couldn't tell a real DB write from a no-op).
`test_dashboard.py` gained `test_pricing_page_served_without_auth`.
`pytest -q`: 173 passing.

**Verified in a real browser**: a mocked preview with a free-tier account
already locked to Draenor, where `/api/config`'s own default was a
*different* realm (Silvermoon) and `/api/realms` listed both — confirmed the
dropdown still locks to Draenor specifically (not just "whatever's first"),
is genuinely `disabled`, and the hint text renders correctly, no console
errors.

**CI went red after pushing this, fixed same-session (`aef383c`)**: adding
`session: AsyncSession = Depends(get_async_session)` to `/api/snipes` means
FastAPI resolves that dependency on *every* request to the route,
regardless of whether `_enforce_realm_lock` ever reaches its write branch.
`test_dashboard.py` didn't override `get_async_session` the way
`test_auth.py` does, so it fell through to the real one, which needs
`DATABASE_URL` — unset in CI, 17 tests failed on dependency resolution
alone. Passed locally purely by accident (`.env` has `DATABASE_URL` set,
pointing at a stopped local Postgres container — SQLAlchemy engines are
lazy, so nothing tried to actually connect since none of those tests write).
Fixed with the same throwaway-per-test-SQLite override pattern
`test_auth.py` already uses; this time verified by re-running the full
suite with `DATABASE_URL` explicitly unset (`env -u DATABASE_URL pytest`)
*before* pushing, matching CI exactly rather than trusting a local pass.
**Lesson for next time a route gains a new dependency**: check whether
every test file hitting that route overrides it, don't assume a local green
run means CI will agree — a locally-configured `.env` can mask exactly this
class of gap.

**Real bug caught live after deploy, fixed same-day**: the human reported a
free-tier account got locked to a sell realm it never actually chose.
Traced to `init()`: it pre-selected and immediately auto-queried the
server's site-wide default realm (`/api/config`'s `default_sell`) before
the human touched anything — for a free-tier account with no lock yet, that
silent auto-fetch is exactly what `_enforce_realm_lock()` used to set the
lock, so "your choice" was actually whichever realm the server happens to
default to, the same for every visitor. **Not an old-vs-new-account issue**
— this would hit any free-tier account, new or old, on its first-ever
dashboard load, since `locked_sell_realm` starts NULL for everyone
regardless of when the account was created. Fixed by giving
`populateRealmPicker()` a `requirePick` mode (a leading blank "Choose a
sell realm..." placeholder, `defaultSell` ignored entirely) used only when
free-tier *and* unlocked — `init()` computes `freeTierUnlocked` and passes
it through. With the select genuinely empty, `checkForUpdates()`/
`fetchBatch()`'s existing `if (!sell) return` guards mean nothing fetches
until the human makes an explicit selection, which is the only thing that
should ever become a lock. Subscribed/superuser accounts and already-locked
free-tier accounts are unaffected -- the placeholder only appears for the
one case that was actually broken. Verified live with a mocked preview:
confirmed zero `/api/snipes` calls on load (previously would have been 1,
silently locking to the server's default) and exactly one call after
simulating an explicit dropdown selection, locking to *that* realm instead.

### Copy pass + contact info (added 2026-07-25)

Same day, human follow-up on `pricing.html`/`subscribe.html` copy: "Up to
250/2,000 validated snipes per query" reworded to "250/2,000 snipes in
total, refreshes every hour when new AH data comes" (more accurate — the
cap is a total budget per batch, not a per-request thing, and ties the
number to the real refresh cadence rather than reading like an arbitrary
limit); removed "No card required" (Free) and "Everything in Free"/
"Everything the free tier already gets, uncapped" (Subscriber) as
redundant given the rest of the copy already says this; removed the em-dash
from the funding sentence, split into two plain sentences instead. Applied
consistently to both `pricing.html` and `subscribe.html`.

`static/log.html`'s lede paragraph shortened from a 3-sentence explainer to
one line ("Log for when the collector has gotten new AH data from
Blizzard.") per direct human instruction — the longer version explaining
the `If-Modified-Since`/no-op-poll mechanics was judged unnecessary for a
page whose UI already makes the point self-evident.

**Contact info added** — `rasmus2001@gmail.com` / Discord `rasmus5533`, for
site or payment issues — to `subscribe.html` (near the checkout button,
where payment issues are most relevant), `profile.html` (new `.contact`
block below the account card), and `pricing.html` (below the FAQ). Not
added to every page — deliberately scoped to the pages that actually touch
money/account state, not `dashboard.html`/`log.html`/`login.html`/
`register.html`. On `profile.html` specifically, the block had to be moved
*outside* `.shell` (a `display:flex` container with no explicit
`flex-direction`, i.e. row by default) rather than added as a second child
inside it, which would have placed it beside the profile card instead of
below it — caught before shipping by checking the actual CSS rather than
assuming block-level stacking.

### Second ilvl-plausibility bug: absolute ceiling added (2026-07-25)

Human reported a wrong-looking ilvl on a real snipe: item 237468
(Nightfall Executioner's Girdle) showed "ilvl 3031". Traced live: base
catalog level is 610, and 3031 sits *inside* the existing
`ILVL_PLAUSIBILITY_MULTIPLE` (5x → 3050, as it stood that morning — since
lowered to 3x, see "Inference logic" below) — the exact ratio-based guard
added 2026-07-23 for a different case (a classic item claiming "ilvl 1112"
against a base of ~34) didn't catch this one, since the ratio here (~5x)
just barely stayed under the threshold. Checking every live listing for
this item found *only two* modifier-28 values ever appear (3031, 2462),
never anything close to the real ~610 base — strong evidence type 28 isn't
encoding item level at all for this item's itemization, not just a loosely
-scaled approximation of it.

**Fix**: `dashboard.py` gained `ILVL_ABSOLUTE_MAX = 1000`, ANDed with the
existing ratio check in `_variant_label()` — both must pass for a claimed
ilvl to display. Real WoW item levels have never approached four digits
across the game's history, so 1000 is deliberately generous headroom for
future content, not a tightly-fitted bound; it exists specifically to catch
implausible claims on *high*-base-level items, where the ratio check alone
isn't tight enough (the original ratio check remains the guard for *low*-
base-level items, where even a moderate absolute value can be nonsense —
neither check alone covers both failure modes). New regression test
mirroring the existing ilvl-1112 test, using this item's real numbers.
`pytest -q`: 174 passing.

### Hosted deployment (Railway)

**Live at `https://wow-project-production.up.railway.app`.** Project
`valiant-peace` on Railway, two services:
- `wow-project` — the FastAPI app, built from the repo's `Dockerfile` (not an
  auto-detected builder). A persistent Volume is mounted at `/app/data` —
  this is where collected AH data actually lives, entirely separate from
  Postgres's own storage.
- `Postgres` — managed database, holds only the relational `user` table
  (auth/subscription state). Its default volume is 5GB, but that's unrelated
  to AH-data storage, which lives on `wow-project`'s own Volume, not here.
  `DATABASE_URL` on `wow-project` references it via Railway's private
  internal networking (`postgres.railway.internal`), with the
  `postgresql+asyncpg://` scheme our async SQLAlchemy setup needs (Railway's
  own default `DATABASE_URL` uses plain `postgresql://` — had to be
  reconstructed, not used as-is).

`docker-entrypoint.sh` runs `alembic upgrade head` before starting `uvicorn`,
so every deploy migrates the database automatically (Stage 5's CD goal:
backend/database/web all update together on one push to `main`). Railway's
**"Wait for CI"** setting (Service → Settings → Deploy, CLI doesn't expose
it — flipped on directly in the dashboard by the human, 2026-07-23) is
enabled and **confirmed working end to end**: a push now sits in `WAITING`
until `.github/workflows/ci.yml`'s check passes, then proceeds `BUILDING` →
`DEPLOYING` → `SUCCESS` automatically — a red CI run genuinely blocks the
deploy now, it doesn't just run in parallel with it.

**`ENABLE_BACKGROUND_COLLECTION=true`** is set on Railway only (default false
everywhere else) — this is what makes `wow-project` run `collect_all.py` on
its ~10-minute in-process loop, replacing what local Task Scheduler used to
be the only source of. See "Scheduled automation" below for how the two
relate.

**Stripe is wired in live mode** (`STRIPE_SECRET_KEY`/`STRIPE_PUBLISHABLE_KEY`/
`STRIPE_PRICE_ID`/`STRIPE_PRODUCT_ID`/`STRIPE_WEBHOOK_SECRET` all set on
Railway to live-mode values, human decision — see the Stripe process
deviation note above). Webhook endpoint registered in Stripe's live-mode
dashboard, destination id `we_1TwOESE53HGCO43pWiDY48Gn`, listening to
exactly `checkout.session.completed`/`customer.subscription.updated`/
`customer.subscription.deleted` at `/billing/webhook`.

**CLI tooling note**: the Railway CLI's Windows binary is blocked by this
machine's Smart App Control (a real Windows 11 feature — Microsoft states it
cannot be turned back on without a full reinstall once disabled, so this
was deliberately *not* the fix). Workaround: run the CLI inside a Docker
container instead (`node:20-slim` image, `npm install -g @railway/cli`) —
a Linux binary running under Docker/WSL2 never touches that Windows-native
policy at all. Useful if Railway CLI access is needed again later.

**Remote debugging note (2026-07-23)**: `railway ssh` gives an actual shell
on the live `wow-project` container (not `railway run`, which executes
*locally* with Railway env vars injected — useful for hitting the remote
Postgres via `DATABASE_PUBLIC_URL`, but it does **not** reach the remote
Volume or let you run the app's own scripts against live data). `railway
ssh` needs an SSH keypair registered with Railway first: `apt-get install
openssh-client` in the `node:20-slim` helper container, `ssh-keygen -t
ed25519`, `railway ssh keys add -k <path>.pub`, and `StrictHostKeyChecking
accept-new` in `~/.ssh/config` (the container has no persistent known_hosts
across rebuilds). Once connected, ordinary project scripts work directly
against production data, e.g. `railway ssh -- "cd /app && python analyze.py
--cr-id 1403 trace 15138"` — this is how the `min_sales` bug below was
actually confirmed against live data rather than guessed at.

### Scheduled automation

**Local collection is fully retired (human decision, 2026-07-23)**:
`run_cycle.py`, `run_cycle_task.ps1`, and the Windows Task Scheduler task
**AHSnipePipeline** (created 2026-07-20) are all deleted/unregistered, not
just paused. `collect_all.py` running in-process inside the Railway
deployment (every ~10 minutes, see its file entry above) is the *only*
collection path now — this product is explicitly
not meant to be run locally as a going concern; local work is for changing
code, then letting CI/Railway deploy it (see README's "The deploy flow").
The local dev Postgres container (`wow-project-pg`) was stopped (not
removed) for local Stage 3+ development/testing only.

The original reasoning for rejecting a **cloud** scheduled agent (a fresh,
stateless Claude routine checkout with no access to local `.env` or
accumulated `data/` history) still holds for *that specific approach* — but
it doesn't apply to the Railway deployment, which has its own persistent
Volume and its own `.env`-equivalent (Railway env vars) and therefore *can*
accumulate history across restarts. That's the loophole that made moving
collection off the human's machine viable at all.

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
3. Identical `(item_id, market_key(bonus_key), buyout, quantity)` appears among
   *brand-new* auction ids in N+1 → `likely_relisted` (consumed from a Counter,
   so two identical listings need two relists).
4. `gap_seconds >= MIN_REMAINING[time_left]` → `ambiguous` (could have expired;
   also absorbs collector-downtime gaps).
5. Else → `inferred_sale`.

`bonus_key` canonicalizes `bonus_lists` + `modifiers` so identical gear variants
compare equal. **Known blind spot:** a cancel *without* relist is
indistinguishable from a sale; the README verification protocol measures that
noise floor empirically. Ideas to shrink it later (seller-behavior priors,
multi-interval relist windows) belong after Phase 0, not during.

**`market_key()`, added 2026-07-23** (see `fetch_snapshot.py`): a coarser
version of `bonus_key` used everywhere matching/grouping happens (relist
detection here, plus sold-price percentiles/current-lowest-cap/buy-sell join
in `snipe_check.py`), stripping `MARKET_IGNORE_MODIFIER_TYPES = {9, 42, 44}`
-- undocumented-by-Blizzard modifier types found, from real production data,
to be a per-craft stat roll (42, varies continuously) and a per-instance
craft serial (44, increments sequentially: 245822, 245823, 245824...).
Neither is meaningful for price comparison, but both make `bonus_key` itself
near-unique per crafted item, fragmenting what's really one liquid market
into dozens of 1-2-sale buckets that the existing `min_sales`/current-lowest-
cap guards couldn't see across. The raw `bonus_key` is still stored/displayed
everywhere as-is; only matching uses the coarser key. Two independent
implementations (Python in `fetch_snapshot.py`, a SQL macro in
`analyze.MARKET_KEY_MACRO_SQL` for DuckDB-side grouping -- no shared UDF,
since this duckdb version's Python UDF support needs numpy, an otherwise-
unneeded dependency) kept honest by `tests/test_market_key.py`'s parity
check.

**Type 9 added 2026-07-24, a non-crafted item this time**: item 7761
(Steelclaw Reaver, a level-21 Rare weapon, so type 42's "crafted stat roll"
explanation didn't even apply) had nine distinct `m:9=NN` values region-wide.
Traced live: Draenor's own sell-side history for this item was **100% troll/
camped-relist listings** (~398,605g repeated across different auction ids,
same price regardless of the `9=` value -- itself evidence 9 isn't a real
stat, since a troll price wouldn't happen to match across genuinely different
variants), while a real, human-confirmed-legitimate listing existed
region-wide at 28,500g under a *different* `9=` value than what was on
Draenor, so it never joined against Draenor's sell-side data at all and was
invisible to `snipe_check` entirely. A human confirmed type 9 doesn't affect
the item's transmog appearance (i.e. not a real distinguishing variant)
before it was added to the ignore set -- unlike 42/44, this one isn't
self-evidently inference from the data alone, so it went through a "does
this actually matter" check first rather than being assumed. Both DuckDB
regex passes and the Python implementation updated together per the existing
convention; `tests/test_market_key.py` got new vectors using item 7761's
real bonus strings.

**Known, still-open limitation exposed by the same investigation**: even
after pooling, if a sell realm's *entire* observed history for an item/
market_key is troll/camped listings (as Draenor's was for 7761), none of the
existing guards (`min_sales`, the current-lowest-listing cap) can rescue the
estimate -- there's no legitimate sell-realm data to fall back to. A
region-wide cross-check against `data/listings/*.parquet` (which the buy
side already scans) was proposed as the principled fix but not built this
session -- parked, not forgotten, same status as the camped-relist window
bug below.

**This blind spot hit production, 2026-07-23**: item 15138 (Onyxia Scale
Cloak) on Draenor had exactly one `inferred_sale` ever recorded, for a
troll-priced decoy listing at 99,624g (the item actually trades around
444g) that was almost certainly cancelled, not bought — traced live via
`analyze.py --cr-id 1403 trace 15138`. With a young sell realm's thin
history, `snipe_check.find_snipes()`'s `min_per_day` filter alone wasn't
enough of a guard: `count(*) / days` can round up fast when `days` itself is
small, letting a single unverified sample look "liquid." Fixed with a new
`min_sales` floor (default 2, in addition to `min_per_day`) in
`snipe_check.find_snipes()` — see its file entry below. Doesn't eliminate
the underlying blind spot (two troll listings could still both be bogus),
just stops one sample from being trusted outright.

**A second, structural fragmentation problem hit production the same day**:
item 238014 (Sun-Blessed Sickle, a crafted item) on Draenor had 25+ distinct
exact `bonus_key`s live simultaneously, most clustered around 1000-1300g, but
`snipe_check.find_snipes()`'s exact-`bonus_key` grouping meant any one
listing's sold-price percentile came from whichever 1-2-sale bucket its exact
crafted roll happened to fall into -- one bucket's only sale was the camped
4999.99g relist described above, inflating that bucket's estimate to ~5x the
item's real market price. Traced live via a custom `analyze.connect()` query
grouping sales/current-listings by raw `bonus_key` (not the CLI's built-in
commands, which don't break down by variant) -- see the `market_key()` fix
above, which pools these buckets by market instead of matching them exactly.

**Type 28 conditional pooling, added 2026-07-25 (item 164353, Plundered
Scalebane Claymore)**: human found a cheaper Auchindoun listing for this
item (8214g) that wasn't surfacing as a snipe against Argent Dawn despite
looking like an obvious discount, and asked whether it was in the data at
all or being cut off by a limit. Traced live: the listing *was* present, but
`market_key()` (as it stood that morning, unconditionally stripping only
9/42/44) still matched on the raw type-28 value, and this item's live
listings carried five different type-28 values region-wide (186, 189, 645,
670, 289) against a real catalog base level of 60 — none of them a genuine
ilvl claim, all of it junk the same way item 237468's did, just for an old
Rare weapon instead of a modern raid item. Unlike 9/42/44, type 28 can't be
*unconditionally* added to `MARKET_IGNORE_MODIFIER_TYPES`: for current-
content ilvl-scaling gear, two copies of the same item at genuinely
different item levels are genuinely different markets (see item 237468's
own girdle example inside "the legitimate case" below) — human's framing
was that ilvl is *transmog-irrelevant noise* specifically for BoE gear
that's old enough to be a transmog target, not a rule that holds for every
item ever. `market_key()` needed to tell the two cases apart per item, not
apply one global rule.

**Fix**: `market_key(bk, base_level=None)` gained an optional second
argument. When supplied, it re-checks any type-28 value against
`ilvl_plausible(claimed, base_level)` (the same ratio-and-absolute-ceiling
check `dashboard.py`'s display logic already used) and, only if that check
fails, adds 28 to the per-call ignore set before stripping — a *plausible*
type-28 value is never touched, keeping genuinely different ilvl variants
distinct. Callers with no `base_level` handy (`diff_snapshots.relist_key()`)
get the unchanged pre-2026-07-25 behavior — unknown always means "don't
strip," never "assume it's junk," so a wrong guess can't silently merge two
items that really do have different, price-relevant item levels.
`analyze.MARKET_KEY_MACRO_SQL` was rebuilt into three small macros mirroring
this exactly (`_ilvl28_value`/`_ilvl28_implausible`/`_strip_type`), with
`market_key(bk, base_level := NULL)` using DuckDB's default-parameter syntax
so both call shapes work.

Since `find_snipes()`'s SQL can't make a Blizzard API call mid-query, a new
`_populate_base_levels()` (see `snipe_check.py`'s file entry above) runs
first each call, gathering every candidate item and populating a temp
`item_base_levels` table that `sell_now`/`sell_stats`/`buy` all join
against — the one place this pipeline now makes a network call where it
never did before (mitigated by `NameCache`'s existing caching, so it's a
one-time cost per never-before-seen item, not a per-call cost).

**The multiplier itself needed tightening, not just the conditional logic**:
testing the fix against item 164353's real values found the existing 5x
ratio (`60 * 5 = 300`) still didn't strip 186, 189, or 289 — only 645 and
670 exceeded it — so the bug would have only partially resolved. Compared
2x/3x/4x/5x against every known real case (164353's five values, 237468's
two, and the one confirmed-legitimate case, base 600 claiming 636): both 2x
and 3x correctly stripped every known junk value in both cases while
leaving the legitimate case untouched; 4x and 5x both left at least one of
164353's junk values unstripped. Chose **3x** (`ILVL_PLAUSIBILITY_MULTIPLE`
in `fetch_snapshot.py`, and the hardcoded `base_level * 3` inside
`analyze.py`'s `_ilvl28_implausible` macro — a separate literal, not a
shared constant, so it has to be kept in sync by hand, same as the rest of
this SQL mirror) over 2x as the more conservative choice, leaving a bit more
headroom for a hypothetical legitimate item scaling close to 2x-3x its base
before ruling it implausible. `tests/test_market_key.py` encodes all of
this: `test_implausible_type_28_pools_across_different_values` (164353's
full real vector, must all pool to one key), the renamed
`test_implausible_type_28_pools_via_absolute_ceiling` (237468), and
`test_plausible_type_28_is_never_stripped`/
`test_unknown_base_level_never_strips_type_28` guarding the two ways this
could regress. `pytest -q`: 225 passing.

### Real production outage caused by the type-28 fix (2026-07-25, same day)

Human reported the live site completely unresponsive (every request timing
out, 5+ minutes). Confirmed independently: `curl` against the public URL
timed out, and — ruling out a network/routing issue — a direct request from
*inside* the container to `127.0.0.1:8080` (the app's own listening port,
per `PORT`) also timed out. `railway status` still reported the service
`Online` the whole time; Railway's own health signal doesn't detect an
application-level hang, only whether the process is alive.

**Root cause**: the type-28 fix above added `_populate_base_levels()`,
which makes a real (if `NameCache`-cached) Blizzard API call per
never-before-seen item carrying a type-28 modifier — gathered unconditionally
across every sell realm's sales/snapshots *and* the entire region's buy-side
listings, by design (see its docstring). `dashboard.py`'s `api_snipes()` is
an `async def` route, but was calling `snipe_check.find_snipes()` (and
therefore `_populate_base_levels()`) directly on the event loop thread, not
offloaded anywhere. On the first real request after deploying, against a
cold cache, that meant potentially hundreds of sequential blocking HTTP
calls -- and since this is a single-process server, *nothing else* could be
served for the entire duration, including `/api/status`. This is a
completely different failure mode from the disk-usage risk below; it's an
availability bug in this session's own fix, caught live within the same
session rather than by a later report.

**Fix**: `api_snipes()` now runs the whole query (`analyze.connect()` +
`find_snipes()`) via `await asyncio.to_thread(...)`, so a slow first-time
call no longer blocks any other request. `_populate_base_levels()` also now
saves the `NameCache` incrementally (every 50 items) instead of only at the
very end -- an interrupted run (container replaced mid-populate, which is
exactly what happened here when the fix itself was deployed) no longer
loses all its progress; subsequent calls converge instead of restarting
from zero every time.

**Verified live**: after deploying the fix, `/` returned HTTP 200 in
~0.25-0.3s consistently, including while a deliberately-triggered slow
`snipe_check.py` run (cold cache, region-wide gather) was still in flight
via `railway ssh` in parallel -- confirming the rest of the app stays
responsive during exactly the condition that caused the outage. `pytest -q`:
225 passing throughout (this was an availability bug under real load, not a
logic bug the existing test suite would catch -- there is no test coverage
for "does this route block the event loop," which is a real gap; worth a
regression test using a slow stub in `_populate_base_levels()` if this class
of bug recurs).

**Lesson for next time a route gains a synchronous, possibly-slow
dependency** (a new network call, a large one-time computation): ask
whether it can block *other* requests, not just whether it's correct or
whether it's individually fast on a warm cache. An `async def` FastAPI route
does not protect you from this by itself -- it only helps if the actual
blocking work is offloaded (`asyncio.to_thread`, a background job, etc.).

### Per-request latency fix: parallelized `_populate_base_levels()` (2026-07-25)

`asyncio.to_thread` (above) fixed *server-wide* availability during a slow
`_populate_base_levels()` run, but not the *individual* request's own
latency -- it was still resolving every not-yet-cached item's base level
with one sequential Blizzard API call at a time. Human reported the live
dashboard stuck on "Loading…" with the table greyed out for 5+ minutes
while logged in as superuser. Traced live via `railway logs --http --path
/api/snipes`: three `/api/snipes` requests in that window, abandoned by the
browser (HTTP 499) after 49s, 31s, and 175s. Root cause: a superuser's
`top=5000` request has no `--items` filter, so `_populate_base_levels()`'s
candidate set is drawn from the *entire* region-wide `listings` table (36
realms) -- a large, often-partially-uncached set of items carrying a
type-28 modifier, each needing its own real HTTP round-trip. Confirmed via
`railway ssh` that `data/item_names.json` was genuinely still filling in
(1818 items cached, file mtime seconds old) -- not a stuck process, just a
slow one. **Amplifier**: `dashboard.html`'s `checkForUpdates()` (60s timer +
page load) has no dedup -- each tick fires a fresh `fetchBatch()` regardless
of whether a prior one is still pending, so overlapping expensive queries
piled up during the same window rather than one finishing before the next
started.

**Fix, two parts**:
1. `item_names.NameCache.ensure_many(item_ids, max_workers=16)` (new) --
   resolves every not-yet-cached id concurrently via a
   `ThreadPoolExecutor` instead of one item at a time, saving incrementally
   every 50 completions (same crash-resilience reasoning as the old
   per-item loop). Blizzard's rate limit (100 req/s, 36,000/h) has enormous
   headroom over this project's steady-state usage, so a burst of 16
   concurrent calls is safe. `_ensure_item_details()`'s merge logic was
   factored out into `_is_complete()`/`_merge_details()` so both the
   single-item and batch paths share it.
   `snipe_check._populate_base_levels()` now calls `ensure_many()` once up
   front instead of looping `base_level()` per item -- every `base_level()`
   call in the row-building loop after that is a cache hit.
2. `dashboard.html` gained a `fetchInFlight` guard: `fetchBatch()` is a
   no-op if an equivalent fetch is already running, instead of piling a
   second slow query on top of the first. Not a queue or an abort -- since
   every caller re-fetches the same loose batch, a skip is sufficient; the
   in-flight call will deliver fresh-enough data for both callers.

**Verified**: `pytest -q` (231 passing, up from 225 -- new
`tests/test_item_names.py` coverage for `ensure_many()`: concurrent
resolution, dedup of repeated ids in one call, skipping already-complete
items, tolerating a failed fetch for one id without losing the others,
incremental disk saves, no-op on empty input) and
`env -u DATABASE_URL pytest -q` (same count, matching CI). The frontend
guard was verified in a real browser (throwaway local preview, mocked
`window.fetch` with an artificial 800ms delay standing in for a slow
`/api/snipes` call, same technique as the "localStorage batch cache"
verification): firing two overlapping `fetchBatch()` calls produced exactly
one real network call, the loading state cleared correctly afterward
(`fetchInFlight` reset to `false`, button back to "Refresh"), and a
subsequent call still went through normally -- the guard doesn't get stuck.

### Disk usage / retention (investigated 2026-07-25, not yet built)

Human asked whether Railway's disk usage had been checked, worried about
hitting the ~5GB cap on the hobby plan's Postgres volume default (the
`wow-project` app Volume is the one that actually matters here — AH data,
not the relational `user` table, see "Hosted deployment" above). Checked
live: `RETENTION_DAYS = 14` in `collect_all.py`'s `prune_old_snapshots()` is
real and already running, but the deployment is young enough (~1.33 days of
real history across all 36 FULL/HIGH-pop realms as of this check) that
extrapolating current growth to the full 14-day retention window projects
to roughly **8.7GB** — well past the volume's ~4.9GB practical cap.

Proposed (not built): adaptive retention — keep targeting 14 days by
default, but trim more aggressively if total on-disk usage approaches a
safety threshold (e.g. 4.5GB), rather than a single fixed day count that
either wastes headroom early on or blows the cap later as more realms
accumulate history. Asked the human to confirm both the enforcement
mechanism and the depth/safety tradeoff via `AskUserQuestion`: confirmed
**tighten the existing day-based retention** (not, e.g., switching to a
row/size-based cap or compacting old data instead of deleting it), and
confirmed the human is fine spending "up to 4-5GB" and thinks 14 days is
probably an acceptable target *if* it fits that budget. Net: the adaptive
approach above is the shape both answers point to, but it was explicitly
parked this session, not implemented — next session's starting point is
`collect_all.py`'s `prune_old_snapshots()` and `RETENTION_DAYS`.

### Reusable Claude Code tooling (added 2026-07-25)

The Railway/CI/test incantations above existed only as prose until this
point — every session had to re-derive the exact `docker exec`/
`MSYS_NO_PATHCONV`/`node .../railway.js` invocation from scratch. Turned the
recurring ones into project-scoped slash commands and a skill, all under
`.claude/`:

- **`/railway-status`** — deploy status, latest CI run, volume usage vs the
  4.9GB cap, read-only.
- **`/railway-debug <command>`** — runs a command against live production
  data via `railway ssh` (the actual technique behind every "traced live"
  bug writeup in this file).
- **`/ship`** — this project's real deploy flow end to end: test (including
  the CI-matching `env -u DATABASE_URL` run), commit, push, watch CI,
  confirm the Railway deploy landed, optionally live-verify via
  `/railway-debug`.
- **`project-review` skill** — a repo-specific pre-push checklist (not a
  generic review) covering the traps that have actually bitten this project:
  market_key Python/SQL parity, copper-vs-gold unit bugs, the CI-env test
  mismatch class of bug, the ToS/secrets/Stripe-key guardrails, and the
  frontend-verify-in-a-real-browser convention.

Keep these current the same way as everything else here — if the Railway
CLI invocation changes, or a new recurring trap gets found, update the
command/skill file, not just this paragraph.

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

These are standalone debugging/inspection tools now — none of them are how
the product actually runs (`collect_all.py` inside the deployed app is).
Useful for e.g. manually checking a realm's data or testing a change locally
before pushing.

```
python fetch_snapshot.py --find silvermoon          # realm slug -> cr-id
python fetch_snapshot.py --cr-id 1096 --loop        # collect (48h+), local debugging only
python diff_snapshots.py --cr-id 1096               # build events
python analyze.py --cr-id 1096 summary --top 30
python analyze.py --cr-id 1096 item 152510 --price 2500000   # copper
python analyze.py --cr-id 1096 trace 152510   # per-auction classifications (verification)
python scan_region.py --exclude 1403          # one sweep of all EU realms except your sell realm(s)
python snipe_check.py --sell 1403             # flag discounted listings vs sell-realm sold prices
python snipe_check.py --sell 1403 --items-file watchlist.txt --min-discount 0.3
python dashboard.py --sell 1403               # local dev server on http://127.0.0.1:8000
                                               # (leave ENABLE_BACKGROUND_COLLECTION unset locally)
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
  end-to-end "validated snipe" proof. (Originally noted here as unable to
  tell Warbound/BoE items from BoP ones — that concern turned out to be moot,
  see the "Transferability flag, resolved" note below: every item this flags
  is by definition AH-listed, hence unconditionally unsoulbound, so no such
  distinction was ever needed.)
- Hardening carried over: retention on sell-realm snapshots (compact >7 days
  into daily per-item aggregates before deleting), systemd unit / Windows Task
  Scheduler examples in README, `--since` flag for incremental diffing if event
  rebuilds get slow.

### Phase 2 — commodities feed
**Out of scope (human decision, 2026-07-24) — not being pursued.** Region-wide
collector + quantity-delta inference (see API facts) was the original plan;
separate schema, don't force gear and commodities into one table if this is
ever revisited, but there's no current intent to build it.

### Phase 3 — appearance layer
itemId → appearanceId via wago.tools DB2 exports (`ItemModifiedAppearance`),
cached locally; static API as fallback. Per appearance: count of source items,
obtainability flag (manual curation acceptable at first), region-wide AH
scarcity. This is the differentiator — design the schema carefully.
**Started ahead of Phase 1's remaining hardening (2026-07-23, see the fourth
process deviation above).** Done: itemId → appearanceId mapping + source-item
count, `appearance.py`/`AppearanceCache`, wired into `snipe_check.py`
(`--max-appearance-sources`) and `dashboard.py`. **Not done**: static-API
fallback (the DB2 export has been reliable so far, so this stayed
unimplemented rather than built speculatively), obtainability flags (still
"manual curation acceptable" per the original plan — `source_count` is a
rarity proxy, not a real farmability check), and region-wide AH scarcity
*of currently listed appearances* (source_count is a static catalog-level
rarity signal, not "how many are for sale on the AH right now" — that would
need joining the appearance cache against live listings data, not built yet).
**Transferability flag, resolved — no flag will be built (2026-07-23,
finalized 2026-07-24):** the original plan was a per-item "warband
transferability flag" (warbound vs BoP). That framing was wrong — AH
listings are guaranteed unsoulbound (BoP can't be listed), so warband-bank
transfer isn't item-dependent; the only real risk is equipping/using the
item before moving it, which is a *usage* caveat, not a per-item data flag.
2026-07-24 human decision closes this for good, not just for now: since
every item this tool ever surfaces is, by construction, something actually
listed on the AH, "is this item Warbound or BoP" can never be a live
question for it — there is no non-AH item in scope to need the distinction
for. The existing CAVEAT text (equip/use before transferring locks it) is
the only transferability guidance this product needs.

### Phase 4 — deal score + Discord alerts (first paid feature)
Score = f(discount vs the *sell realm's* sold-price percentile net of 5% AH
cut, sell-realm sales_per_day, appearance scarcity), attached to a route: buy
realm → sell realm. Webhook alert engine with per-user config (their sell
realms + watchlist). Payments require the human's explicit ToS sign-off first.

### Phase 5 — free companion addon + web dashboard
Addon overlays Deal Score on tooltips/sniper results (free, per Blizzard
policy); dashboard is the premium surface. **The *entire* dashboard half of
this phase was pulled forward and is done as of 2026-07-23** — see the
second and third process deviation notes above: hosted, multi-tenant, auth,
live Stripe subscriptions, all shipped. What's still Phase 5, not done: only
the free in-game addon itself remains.

## Definition of done for the current milestone

- pytest suite green in CI-less local runs (`pytest -q`).
- 48h of live snapshots collected without collector death.
- `VALIDATION.md` written with real numbers and a clear go/no-go call.
