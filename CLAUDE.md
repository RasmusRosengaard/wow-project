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
realm's own current cheapest listing for that item, net of the 5% AH cut.

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

**Matching model note (2026-07-26, see HISTORY.md's "Bonus/ilvl matching
removed" entry)**: matching used to be `(item_id, market_key(bonus_key),
pet_species_id, pet_quality_id)` — a coarsened-but-not-fully-collapsed
version of an item's bonus/modifier data (ilvl, sockets, crafted stat
rolls), pooling *some* variance via `market_key()`'s per-item noise
detection while deliberately keeping other variance (a real ilvl tier, a
real socket bonus) as separate markets. That noise-detection heuristic
turned out to have a real, silent failure mode — traced live to a ~20-
sample floor that ~1,223 real items were sitting under, post-2026-07-25's
retention change, so exactly the per-craft fragmentation it existed to
catch kept recurring. Combined with an explicit human decision that
bonus/ilvl differences shouldn't gate a match at all, matching is now
purely **`(item_id, pet_species_id, pet_quality_id)`** — every bonus/ilvl
variant of an item is one market, priced at the sell realm's overall
cheapest listing for that item_id, full stop. The specific bonus_key/ilvl
of the *buy-side* listing is still shown per row (display only, unaffected
— see `dashboard.py`'s `_variant_label()`). `market_key()` and its noise-
detection machinery (`fetch_snapshot.py`) are still real code, just no
longer called from `snipe_check.py` — they remain in use by
`diff_snapshots.py`'s relist detection and `analyze.py`'s manual debugging
tool, both of which still need finer-grained identity than "same item_id."

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
  realm's own current cheapest listing for that item, net of the 5%
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
| `fetch_snapshot.py` | Sell-realm collector CLI: polls one connected realm, writes hourly parquet snapshots (If-Modified-Since aware); backs off on 429/5xx, skips malformed-JSON bodies. Owns `bonus_key()` (canonical variant string, stored/displayed as-is), `parse_bonus_key(bk) -> dict` (read-only tokenizer — `{"bonus_ids": [...], "mods": {type: value}}` — shared by `market_key()`'s own type-28 check and `dashboard._parse_variant()`'s display logic), and `market_key(bk, base_level=None, noise_bonus_ids=None)` (coarser matching-only key — see "Inference logic" below). `ilvl_plausible(claimed, base_level)` — `claimed <= base_level * ILVL_PLAUSIBILITY_MULTIPLE (3) and claimed <= ILVL_ABSOLUTE_MAX (1000)`, both guards required (ratio catches low-base-level junk, absolute cap catches high-base-level junk — see HISTORY.md for the two real cases, items 237468 and 164353, that motivated each). `BONUS_NOISE_*` constants tuned the structural noise test formerly run by `snipe_check._detect_noise_bonus_ids()` — both removed 2026-07-26 along with market_key-based pricing (see "Inference logic"); the methodology is preserved in `HISTORY.md`'s "Bonus-list noise detection" entry if it's ever needed again. |
| `scan_region.py` | Region scanner: sweeps every EU connected realm's *current* listings (`--exclude` to skip sell realms already deep-collected) into `data/listings/{cr_id}.parquet`, overwritten each sweep via a temp-file + `os.replace()` atomic rename (fixed 2026-07-25 after a real production crash — a reader could open the file mid-write). |
| `snipe_check.py` | Joins `data/listings/*.parquet` against the sell realm's own **current cheapest live listing** (not a sold-price percentile — see "What this project is" above) on **`(item_id, pet_species_id, pet_quality_id)`** (changed 2026-07-26, dropping `market_key(bonus_key)` from the match key entirely — see "What this project is" above's matching-model note and "Inference logic" below). `find_snipes()` params: `items`/`min_discount`/`min_gold`/`max_gold`/`min_sell_now`/`max_appearance_sources`/`max_per_item`/`class_quotas`/`top`/`sort`. `check_data_ready(sell) -> str \| None` is the shared "at least one snapshot exists + listings swept" precondition (changed 2026-07-25 from an events-file check — see `collect_all.py`'s row below), used by both the CLI and `dashboard.py`'s `/api/snipes`. `_filter_by_appearance(rows, max_appearance_sources)` applies the transmog-rarity filter (via `appearance.py`) and excludes `NON_TRANSMOG_INVENTORY_TYPES` (profession tool/accessory slots). Every returned row carries the raw `bonus_key` (display only) plus `pet_species_id`/`pet_quality_id` (dashboard groups by the latter two plus `item_id`, not `market_key` anymore) and `appearance_sources`. Prints `snipe_check.CAVEAT` every CLI run (the equip-before-transfer warning). No longer imports/calls `fetch_snapshot.market_key()` or anything base-level/noise-bonus-id related — that machinery moved conceptually to "manual tooling only" (see `diff_snapshots.py`'s row). **`sell_price_suspect`** (added 2026-07-27, human product decision): every row also carries a boolean flag, true when the sell realm's reference price (`cheapest_now`) is more than `SELL_PRICE_SCAM_MULTIPLE` (500x) the *average* current cheapest-per-realm price for that item across the rest of the scanned region (`region_avg`, a new CTE built from the same `buy`/`data/listings` rows already loaded — no new data source or API calls). Traced live to Draenor item 36519 (Moonlit Katana): its only 4 current listings, across 4 different bonus_keys, were all priced at exactly 139,846g75s while Undermine Exchange showed ~1,500g as the real going rate elsewhere — one seller camping every variant at a single joke price, which becomes the *entire* reference price once it's the only thing listed (no history is retained to fall back on, see `collect_all.py`'s row). **Deliberately non-authoritative**: this never filters a row out of `find_snipes()`'s results, only annotates it — the human's explicit call was to surface the signal, not silently drop rows on a heuristic (every prior one in this project, see `market_key()`'s noise-detection history, eventually had a real blind spot). `print_snipes()` shows a `⚠` in a new `flag` column. **`region_median_g`/`region_median_copper`** (added 2026-07-27, human request): every row also carries the *median* (not mean — the mean is what `sell_price_suspect` uses instead, deliberately different: a median is a more honest "typical EU price" to show a human directly, since one other realm's own outlier listing skews a mean far more) of the same per-other-realm cheapest listings, computed in the same `region_stats` CTE (renamed from `region_avg` to hold both statistics). Purely informational, gates nothing. `print_snipes()` shows it in a new `eu_med_g` column. **`class_quotas`** (added 2026-07-27, human request, resolves the "decoy listings crowd out entire categories" issue tracked in `PROGRESS.md`'s "Known gaps/risks" and "Next up" since 2026-07-26): an optional `{bucket: max_rows}` dict (bucket keys match `dashboard.html`'s `ITEM_CLASS_FILTERS` — `weapon`/`armor`/`container`/`profession`/`housing`/`battlepet`/`quest`/`mount`/`recipe` (the last added 2026-07-28, `item_class` 9, confirmed live via `GET /data/wow/item-class/index` — distinct from Profession's 19), see `CLASS_BUCKET_RULES`) that caps each item-class bucket independently instead of one flat top-N by discount% — a real 88.1%-discount Housing snipe was previously crowded out entirely by 99%+-discount decoy listings elsewhere, confirmed live. `None` (the default, and every CLI call) preserves the exact prior flat-ranking behavior. **Rewritten same day** after an initial version (widen the SQL `LIMIT` to a fixed ceiling, bucket/truncate in Python afterward) was proven insufficient live: 450,568 rows qualified region-wide on Draenor at `min_discount=0`, and the first genuine Housing candidate sat at rank 39,524 — past any reasonably-bounded widening, so the "guarantee" wasn't actually one. The real fix: `capped` (the item-deduped match set) is materialized into a `TEMP TABLE` with **no row-count truncation at all**; `item_class`/`item_subclass` are resolved for every *distinct* item id in it via `NameCache` (`_register_class_quota_maps()`, bounded by `CLASS_QUOTA_RESOLVE_LIMIT=20000` — raised from an earlier, too-low 2000 once the real distinct-item count, 15,258 on Draenor, was known; same worst-case-latency reasoning as `collect_all.py`'s prewarm cap, self-healing as the background prewarm catches up) and registered as two DuckDB relations (`class_quota_item_map`/`class_quota_bucket_map`) via `con.register()`; a real `ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY discount_pct DESC)` then ranks and filters per bucket as a genuine SQL window function over the *complete* candidate set — a guarantee, not a "wide enough in practice" hope. A bucket missing from the dict (or given `0`) is excluded entirely. `_class_bucket()`'s "mount" rule needs both `item_class==15 AND item_subclass==5` since Mount is a subclass of the generic Miscellaneous class, not its own item_class — a class-15 item with a different subclass matches no bucket at all, same as any item_class this dict doesn't cover. Verified live via `railway ssh` against real Draenor data both before (finding the bug) and after (confirming the fix) shipping. |
| `collect_all.py` | The sole collection path (runs in-process inside `dashboard.py`'s background loop, `ENABLE_BACKGROUND_COLLECTION=true`, Railway only). Deep-collects FULL/HIGH-population EU realms (`deep_collect_realm_ids()`, cached in-process), then an unscoped `scan_region.sweep()` (every EU realm's listings). Runs every ~10 minutes, not hourly (Blizzard publishes at no fixed clock time). **Only the latest snapshot per realm is kept** (2026-07-25, replacing the earlier adaptive multi-day retention the same day it shipped — pricing turned out to only ever read the latest snapshot, so retaining history was unnecessary complexity): `prune_to_latest(cr)` deletes every snapshot but the newest immediately after a fresh one lands. `diff_snapshots.py` is no longer invoked from this loop as a result — see its own row below. `_prewarm_item_base_levels()` resolves up to `PREWARM_BASE_LEVEL_CAP=1000` more type-28 item base levels per cycle in the background, regardless of user traffic, so `snipe_check._resolve_base_levels()`'s cache converges over a few hours instead of depending on dashboard loads. |
| `dashboard.py` | FastAPI app, read-only web layer over `snipe_check.find_snipes()`. `GET /api/snipes` mirrors the CLI's flags as query params, returns JSON rows + raw-copper prices (`buy_copper`/`sell_copper`) + `pet_species_id`/`pet_quality_id` (unconditional — replaced `market_key` 2026-07-26, see "What this project is" above's matching-model note) + `sell_price_suspect` (added 2026-07-27, passed through `_row_to_json()`'s explicit dict — see `snipe_check.py`'s row above for what it means; never filters server-side, `dashboard.html`'s "Hide flagged" checkbox is the only thing that can hide it, and only when checked) + `region_median_g`/`region_median_copper` (added 2026-07-27, same passthrough, purely informational — see `snipe_check.py`'s row above) + (when `names=true`) `icon`/`quality_color`/`quality` (the tier name, e.g. `"EPIC"` — added 2026-07-25 to back the dashboard's rarity filter)/`item_class`/`item_subclass`/`is_profession_item`/a smart `variant` summary (`ilvl NNN` only when `_variant_label()`'s ilvl-plausibility check passes, via `fetch_snapshot.ilvl_plausible()`/`parse_bonus_key()`, else a bonus-count fallback — `variant_raw` always carries the raw string, still per-row display only, unaffected by the matching change). Runs the query via `await asyncio.to_thread(_run_query)` **and** separately runs the `names=true` row-building step via its own `await asyncio.to_thread(_build_rows)` (see "Real production outage" below — the per-row `NameCache` lookups can make blocking Blizzard calls; `find_snipes()` itself no longer does mid-query since `_populate_market_keys()` was removed 2026-07-26, except via `_filter_by_appearance()`'s `NameCache().inventory_type()` calls when `max_appearance_sources` is set). `_build_rows()` prewarms the batch's distinct item ids via `NameCache.ensure_many()`/`.ensure_icons_many()` (concurrent) before the per-row translation, so a cold realm resolves in parallel instead of one blocking call at a time. **Auth/tiers**: `current_active_user` gates every `/api/*` route (not `current_subscribed_user` — free tier, see below). `SNIPE_TIER_CAPS`/`_snipe_cap(user)`: 250 (logged in, no subscription) / 2000 (active subscription) / 10000 (superuser, raised from 5000 2026-07-26). **`_class_quotas(user)`** (added 2026-07-27, human-specified numbers, mirrors `_snipe_cap()`'s same tier detection): returns the tier's `FREE_CLASS_QUOTAS`/`SUBSCRIBED_CLASS_QUOTAS`/`SUPERUSER_CLASS_QUOTAS` dict, passed to `find_snipes()`'s `class_quotas` param on every `/api/snipes` call. Free tier: `weapon` 50 / `armor` 100 / `housing` 40 / `mount` 5 / `battlepet` 5 / `recipe` 50 (sums to exactly its 250 cap; deliberately zero `quest`/`profession`/`container` — a human product decision, not an oversight). **`recipe` added 2026-07-28** (human decision, after realizing recipes — item_class 9, confirmed live, distinct from Profession's 19 — had no bucket at all): free tier's `weapon` quota was cut from 100 to 50 to fund an equal 50 `recipe`, rather than raising the 250 cap itself. Subscribed (2000) and superuser (10000) both keep fixed floors `quest` 100 / `profession` 100 / `container` 20 (not scaled — free tier has zero of these to scale a ratio from) and scale the remaining budget using free tier's own weapon/armor/housing/mount/battlepet/recipe ratios (20%/40%/16%/2%/2%/20%, weapon and recipe now equal instead of weapon alone at 40%): subscribed → 355/712/285/36/36/356, superuser → 1955/3912/1565/196/196/1956 (weapon/armor/housing/mount/battlepet/recipe order). Every tier's dict sums to exactly that tier's `SNIPE_TIER_CAPS` value (`tests/test_dashboard.py::test_class_quotas_by_tier_sum_to_the_tier_cap` asserts this). `_enforce_realm_lock(user, sell, session)`: a free-tier (non-subscribed, non-superuser) account is locked to the *first* sell realm it ever queries (`db.User.locked_sell_realm`, written once); querying a different realm afterward is a 403. `GET /api/realms` (backed by `_list_snapshotted_realms()` — any realm with at least one snapshot, since 2026-07-25; no longer requires an events file, see `collect_all.py`'s row)/`GET /api/status` (returns `has_data`, renamed from `events_exist` the same day — true once a snapshot has ever been retrieved for the realm)/`GET /api/me` (also exposes `locked_sell_realm` and, since 2026-07-29, `nickname` — see the next paragraph). **`PATCH /api/me/nickname`** (added 2026-07-29, human request — Snipe Board posts used to show the account's real email publicly): body `{"nickname": str}`, strips whitespace, rejects empty or over `NICKNAME_MAX_LEN` (50) with 400, writes `db.User.nickname`. Gated by `current_active_user` like every other `/api/*` route; `forum.create_post()` independently requires `user.nickname` be set before it'll accept a post (see `forum.py`'s row below) — this endpoint is what `static/snipeboard.html`'s post dialog and `static/profile.html` both call to set/change it. `GET /api/log/realms`/`GET /api/log?sell={cr}` and `GET /pricing`/`GET /log` are the deliberately unauthenticated routes (realm names/retrieval timestamps/pricing aren't the paid product). **Routing changed 2026-07-26**: `GET /` now serves `static/landing.html` (a public marketing page, human request); the sniper tool itself moved to the new `GET /snipes`, which serves `static/dashboard.html` (unauthenticated route, same as before — auth is enforced client-side by the page's own `init()`). Every other page's nav "Dashboard" link and `login.html`'s post-login redirect were updated from `/` to `/snipes` to match; `pricing.html`'s "Dashboard" nav link was removed outright (human request) rather than repointed. `python dashboard.py --sell 1403` runs it locally on `127.0.0.1:8000` — leave `ENABLE_BACKGROUND_COLLECTION` unset locally. |
| `static/dashboard.html` | Single static file, vanilla JS, no build step. Light "assay ledger" visual identity with a dark-mode toggle (`localStorage`-backed, shared pre-paint `<head>` script across all six pages) — see `HISTORY.md`'s "UI design pass" for how this was chosen. Quality-colored item-icon rings, gold/silver/copper coin-icon formatting, a hover tooltip, click-icon-for-Undermine-Exchange-link. **Client-side filtering architecture** (2026-07-24): fetches one loose batch per sell realm (`fetchBatch()`, `BATCH_TOP=10000` — the ceiling across every tier, must stay >= the highest tier cap, the server clamps down to the real cap via `_snipe_cap()`) and re-filters/re-sorts entirely in the browser (`applyFilters()`/`renderTable()`/`compareRows()`) — only a sell-realm switch, an item-id change, or the 60s auto-refresh timer re-fetches (the manual Refresh button was removed 2026-07-25 as redundant — `checkForUpdates()` already covers "is there new data" on its own). `checkForUpdates()` (auto-refresh + initial load **and**, since 2026-07-26, a realm/item-id switch too via `onCandidatePoolChange()`) checks the cheap `/api/status` timestamp first and only re-fetches `/api/snipes` when it's actually advanced — a realm switch used to always re-run the full query even when the just-selected realm's cached batch was already fresh, since it called `fetchBatch()` directly instead of going through this check (verified live in a real browser against a stubbed backend: switching back to an already-cached, unchanged realm now does zero new `/api/snipes` calls, only the cheap status check). The last-fetched batch is cached in `localStorage` (keyed per sell realm + item filter, cleared on logout, `CACHE_VERSION`-namespaced so a row-shape change can't get painted against code expecting different fields — bumped to 2 on 2026-07-27's `sell_price_suspect`/`region_median_*` addition) and painted instantly on load. **`saveCache()` hardened twice more the same day, both traced live against a real production account** (Draenor, superuser tier): (1) it now purges every *other*-version `snipe_cache_v*` key before writing, not just on logout — a `CACHE_VERSION` bump orphans the old entry forever (nothing else ever reads it), but it doesn't disappear from `localStorage` on its own, and a full `BATCH_TOP`-row batch can sit close enough to the browser's per-origin quota that the orphaned entry alone blocked the new version's write with `QuotaExceededError` — silently, since the write was already wrapped in a fail-soft `catch`, so caching was permanently broken with no visible error, every load painting from nothing and reading as a cold "Loading latest data…" forever. (2) Even with that purged, the *current* payload alone measured 11.12MB for 10,000 rows on Draenor (its huge near-100%-discount decoy-listing pool, see "Known gaps/risks" in `PROGRESS.md` — likely over quota on its own for any account hitting a large chunk of that pool), so `saveCache()` now retries with progressively fewer rows (halving up to 6 times) instead of giving up outright — a partial cache still serves the "instant paint" purpose, and the next real fetch always replaces it with the true current batch regardless of how many rows made it in. **`fetchBatch()`'s loading treatment is quiet on a background refresh** (fixed 2026-07-27, human report: the dim/blocked `.table-wrap.loading` state — meant for a cold load with nothing to show yet — was firing on every silent 60s-timer refresh too, roughly once an hour when Blizzard's AH data actually republishes, often with no visibly different result; felt like an unwarranted interruption). `isBackgroundRefresh = cachedRows.length > 0` gates it: rows already on screen means `setLoading(true)`/the blocking dim is skipped entirely (and a failed background refresh no longer blanks the table either — it just leaves the last-known-good rows and lets the next tick retry); only a genuine cold load (nothing cached yet) still shows the blocking "Loading latest data…" state. Verified live via a direct `fetchBatch()`/`checkForUpdates()` console exercise against both cases. Filter rail: discount%/gold range/sell-now/max-per-item/unique-transmog/"hide flagged"/9-way item-class (added `recipe` 2026-07-28, item_class 9)/6-way rarity checkboxes (each group OR'd together, groups AND'd), all client-side — the rarity filter (added 2026-07-25) reads the new `quality` field (`QUALITY_FILTERS`) and shows a color swatch matching each tier's icon-ring color. The "hide flagged" checkbox (added 2026-07-27, unchecked by default — nothing hidden unless asked) reads `sell_price_suspect` (see `snipe_check.py`'s row above); flagged rows also show a `⚠` (`.suspect-flag`) next to the sell price regardless of the checkbox state, so the signal is visible even when not filtering on it. An **"EU median"** column (added 2026-07-27, human request) shows `region_median_copper` next to Sell price, sortable (`eu_median` sort key) same as every other numeric column, plus a matching row in the hover tooltip — purely informational, doesn't filter anything. Rows group by `(item_id, pet_species_id, pet_quality_id)` (`groupKey()`, changed 2026-07-26 from `market_key` — see "What this project is" above's matching-model note; pet identity is still needed since every caged pet shares one item_id), best-discount first, with a `▾ N` expand toggle. Free-tier accounts get a `requirePick` realm dropdown (no default pre-selected — an earlier version silently locked new accounts to the server's site-wide default realm on first load, fixed 2026-07-25). Topbar brand mark (seal + wordmark) is wrapped in a link to `/` (2026-07-26, `.brand-link`, `display: contents` so it doesn't disturb `.brand`'s flex layout) — same convention now shared by every other static page (see the row below). The `seal-label` "Validated Data" text next to it was removed the same day (human request); the seal icon itself is unchanged. Nav is `Log`/`Profile`/`Log out` — the `Pricing` link was removed (2026-07-26, human request) since this navbar is only ever seen by already-logged-in users inside the tool; `static/landing.html`'s own navbar (the marketing frontpage) keeps its `Pricing` link regardless of login state — a deliberate distinction, not an oversight. |
| `static/landing.html` | New (2026-07-26): the public marketing page now served at `/` (see `dashboard.py`'s row above — the sniper tool itself moved to `/snipes`). Same "assay ledger" visual identity/tokens as every other page, plus one deliberate, restrained departure: the hero `<h1>` alone uses a serif system font stack (`Georgia, "Times New Roman", serif`) against the sans-serif body everywhere else, meant to read like a heading on a certified/appraisal document — reinforcing the existing seal-mark thesis rather than introducing a new one. Hero's signature element is a **real excerpt of the actual ledger table** (human request, replacing an earlier custom-card mockup): the same `table`/`item-cell`/`item-icon`/`money`/`coin` markup and class names as `dashboard.html`'s own table, one illustrative row (labeled "Example listing: illustrative, not live data" so it's never mistaken for real numbers), not a paraphrased approximation. A numbered 3-step "how it works" section (genuinely sequential, not decorative numbering: "Pick your sell realm" / "Scans every EU realm" / "Only genuine snipes surface") follows; the earlier "Not another sniper" TSM/Auctionator comparison section was removed outright (human request), as was a closing "Every account gets real data" CTA section (human request, along with its now-unused `.closing` CSS and the `#closing-cta` reference in the CTA-personalization script below). Copy avoids em-dashes throughout (human request) — periods/colons instead. Hero subheading (and the matching `<meta name="description">`) rewritten (human request) to stop overclaiming "already validated" — now explicitly says the tool calculates the real price difference (the literal "arbitrage") and that not every flagged gap is a genuine bargain, since a troll/camped listing can still be the reference price (see "Known gaps" in `PROGRESS.md` — this was a real accuracy fix, not just polish). **Does not redirect a logged-in visitor away** (tried and reversed the same day, human feedback: forcing a redirect means nobody signed in could ever actually see the page, e.g. a shared link or checking their own marketing copy) — instead, checks `/api/me` on load and swaps the sign-up CTAs (`#nav-cta`/`#hero-cta`) to "Go to dashboard" pointing at `/snipes`, and removes the "Log in" nav link (`#nav-login`), leaving the rest of the page (including "See pricing") untouched either way. No brand-mark self-link (it *is* `/`). |
| `Dockerfile` / `.dockerignore` / `docker-entrypoint.sh` | Packages the web app into a container; entrypoint runs `alembic upgrade head` then `exec python dashboard.py`. Reads `PORT` (Railway-injected) and `DEFAULT_SELL` (UI prefill only) from env. |
| `tests/` | pytest suite (`pytest -q`; root `conftest.py` makes top-level modules importable). Real duckdb/pyarrow throughout, no mocking of the data layer; live Blizzard calls are stubbed. `isolate_item_names_cache`/`isolate_appearance_cache` autouse fixtures redirect cache paths into `tmp_path` so tests never touch the real gitignored caches. **Always run both** `pytest -q` and `env -u DATABASE_URL pytest -q` before pushing — CI has no `DATABASE_URL` set, and a local `.env` can mask a missing test-fixture override (see "CI incident" note under `dashboard.py`'s history in `HISTORY.md`). |
| `diff_snapshots.py` | **Core IP**, the snapshot-diff classification engine — **no longer invoked automatically** (2026-07-25, see `collect_all.py`'s row above: pricing only ever reads the latest snapshot, so nothing keeps multi-day history around for this to diff anymore). Still real, still correct, still usable by hand against manually-accumulated snapshot history (e.g. `fetch_snapshot.py --loop` run locally, then `python diff_snapshots.py --cr-id X`). `relist_key(r)` — non-price identity `(item_id, market_key(bonus_key), pet_species_id, pet_quality_id, quantity)` — **the last remaining live caller of `fetch_snapshot.market_key()`** (2026-07-26, since `snipe_check.py`'s pricing join dropped it, see "What this project is" above); relist detection genuinely needs finer-than-item_id identity (a vanished ilvl-636 listing relisting as ilvl-636, not as ilvl-40, is what "relisted" means), unlike pricing. Price is matched separately via `_find_relist_match()` within `RELIST_PRICE_TOLERANCE` (±15%, added 2026-07-25) instead of requiring exact equality, so a troll reposting at a nearby joke price still counts as a relist rather than a fake `inferred_sale`. See "Inference logic" below for the full classification rules. |
| `analyze.py` | DuckDB CLI: liquidity summary + per-item sold-price distribution / percentile check / per-auction trace (manual debugging only, not on the pricing path). `connect(cr)` builds `ev`/`sales` from `data/events/{cr}.parquet` if a human has run `diff_snapshots.py` for that realm; otherwise (the production default since 2026-07-25) builds an empty `ev` table instead of erroring, so the live pipeline stays functional with no events file at all. Registers `market_key(bk, base_level := NULL)` as a DuckDB macro (three helper macros: `_ilvl28_value`/`_ilvl28_implausible`/`_strip_type`) — the SQL-side mirror of `fetch_snapshot.market_key()`'s base_level behavior, kept honest by `tests/test_market_key.py`'s parity check; used by this file's own manual debugging queries, not by the live pricing path (see "What this project is" above). `noise_bonus_ids` (a separate, Python-only argument to `fetch_snapshot.market_key()`, never mirrored in this SQL macro at all) was previously computed by `snipe_check._detect_noise_bonus_ids()`, removed 2026-07-26 along with the rest of `market_key`-based pricing/matching (see "Inference logic" below) — nothing currently computes it. |
| `appearance.py` | `AppearanceCache`: itemId → transmog-appearance rarity (`source_count` — how many distinct item ids grant the same appearance region-wide), cached at `data/appearances.json`, source is wago.tools' `ItemModifiedAppearance` DB2 export (`python appearance.py --refresh`, manual/periodic — not wired into the Railway background loop, since wago.tools is outside this project's Blizzard rate-limit budget). Display/filter-only, never-raises. A v1 rarity proxy, not a real obtainability model. |
| `item_names.py` | `NameCache`: display/filter-only, never-raises lookups backed by the static item/pet API, cached at `data/item_names.json`. `.get()` name, `.icon()`, `.quality_color()`, `.quality()` (the tier name itself, e.g. `"EPIC"` — added 2026-07-25 alongside `.quality_color()`'s derived ring color, backs the dashboard's rarity filter), `.base_level()` (still used for display only — `dashboard._variant_label()`'s "ilvl NNN" check — since matching stopped using ilvl 2026-07-26, see "What this project is" above), `.inventory_type()`, `.item_class()`/`.item_subclass()` (Blizzard's official ids, confirmed live via `GET /data/wow/item-class/index`). `.ensure_many(ids, max_workers, limit)` resolves concurrently, used by `collect_all._prewarm_item_base_levels()` (its other original caller, `snipe_check._resolve_base_levels()`, was removed 2026-07-26 along with market_key-based matching). `.ensure_icons_many(ids, max_workers)` (added 2026-07-26) is the same concurrent-batch pattern for `.icon()` specifically — `.icon()` isn't covered by `.ensure_many()`'s `_fetch_item_details` batch at all (icons come from a separate media endpoint), which was a real gap: `dashboard.py`'s `names=true` row-building used it item-by-item and hung on any never-before-queried sell realm (see "Real production outage" below). All fields backfill onto an older cache entry missing them, self-healing an old cache file instead of returning `None` forever. |
| `db.py` | Async SQLAlchemy for the *relational* data only (users/sessions/subscription state, and now forum posts) — separate from the parquet+DuckDB AH data layer. `User` model: FastAPI-Users base fields + `stripe_customer_id`/`stripe_subscription_id`/`subscription_status`/`subscription_current_period_end` (written only by `billing.py`'s webhook) + `locked_sell_realm` (nullable, written only by `dashboard._enforce_realm_lock`) + `nickname` (added 2026-07-29, nullable, no uniqueness constraint — public display name for Snipe Board posts, written only by `dashboard.update_nickname()`; see that route's entry above for why it exists). `ForumPost` model (added 2026-07-29): `author_id` (FK to `user.id`, using the same `fastapi_users_db_sqlalchemy.generics.GUID` type as `user.id` itself) + `author_email` (denormalized at post time, **no longer exposed by the API** — see `forum.py`'s row below, kept only for internal reference) + `author_nickname` (added 2026-07-29, denormalized the same way and for the same reason as `author_email` — a post keeps showing the nickname the poster had *at post time* even if they change it later; nullable only because posts made before this column existed have none) + `title` (nullable) + `image_filename` + `created_at` (set in Python via `datetime.now(timezone.utc)`, not a DB `server_default`, so it's identical across the Postgres-in-production/SQLite-in-tests split). Tests override the session dependency with SQLite. |
| `forum.py` | Backing module for the **Snipe Board** page (renamed 2026-07-29 from an initial "Forum" — human request, module/route names weren't renamed to match, same precedent as `dashboard.py` serving `/snipes`). "Post a snipe you found" feature — an image (required) + optional title, logged-out visitors can see every post, posting requires login **and a nickname** (added 2026-07-29, human feedback: posts were showing the account's real email publicly, the wrong default). `create_post()` rejects with 400 if `user.nickname` is unset — enforced here, not at registration, since that also naturally covers every account that registered before nicknames existed; `static/snipeboard.html`'s post dialog is what actually prompts for one inline (via `PATCH /api/me/nickname`, see `dashboard.py`'s row above) before a first post, but this check is the real boundary, not that client convenience. `_post_to_json()` returns `author_nickname` (falling back to the literal string `"Anonymous Sniper"` for the small number of posts made before this column existed — see `db.ForumPost`) and never `author_email`. Deliberately minimal: no editing/deleting/comments/moderation. Two `APIRouter`s: `router` (`/api/forum/posts`, `GET` public / `POST` gated by `auth.current_active_user`) and `image_router` (`/forum/images/{filename}`, public, reads `IMAGE_DIR` fresh per request rather than a `StaticFiles` mount — a fixed-at-mount-time directory can't be redirected into a tmp dir for tests, this can via `monkeypatch.setattr(forum, "IMAGE_DIR", ...)`). Images are plain files under `DATA/forum_images/` on the same persistent volume `data/snapshots`/`data/listings` already use (`ALLOWED_IMAGE_TYPES` content-type allowlist, not a client-supplied extension; `MAX_IMAGE_BYTES` = 5MB) — server-generates the filename (`uuid4().hex` + the validated extension) so the client's original filename is never trusted for anything, including the path `serve_image()` reads from (`Path(filename).name` strips any directory components as defense in depth). Wired into `dashboard.py` via `app.include_router(forum.router)` / `app.include_router(forum.image_router)`, plus a public `GET /snipe-board` route serving `static/snipeboard.html` (same client-side-gate convention as `/snipes` — the page itself checks `/api/me` to decide whether to show the "+ Post a snipe" button or a "log in to post" link). |
| `auth.py` | FastAPI-Users wiring: email/password register+login, cookie-based sessions. `current_active_user` gates login-only routes. `has_active_subscription(user)` — single source of truth for "unrestricted" (active subscription OR superuser), used by both `_enforce_realm_lock` and anywhere else that needs the same check. `current_subscribed_user` (402 if not subscribed) is defined but currently unused by any route (free tier superseded it) — kept as a legitimate dependency for a future genuinely-subscriber-only route. `COOKIE_SECURE` env toggle (`false` in dev `.env`, unset/secure in production). |
| `billing.py` | **Live Stripe mode** (human decision — deployed straight to live, no test-mode verification pass). `POST /billing/checkout` creates a Checkout Session for the single €4.99/mo price; `POST /billing/webhook` verifies the Stripe signature and handles `checkout.session.completed`/`customer.subscription.updated`/`customer.subscription.deleted`, the only writer of the user's subscription fields. Still on the full `sk_live_...` secret key, not a restricted key (see "Roadmap" — human-only to change). |
| `alembic/`, `alembic.ini` | DB migrations. `env.py` reads `DATABASE_URL` from the environment. |
| `static/login.html`, `register.html`, `subscribe.html`, `profile.html`, `log.html`, `pricing.html`, `snipeboard.html` | Plain HTML/JS, same no-build-step convention, same visual identity/dark-mode as `dashboard.html`. `profile.html` shows subscription status + Stripe customer portal link + (added 2026-07-29) a nickname field/Save button (`PATCH /api/me/nickname`, see `dashboard.py`'s row above) — the one place to change a nickname after it's first set via `snipeboard.html`'s post dialog. `subscribe.html` explains what a subscription changes vs the free tier, links to `/pricing`. `log.html` — public retrieval-time log. `pricing.html` — public Free-vs-Subscriber comparison + FAQ, doesn't advertise the internal superuser/10000 tier (founder/admin headroom, not purchasable, raised from 5000 2026-07-26). `snipeboard.html` (added 2026-07-29 as `forum.html`, renamed the same day — human request, "Forum" undersold what it actually is) — public feed of user-posted snipes (see `forum.py`'s row above); checks `/api/me` on load to swap between a "+ Post a snipe" button (logged in) and a "log in to post" link (logged out), same client-side-gate pattern `landing.html` uses for its CTA swap, not a hard redirect — the feed itself is never gated. The post form lives in a native `<dialog>` (`showModal()`), opened only by that button, so browsing the feed never shows the form unasked; a second `<dialog>` (`#lightbox`) opens full-size on clicking any posted screenshot (`cursor: zoom-in`) and closes on a click anywhere inside it or Escape (native `<dialog>` behavior) — no separate full-resolution asset, it's the same upload just unconstrained by the feed's `max-height`. The post dialog's `#nickname-field` only shows when `currentNickname` (set from `/api/me` at page load) is falsy — a real, unset `PATCH /api/me/nickname` call happens before the post itself if it's visible; `profile.html` is where an already-set nickname gets changed later (a plain input + Save button, same endpoint), so the dialog isn't the only place this can be edited. Every page's brand mark links to `/` now (`.brand-link`, see `dashboard.html`'s row above for the mechanism); `profile.html`/`log.html`'s "Dashboard" nav link points to `/snipes` (changed 2026-07-26 along with the routing move); `pricing.html`'s "Dashboard" nav link was removed outright (human request) rather than repointed; `subscribe.html`'s "← Back to dashboard" link was removed the same way. `login.html`'s post-login redirect goes to `/snipes`, not `/`. `pricing.html`'s "Log in" nav link (`#nav-login`) is unconditionally shown by default but removed on load if `/api/me` confirms an active session (2026-07-26, human request — the page previously always showed "Log in" even to an already-authenticated visitor) — no nav link put back in its place, since the "Dashboard" link here was deliberately removed already, not an oversight to restore. |
| `requirements.txt` | `requests`, `pyarrow`, `duckdb`, `fastapi`, `uvicorn`, `httpx`, `fastapi-users[sqlalchemy]`, `sqlalchemy[asyncio]`, `asyncpg`, `aiosqlite` (tests only), `alembic`, `stripe`, `pytest-asyncio`, `python-multipart` (added 2026-07-29 for `forum.py`'s image upload form — FastAPI's `Form`/`File` parsing needs it, not imported directly). |
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
- **Bonus/ilvl-aware matching (`market_key()`) was dropped from live
  pricing** (2026-07-26) — see "What this project is" above's matching-
  model note. A human product decision (matching should be pure `item_id`,
  full pooling, lowest price wins, ilvl/bonus differences display-only)
  made after a live-traced bug in the noise-detection heuristic that
  matching depended on. Simplifies `snipe_check.py` substantially (four
  helper functions and a temp-table join removed); `market_key()` itself
  remains real code, still used by `diff_snapshots.py`'s relist detection
  and `analyze.py`'s manual debugging tool.

Not yet present: sell/scan realm config split (manual `--exclude`/`--items`
flags stand in), `--since` incremental diffing, `VALIDATION.md`.

## Architecture & data layout

```
Blizzard API ──> data/snapshots/{cr_id}/{epoch_ts}.parquet   (sell realms; only the
                        │                                      LATEST is kept automatically
                        │                                      since 2026-07-25, see
                        │                                      collect_all.py's module docstring)
                        v
                 snipe_check.find_snipes() reads it directly -- no diffing needed,
                 pricing is the sell realm's own current cheapest live listing

data/state/{cr_id}.json  — Last-Modified cursor for the sell-realm collector

Blizzard API ──> data/listings/{cr_id}.parquet   (region scanner, ALL EU realms;
                                                    latest sweep only, overwritten,
                                                    no history — buy side)
```

**Manual/ad-hoc path only** (diff_snapshots.py is no longer run automatically): a
human who wants the sale-classification signal first accumulates real history
themselves (e.g. `fetch_snapshot.py --loop` run locally, which is unaffected by
collect_all.py's prune-to-latest), then runs `diff_snapshots.py --cr-id X` by
hand to produce `data/events/{cr_id}.parquet` (derived; recomputed from
scratch each run — always safe to delete), which `analyze.py`'s DuckDB views
(`snaps`, `ev`, `sales`, `span`) then read. `analyze.connect()` works fine
with no events file too — `ev`/`sales` just come back empty.

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
  15=Miscellaneous with subclass 5=Mount. 9=Recipe (confirmed live
  2026-07-28, distinct from Profession's 19 — see `snipe_check.py`'s
  `CLASS_BUCKET_RULES` row below).
- Rate limit 36,000 req/h, 100 req/s. Headroom is not an invitation — stay
  polite. `collect_all._prewarm_item_base_levels()` and `dashboard._build_rows()`'s
  `NameCache.ensure_many()`/`.ensure_icons_many()` calls are where this
  pipeline makes bulk Blizzard calls; the first is explicitly capped per
  call (see its file-table entry above).
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
classification itself is still real, correct code, but (since 2026-07-25)
no longer runs automatically; it's a manual/ad-hoc tool for `analyze.py`'s
debugging commands, not a live signal (see `collect_all.py`'s row in
"Current state").

### `market_key()` — the matching-only coarsening of `bonus_key`

**No longer used by `snipe_check.py`'s live pricing/matching** (changed
2026-07-26 — see "What this project is" above's matching-model note; that
join is now plain `item_id`, no bonus/ilvl pooling logic needed at all).
Still real, still correct, still used by `diff_snapshots.relist_key()`
(needs finer-than-item_id identity for relist detection) and `analyze.py`'s
manual debugging macro. Kept here for that reason, not as dead documentation.

`bonus_key()` is pure and canonical — never changes what's stored/displayed.
`market_key(bk, base_level=None, noise_bonus_ids=None)` is a *separate*,
coarser key used only for matching/grouping (relist detection, and
formerly the buy/sell join in `snipe_check.py`) — real crafted-item
variance and Blizzard's undocumented per-craft/per-instance ids otherwise
fragment one liquid market into dozens of near-unique buckets.

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
  Python-only. Formerly computed by `snipe_check._detect_noise_bonus_ids()`
  via a **structural** test (not a frequency threshold — a flat cutoff was
  tried and live-disproven, see `HISTORY.md`): a bonus-list value was
  treated as real only if it had a *partner* — reliably co-occurring with
  another specific value (a companion pair), or belonging to a small
  mutually-exclusive set that jointly covers most of an item's listings (a
  partition); per-craft noise had neither shape. **Removed 2026-07-26**
  along with the rest of `market_key`-based pricing (see this section's
  intro) — the per-20-sample floor this test needed (`BONUS_NOISE_MIN_SAMPLES`)
  turned out to silently fail on ~1,223 real items post-2026-07-25's
  retention change (see `HISTORY.md`'s "Bonus/ilvl matching removed" entry),
  and matching no longer needs bonus-id noise detection at all now that it
  doesn't look at bonus_key. Nothing currently computes this parameter —
  every real caller (`relist_key()`) passes `None`, same as always.

Any **new** modifier type discovered to be junk needs either strong
corroborating evidence from real data (e.g. identical troll price across
different values) or explicit human confirmation it doesn't affect the
thing it might affect (transmog appearance) before being added to the
unconditional ignore set — don't assume.

Mirrored as a SQL macro in `analyze.connect()` (`MARKET_KEY_MACRO_SQL`,
three helper macros) for DuckDB-side grouping — two independent
implementations kept honest by `tests/test_market_key.py`'s parity check
(runs the same real-item vectors through both, asserts identical results).
`noise_bonus_ids` is **not** mirrored in SQL (Python-only, and — since
2026-07-26 — nothing currently computes it at all, see above) — the parity
test only covers the base_level argument shape.

**If you touch any of this**: update both implementations, add a real
(not invented) test vector to `tests/test_market_key.py`, and check the
`project-review` skill's matching-logic checklist before shipping.

## Real production outage, lesson for next time (2026-07-25, recurred 2026-07-26)

`snipe_check._resolve_base_levels()` (since removed 2026-07-26 along with
market_key-based matching, see "What this project is" above — this
narrative describes what was true at the time) could make blocking
Blizzard API calls. `dashboard.py`'s `api_snipes()` is an `async def` route
but was calling it directly on the event loop thread — on a cold cache,
hundreds of sequential blocking calls froze the *entire* single-process
server, including unrelated routes, for the call's full duration. Fixed via
`asyncio.to_thread(...)`. **Full incident in `HISTORY.md`.**

**The same bug recurred 2026-07-26 at a second call site the first fix
didn't cover**: the `names=true` per-row translation added to `api_snipes()`
after the original fix (`NameCache.get()`/`.icon()`/`.quality()`/etc., each
a cache-miss fallback to a blocking Blizzard call) ran directly on the event
loop, never wrapped in `to_thread` at all. Symptom: switching the dashboard's
sell realm to one never queried before hung/timed out, while an
already-warmed realm (Draenor) always worked — the tell that it's *this*
class of bug, not a data issue. Fixed the same way (`asyncio.to_thread`),
plus closed a second gap found in the process: `.icon()` was never covered
by `NameCache.ensure_many()`'s concurrent batch at all (icons are a separate
media endpoint) — added `.ensure_icons_many()` alongside it. See
`HISTORY.md`'s "Realm-switch hang/timeout" entry for the full trace.

**Lesson for next time a route gains a synchronous, possibly-slow
dependency** (a new network call, a large one-time computation): ask
whether it can block *other* requests, not just whether it's correct or
fast on a warm cache. An `async def` FastAPI route does not protect you
from this by itself — it only helps if the blocking work is actually
offloaded, and **that check has to be repeated for every new blocking call
site added later, not just the one that triggered the original fix** — this
is exactly how the 2026-07-26 recurrence happened. There is still no test
coverage for "does this route block the event loop" — worth a regression
test (a slow stub swapped into `_resolve_base_levels()` or `NameCache`,
asserting a concurrent lightweight request still completes quickly) if this
class of bug recurs a third time.

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
