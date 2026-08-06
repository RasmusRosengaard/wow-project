# Data pipeline modules

Collection, scanning, pricing and the caches they read. This is the core
product path: Blizzard API -> parquet snapshots -> DuckDB join -> snipe rows.

## `blizz.py`

`.env` loader, OAuth client-credentials token, `api_get()`, realm-slug↔id
lookups: `find_connected_realm`, `list_connected_realms()`,
`connected_realm_population()`,
`connected_realm_slugs()`/`connected_realm_realms()`.
`connected_realm_realms()` also returns each member realm's `category` (added
2026-07-31, human request — "which language does this realm belong to"):
Blizzard's own per-realm field, confirmed live across all 92 EU connected
realms as one of `English`/`German`/`French`/`Italian`/`Russian`/`Spanish`,
never mixed within one connected realm. **`api_get()` is now rate-limited via
two shared, thread-safe `_TokenBucket`s (`_burst_limiter`: 90 capacity/90 per
second, `_hourly_limiter`: 34000 capacity/~9.44 per second)** (added
2026-08-01, real incident: an ad-hoc diagnostic script's
`NameCache.ensure_many()` call fired a burst of thousands of requests with
zero coordination with anything else in the process, starving
`collect_all.py`'s background collector — confirmed live via production logs,
several realms' collection failing outright with `HTTP 429`). Before this,
`api_get()` had no rate awareness at all — `fetch_snapshot.py` has its own
local retry/backoff for 429s on the one endpoint it calls, but
`scan_region.py`/`item_names.py`/`appearance.py` all call `api_get()` directly
with none, and none of the independent callers had any shared notion of how
much of the budget something else in the same process had already spent. Both
buckets are consulted on every single call (`acquire()` blocks/sleeps
transparently, no caller-side throttling logic needed) — a token bucket rather
than just a concurrency cap, since a cap alone doesn't bound throughput if
individual requests are fast. Capacities sit a bit under Blizzard's published
limits (90/34,000 vs the real 100/36,000 — see "Blizzard API facts" below) as
headroom. **The regular background collection cadence (10 min, see
`collect_all.py`'s row) was deliberately left unchanged, not slowed to hourly,
despite this incident** — confirmed live it only uses ~2.2% of the hourly
budget (~810 of 36,000 req/h: ~35 deep-collect realms, mostly cheap 304s via
If-Modified-Since, + ~100 region-sweep realms, no conditional request at all)
— the incident was caused by an uncoordinated *burst*, not the steady-state
cadence, and slowing the cadence to hourly would cost real data freshness (the
whole reason it's 10 min and not 60, see that row) for negligible safety
benefit; the rate limiter is the fix that actually matters regardless of
cadence. Covered by `tests/test_blizz.py`'s `_TokenBucket`/`api_get()` tests
(a `_FakeClock` makes the wait-duration assertions deterministic without a
real test sleeping).

## `fetch_snapshot.py`

Sell-realm collector CLI: polls one connected realm, writes hourly parquet
snapshots (If-Modified-Since aware); backs off on 429/5xx, skips malformed-
JSON bodies. Owns `bonus_key()` (canonical variant string, stored/displayed
as-is), `parse_bonus_key(bk) -> dict` (read-only tokenizer — `{"bonus_ids":
[...], "mods": {type: value}}` — shared by `market_key()`'s own type-28 check
and `dashboard._parse_variant()`'s display logic), and `market_key(bk,
base_level=None, noise_bonus_ids=None)` (coarser matching-only key — see
"Inference logic" below). `ilvl_plausible(claimed, base_level)` — `claimed <=
base_level * ILVL_PLAUSIBILITY_MULTIPLE (3) and claimed <= ILVL_ABSOLUTE_MAX
(1000)`, both guards required (ratio catches low-base-level junk, absolute cap
catches high-base-level junk — see history.md for the two real cases, items
237468 and 164353, that motivated each). `BONUS_NOISE_*` constants tuned the
structural noise test formerly run by `snipe_check._detect_noise_bonus_ids()`
— both removed 2026-07-26 along with market_key-based pricing (see "Inference
logic"); the methodology is preserved in `history.md`'s "Bonus-list noise
detection" entry if it's ever needed again.

## `scan_region.py`

Region scanner: sweeps every EU connected realm's *current* listings
(`--exclude` to skip sell realms already deep-collected) into
`data/listings/{cr_id}.parquet`, overwritten each sweep via a temp-file +
`os.replace()` atomic rename (fixed 2026-07-25 after a real production crash —
a reader could open the file mid-write).

Each sweep also records every realm's Blizzard publish time (the response's
`Last-Modified`) into `data/state/sweep_publish.json` — one file write per
sweep, no extra requests, `scan_region.load_publish_state()` to read it.
Diagnostics only; nothing on the pricing path reads it. See
`.claude/docs/architecture.md` for the schema and why it exists (short
version: without it, a sniper-list alert timestamp can't be told apart from
an old listing that merely became newly eligible).

`scan_one()` returns `(row_count, last_modified)` — a malformed body yields
`(0, None)`, never `(0, header)`, since the parquet on disk is still the
*previous* sweep's and recording the new publish would overstate its
freshness.

**Not** yet fed back as `If-Modified-Since`. Doing so would cut the sweep's
bandwidth by roughly 60x at the current 60s cadence (~92 full dumps/minute
today), but a 304 leaves the parquet — and its `fetched_ts` — untouched, so
it changes what lands on disk and is left as its own change.

## `collect_all.py`

The sole collection path (runs in-process inside `dashboard.py`'s background
loop, `ENABLE_BACKGROUND_COLLECTION=true`, Railway only). Deep-collects
FULL/HIGH-population EU realms (`deep_collect_realm_ids()`, cached in-
process), then an unscoped `scan_region.sweep()` (every EU realm's listings).
Runs every ~10 minutes, not hourly (Blizzard publishes at no fixed clock
time). **Only the latest snapshot per realm is kept** (2026-07-25, replacing
the earlier adaptive multi-day retention the same day it shipped — pricing
turned out to only ever read the latest snapshot, so retaining history was
unnecessary complexity): `prune_to_latest(cr)` deletes every snapshot but the
newest immediately after a fresh one lands. `diff_snapshots.py` is no longer
invoked from this loop as a result — see its own row below.
`_prewarm_item_base_levels()` resolves up to `PREWARM_BASE_LEVEL_CAP=1000`
more type-28 item base levels per cycle in the background, regardless of user
traffic, so `snipe_check._resolve_base_levels()`'s cache converges over a few
hours instead of depending on dashboard loads. **Also refreshes `tsm.py`'s
`SaleRateCache`** (added 2026-08-01, human request — see `tsm.py`'s row):
`tsm.SaleRateCache().refresh_if_stale()` runs once per cycle, after the
prewarm block; `refresh_if_stale()`'s own `REFRESH_INTERVAL_SECONDS=6*60*60`
gate means most cycles are a cheap no-op, not a real fetch. Wrapped in its own
`try/except` (never lets a TSM outage break realm collection) and reported in
the cycle's `summary` dict as `tsm_refreshed: bool`. Covered by `tests/test_co
llect_all.py::test_collect_all_refreshes_tsm_sale_rates`/`::test_collect_all_s
urvives_tsm_refresh_failure`. **Also runs `watchlist.check_triggers()` every
cycle** (added 2026-08-02, human request -- see `watchlist.py`'s row): rides
this existing cadence rather than getting its own, same non-decision
as `tsm.py`'s refresh call above. Wrapped in its own `try/except`; reported in
`summary` as `watchlist: dict`.

**Cycle re-ordered and prewarms decoupled 2026-08-05** (human request: "get
the notification as fast as possible"). Two changes, and the order between
them matters:

- `watchlist.check_triggers()` now runs **directly after `scan_region.sweep()`**,
  ahead of both prewarms, instead of dead last. It reads the sweep and nothing
  else, so every Discord alert was previously sitting behind up to 1,500
  sequential item-metadata requests it could not be affected by. Free latency,
  zero extra requests. `tsm.SaleRateCache().refresh_if_stale()` was moved with
  it and deliberately kept **ahead** of the trigger check: the standing rule
  prices candidates against TSM sale averages, so the reverse order would judge
  this cycle's listings against last cycle's averages.
- The two prewarms are now gated by `_prewarm_due()` /
  `PREWARM_MIN_INTERVAL_SECONDS=10*60` (wall clock, `time.monotonic()`,
  process-local) instead of running every cycle. This is what makes the cadence
  safe to change at all: prewarms cost up to 1,500 requests against a cycle's
  ~128 of actual auction work (36 deep realms + ~92 sweep), so before this they
  dominated the bill and `COLLECTION_INTERVAL_SECONDS` was effectively
  load-bearing for the 36,000/h rate limit. Poll cadence and prewarm cost are
  now independent knobs. `summary` gained `prewarm_ran: bool`.

`dashboard.py`'s `COLLECTION_INTERVAL_SECONDS` dropped 10 min -> 60s on the
back of that (~13 min alert latency -> ~1-2 min; ~9,800 req/h -> ~12,600, ~27%
-> ~35% of budget). **The real floor is now `scan_region.sweep()` itself**,
which is a plain sequential `for cr in realms` over ~92 realms — parallelising
it is the next available win and was deliberately left out of this change,
since it touches the collector's never-die guarantee and its parquet writes.
Covered by `tests/test_collect_all.py::test_prewarm_due_*` /
`::test_collect_all_skips_prewarms_inside_the_interval_but_still_notifies`,
plus an autouse `reset_prewarm_clock` fixture — `_last_prewarm_at` is
module-level state that otherwise leaks between tests and silently masks a
skipped prewarm.

## `snipe_check.py`

Joins `data/listings/*.parquet` (every other EU realm's current listings)
against the sell realm's own **current cheapest live listing**, matching on
**`(item_id, pet_species_id, pet_quality_id)`** — bonus/ilvl variance never
gates a match; the buy-side row's `bonus_key` rides along for display only.
`find_snipes()` params: `items`/`min_discount`/`min_gold`/`max_gold`/`min_sell
_now`/`max_appearance_sources`/`max_per_item`/`class_quotas`/`min_value_floor_
g`/`min_sale_rate`/`top`/`sort`. Every `ORDER BY` ends in `auction_id` —
without that final tiebreak DuckDB's parallel plan returns different rows
across identical calls. `check_data_ready(sell)` is the shared "snapshot
exists + listings swept" precondition (CLI and `/api/snipes`). Two post-SQL
Python filters, each attaching its columns unconditionally so callers can
always display them: `_filter_by_appearance()` (`appearance_sources`, excludes
`NON_TRANSMOG_INVENTORY_TYPES`) and `_filter_by_sale_rate()`
(`region_sale_rate`/`region_sold_per_day`/`region_sale_avg_copper` from
`tsm.py`; `None` means TSM has no data, which counts as **failing** a set
threshold, not "unknown, allow"). Either being active widens the SQL pool to
`max(top*20, 1000)`. **Thresholds, all human-specified:**
`MIN_VALUE_FLOOR_G=2000` drops a row only when `sell_p_g` **and**
`region_median_g` are both under it (OR-to-keep, AND-to-drop).
`PRICE_SUSPECT_MULTIPLE=10` sets `price_suspect` when `sell_p_g >= 10 *
region_median_g`. `SNIPER_FILTER_N=5`/`SNIPER_FILTER_CLOSE_MULTIPLE=1.7`/`SNIP
ER_FILTER_MIN_REALMS=3`/`SNIPER_FILTER_HIGH_VALUE_EXEMPT_G=200000` set
`sniper_filter_suspect` when the median of the 5 cheapest *other unique
realms* sits within 1.7x the buy price — needs ≥3 such realms, and never fires
once both `sell_p_g` and `region_median_g` clear 200k gold.
`CLASS_QUOTA_PER_ITEM_CAP=3`/`CLASS_QUOTA_RESOLVE_LIMIT=20000` bound
`class_quotas`, a `{bucket: max_rows}` dict (buckets match `dashboard.html`'s
`ITEM_CLASS_FILTERS`, see `CLASS_BUCKET_RULES`; `mount` needs `item_class==15
AND item_subclass==5`) applied as real SQL window functions over the
**complete** candidate set in a `TEMP TABLE` — a truncated pool was tried and
proven insufficient. `is_sus_item(item_id, inventory_type, base_level)` flags
old jewelry (`LEGACY_JEWELRY_INVENTORY_TYPES`, `LEGACY_JEWELRY_ILVL_MAX=150`)
plus `CURATED_SUS_ITEM_IDS` (class-starter armor, Slithershell, Black Tooth
Grunt sets); it has a known blind spot for twink-valuable items, so it never
filters server-side. **House rule: every flag here marks a row, never drops
it** — hiding is the dashboard checkbox's job alone.
`region_median_g`/`region_median_copper` (median of the per-other-realm
cheapest listings) is informational except where `min_value_floor_g` reads it.
`print_snipes()` prints `CAVEAT` (the equip-before-transfer warning) on every
CLI run. History: see history.md.

## `diff_snapshots.py`

**Core IP**, the snapshot-diff classification engine — **no longer invoked
automatically** (2026-07-25, see `collect_all.py`'s row above: pricing only
ever reads the latest snapshot, so nothing keeps multi-day history around for
this to diff anymore). Still real, still correct, still usable by hand against
manually-accumulated snapshot history (e.g. `fetch_snapshot.py --loop` run
locally, then `python diff_snapshots.py --cr-id X`). `relist_key(r)` — non-
price identity `(item_id, market_key(bonus_key), pet_species_id,
pet_quality_id, quantity)` — **the last remaining live caller of
`fetch_snapshot.market_key()`** (2026-07-26, since `snipe_check.py`'s pricing
join dropped it, see "What this project is" above); relist detection genuinely
needs finer-than-item_id identity (a vanished ilvl-636 listing relisting as
ilvl-636, not as ilvl-40, is what "relisted" means), unlike pricing. Price is
matched separately via `_find_relist_match()` within `RELIST_PRICE_TOLERANCE`
(±15%, added 2026-07-25) instead of requiring exact equality, so a troll
reposting at a nearby joke price still counts as a relist rather than a fake
`inferred_sale`. See "Inference logic" below for the full classification
rules.

## `analyze.py`

DuckDB CLI: liquidity summary + per-item sold-price distribution / percentile
check / per-auction trace (manual debugging only, not on the pricing path).
`connect(cr)` builds `ev`/`sales` from `data/events/{cr}.parquet` if a human
has run `diff_snapshots.py` for that realm; otherwise (the production default
since 2026-07-25) builds an empty `ev` table instead of erroring, so the live
pipeline stays functional with no events file at all. Registers
`market_key(bk, base_level := NULL)` as a DuckDB macro (three helper macros:
`_ilvl28_value`/`_ilvl28_implausible`/`_strip_type`) — the SQL-side mirror of
`fetch_snapshot.market_key()`'s base_level behavior, kept honest by
`tests/test_market_key.py`'s parity check; used by this file's own manual
debugging queries, not by the live pricing path (see "What this project is"
above). `noise_bonus_ids` (a separate, Python-only argument to
`fetch_snapshot.market_key()`, never mirrored in this SQL macro at all) was
previously computed by `snipe_check._detect_noise_bonus_ids()`, removed
2026-07-26 along with the rest of `market_key`-based pricing/matching (see
"Inference logic" below) — nothing currently computes it.

## `appearance.py`

`AppearanceCache`: itemId → transmog-appearance rarity (`source_count` — how
many distinct item ids grant the same appearance region-wide), cached at
`data/appearances.json`, source is wago.tools' `ItemModifiedAppearance` DB2
export (`python appearance.py --refresh`, manual/periodic — not wired into the
Railway background loop, since wago.tools is outside this project's Blizzard
rate-limit budget). Display/filter-only, never-raises. A v1 rarity proxy, not
a real obtainability model.

## `item_names.py`

`NameCache`: display/filter-only, never-raises lookups backed by the static
item/pet API, cached at `data/item_names.json`. `.get()` name, `.icon()`,
`.quality_color()`, `.quality()` (the tier name itself, e.g. `"EPIC"` — added
2026-07-25 alongside `.quality_color()`'s derived ring color, backs the
dashboard's rarity filter), `.base_level()` (still used for display only —
`dashboard._variant_label()`'s "ilvl NNN" check — since matching stopped using
ilvl 2026-07-26, see "What this project is" above), `.inventory_type()`,
`.item_class()`/`.item_subclass()` (Blizzard's official ids, confirmed live
via `GET /data/wow/item-class/index`). `.ensure_many(ids, max_workers, limit)`
resolves concurrently, used by `collect_all._prewarm_item_base_levels()` (its
other original caller, `snipe_check._resolve_base_levels()`, was removed
2026-07-26 along with market_key-based matching). `.ensure_icons_many(ids,
max_workers)` (added 2026-07-26) is the same concurrent-batch pattern for
`.icon()` specifically — `.icon()` isn't covered by `.ensure_many()`'s
`_fetch_item_details` batch at all (icons come from a separate media
endpoint), which was a real gap: `dashboard.py`'s `names=true` row-building
used it item-by-item and hung on any never-before-queried sell realm (see
"Real production outage" below). All fields backfill onto an older cache entry
missing them, self-healing an old cache file instead of returning `None`
forever. **`.has_class_info(item_id)`** (added 2026-07-31, real bug fix found
during a repo-wide audit): whether `item_class`/`item_subclass` are already
resolved, without triggering `.item_class()`/`.item_subclass()`'s own
transparent blocking-fetch fallback for a cache miss. Deliberately narrower
than a full "is this item completely cached" check —
`.name`/`.quality`/`.level` are truthy-only writes (a separate, pre-existing
quirk: a genuinely-empty name means those keys can lag behind forever) while
`item_class`/`item_subclass` are always written unconditionally, so reusing
the strict definition would wrongly deny an item a bucket even when this
call's own resolution had a real answer for it. Used by
`snipe_check._register_class_quota_maps()` (see that row) to avoid
reintroducing the blocking-calls failure mode `ensure_many()`'s own `limit`
param exists to prevent. **`.save()` re-reads the cache file and merges in
only this instance's own newly-resolved keys, rather than overwriting with its
full in-memory snapshot** (2026-08-01, real bug fix, root cause of the "items
randomly jumping" report — worst with a small `class_quotas` bucket like the
free tier's 50-row `recipe` quota, or with `sus_item_suspect` flickering under
"hide flagged"): at least three independent `NameCache()` instances race on
`data/item_names.json` within the same process —
`_register_class_quota_maps()` and `dashboard._build_rows()` each make their
own within a single `/api/snipes` call, plus another on
`collect_all._prewarm_item_base_levels()`'s background loop, running
concurrently with live requests. The old `save()` was a blind `write_text()`
of the whole in-memory `_cache` with no locking and no atomic temp-file+rename
(unlike `scan_region.py`'s sweep writes) — a classic lost-update race:
instance B loads the file before instance A resolves and saves item X, then B
finishes its own unrelated work and saves, overwriting the file with a
snapshot that never saw X, silently reverting it to unresolved. Since
`has_class_info()`/`_class_bucket()` gate bucket membership on presence in the
cache, this directly flipped which items appeared in a class-quota'd bucket
between otherwise-identical requests, and flipped `is_sus_item()`'s
`base_level`/`inventory_type` inputs the same way — with zero change in the
underlying auction data. Fixed by tracking each instance's own writes
separately (`self._pending`, via a new `_set()` helper every write site now
goes through) and having `save()` re-read-and-merge those into the current
file instead of replacing it wholesale. `__init__`'s file read is now wrapped
in `try/except` too (a concurrent non-atomic write could leave the file mid-
write for a reader to catch) — previously an unhandled `json.JSONDecodeError`
there would crash whatever request constructed the instance. Covered by `tests
/test_item_names.py::test_save_does_not_clobber_a_concurrent_instances_write`
(reproduces the exact interleaving) and
`::test_save_tolerates_a_torn_read_of_the_cache_file`.
**`.ensure_many()`/`.ensure_icons_many()` gained a `deadline_seconds` param,
and a module constant `LIVE_RESOLVE_DEADLINE_SECONDS=15`** (added 2026-08-01,
human-specified value, real incident: the same day `blizz.api_get()` gained a
shared rate limiter — see `blizz.py`'s row — these calls stopped failing fast
on a 429 and started *waiting* on the shared budget instead, which turned a
single live `/api/snipes` call's resolution step from its documented 30-175s
baseline to 158-300+ seconds, confirmed live): once elapsed time since the
call started exceeds the deadline, the `as_completed()` loop stops waiting and
returns with whatever resolved so far — same self-healing pattern as
`limit`/`CLASS_QUOTA_RESOLVE_LIMIT`, an item that misses the deadline just
stays unresolved for this one call. `None` (the default) preserves unbounded
behavior, used by every background caller
(`collect_all._prewarm_item_base_levels()` — deliberately never passes a
deadline, nothing is waiting on it). Both live-request call sites —
`dashboard._build_rows()` and `snipe_check._register_class_quota_maps()`
(reached on every real `/api/snipes` call too, since `class_quotas` is never
`None` there) — pass `LIVE_RESOLVE_DEADLINE_SECONDS` explicitly.
Implementation no longer uses `with ThreadPoolExecutor(...) as pool:` — that
context manager's `__exit__` calls `shutdown(wait=True)` unconditionally
regardless of the deadline, which would defeat the whole point; manages the
pool explicitly instead and calls `shutdown(wait=False, cancel_futures=True)`
so queued-but-unstarted work is dropped rather than waited on (already-in-
flight fetches, up to `max_workers` of them, keep running in the background
but their results are no longer collected — bounded wasted work, not a
correctness issue). Covered by `tests/test_item_names.py::test_ensure_many_dea
dline_seconds_does_not_wait_for_slow_items`/`::test_ensure_icons_many_deadline
_seconds_does_not_wait_for_slow_items` (a stub blocked on a `threading.Event`
proves the call doesn't hang) and `tests/test_dashboard.py::test_api_snipes_pa
sses_a_real_resolve_deadline_at_both_call_sites`.

## `tsm.py`

**New (2026-08-01)**, human request — "a filter, where the user can set a
minimum sellrate," confirmed against TSM's own real region-wide file schema
before building (`saleRate`/`soldPerDay` exist only on TSM's **region**-wide
public CSVs, `region/items.csv`, not the per-realm `realm/{slug}/items.csv`
ones — confirmed live via `tsm._fetch_csv()` against a real 30,984-row
response). `SaleRateCache`: same `AppearanceCache`/`NameCache` display/filter-
only, never-raises convention, cached at `data/tsm_sale_rates.json`.
`_fetch_csv()` hits `REGION_ITEMS_URL` (`https://public-
data.tradeskillmaster.com/retail/eu/region/items.csv`, no auth — TSM's Public
Data API is free/unauthenticated) and returns `{}` on any non-200 or
exception, never raises.
`refresh_if_stale(interval_seconds=REFRESH_INTERVAL_SECONDS)` (`6*60*60` —
TSM's own region files update roughly daily, so 6h is a conservative poll, not
a real-time need) only re-fetches when the cache is empty or older than the
interval, and **keeps the old data on a failed fetch** rather than clearing it
— a transient TSM outage degrades to "slightly stale sale rates," never to "no
sale rates at all." `.get(item_id)` returns `{"sale_rate": float,
"sold_per_day": float, "avg_sale_price": float}` or `None` — `avg_sale_price`
(added 2026-08-03, human request, "region sale avg from tsm") is TSM's
`avgSalePrice` column, already in copper (confirmed live via `_fetch_csv()` —
item 2624/Thinking Cap: `avgSalePrice` 28,500,000 = 2850g, in line with its
six-figure `marketValue`), same region-wide-only scope as
`saleRate`/`soldPerDay` since it's the same file/row. **Single-writer design,
deliberately** (same reasoning as `item_names.py`'s 2026-08-01 lost-update-
race fix, applied from the start rather than found live a second time): only
`collect_all.py`'s background loop ever calls `refresh_if_stale()`/writes the
cache file; every live-request read (`snipe_check._filter_by_sale_rate()`)
only ever calls `.get()`, never triggers a fetch or a write — so there's no
concurrent-writer race to have in the first place, unlike `NameCache`'s per-
request-instance pattern. No `--refresh` CLI flag by design, for the same
reason `appearance.py --refresh` exists but this doesn't: `appearance.py`'s
wago.tools source is outside this project's Blizzard rate-limit budget and
needs a human to run it manually/periodically, while TSM's feed is free, low-
volume (one HTTP GET), and already fully automated via `collect_all.py` — a
manual trigger would just race the same file the background loop owns. TSM's
Public Data API docs (`support.tradeskillmaster.com`) and ToS
(`tradeskillmaster.com/terms`, confirmed live via a real browser session after
`WebFetch` was blocked with a 403) explicitly invite third-party tools to pull
this feed; no hard ToS blocker found, though the docs specifically invite
heavier/programmatic users to reach out first (`admin@tradeskillmaster.com`) —
not yet done, worth doing before this is under heavier real production load.
Covered by `tests/test_tsm.py` (fetch parsing/errors, cache
get/refresh/staleness/persistence/corruption-tolerance).

## `tsm_import.py`

**New (2026-08-02)**, backing Watchlist's "import a TSM group instead of
adding items one at a time" (see `watchlist.py`'s row,
`feature-watchlist.md`). Decodes a TSM4 group-export string -- confirmed live
(not guessed) against TSM's own real, unmodified addon source
(`Core/Service/Groups/ImportExport.lua`'s `GenerateExport`/`DecodeNewImport`)
to be `LibSerialize:SerializeEx(...)` piped through
`LibDeflate:CompressDeflate()` then `:EncodeForPrint()` -- a 64-char alphabet
(`a-z A-Z 0-9 ( )`), not the classic `^`-prefixed AceSerializer format some
older TSM strings use (AceSerializer is bundled by TSM only for backward-
compat decoding of those, confirmed unused for new exports). Rather than hand-
porting that bit-packed binary format to Python from documentation (real risk
of a wrong bit offset silently producing wrong item ids, not an error), this
runs the two real Lua files TSM itself ships
(`vendor/tsm_lua/LibDeflate.lua`/`LibSerialize.lua`, byte-identical to TSM's
bundled copies -- confirmed via git-blob-SHA comparison against the real TSM
addon source, fetched by a research subagent -- LibSerialize is pinned at
TSM's `MINOR=1`, not current upstream, which has since moved to a different
wire format) through a real embedded Lua interpreter, the `lupa` package (new
dependency, human-approved after an explicit tradeoff discussion against a
pure-Python reimplementation -- see `requirements.txt`'s row). `unpack` is
injected as a global before loading `LibSerialize.lua` -- WoW's Lua 5.1 has it
as a global, modern Lua (what `lupa` embeds) only has `table.unpack`; the one
shim needed, confirmed by grepping both vendored files for other WoW-only
globals before writing this module. A single module-level Lua runtime is
lazily created and guarded by a lock (not safe for concurrent calls from
multiple threads, and recreating the interpreter + reloading both library
files per call is needless overhead for a low-frequency interactive action).
`decode_group_export(export_str) -> TsmGroupExport` raises `TsmImportError`
(not a raw `lupa.LuaError`) on anything that isn't a valid current-format
export -- confirmed live that malformed/garbage input (including valid-
alphabet-but-structurally-invalid strings) fails cleanly inside `LibDeflate`'s
own `DecodeForPrint`/`DecompressDeflate` (returns `nil`, not a Lua-level
error) rather than crashing. Group-path format, confirmed live against a real
300-item/112-subpath sample the human pasted during development: `items` is a
Lua table mapping TSM itemString (`i:<itemId>...`) to the item's sub-group
path *relative to the exported group*, backtick-joined
(`TSM.CONST.GROUP_SEP`), covering both the `group/items` and
`group/subcategory/items` shapes the human flagged as a real case -- re-joined
with `/` here for display, matching how the rest of this project presents
paths. **Known limitation**: only `i:` itemStrings are parsed -- TSM's caged-
pet itemString format (`p:...`) wasn't present in the one real sample
available while building this, and rather than guess at its shape, pet entries
in an imported group are silently skipped (not crashed on). Covered by
`tests/test_tsm_import.py`, which uses the exact real sample string as its
test vector (not synthetic) -- metadata (group name, 300-item count), item-id
parsing, nested-subgroup-path handling, and error cases all re-derived through
the real module.
