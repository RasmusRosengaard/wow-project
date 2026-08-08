# RealmArbSniper (experimental)

Skeleton for the Phase 5 in-game addon. Design and rationale live in
`.claude/docs/feature-ingame-sniper.md` — read that first.

**Status: the scan loop has never run in a WoW client.** Treat `Scan.lua`,
`Core.lua` and the `.toc` as a structural first draft.

`Rules.lua` is the exception: it is pure, and `tests/test_addon_rules.py`
executes it in a real embedded Lua interpreter (`lupa`) against the same
Sniper-filter vectors `tests/test_snipe_check.py` uses.

## Install for development

Symlink or copy `RealmArbSniper/` into:

```
<WoW>/_retail_/Interface/AddOns/RealmArbSniper/
```

Enable "Load out of date AddOns" (the `## Interface` number in the `.toc`
is a placeholder). Open an auction house, then:

```
/realmarb start      # begin the browse-diff loop
/realmarb status     # state, thresholds, remaining search budget
/realmarb pct 40     # flag at <= 40% of reference price
/realmarb max 5000   # absolute ceiling, in gold
/realmarb sources 1  # appearances with <= N source items
/realmarb sniper     # toggle the Sniper filter (ON by default)
/realmarb stop
```

The **Sniper filter** is on by default and behaves like the Discord path,
not the dashboard: a hard reject rather than an opt-in "hide", and it does
not honor `SNIPER_FILTER_HIGH_VALUE_EXEMPT_G`. Its thresholds come from
`Data.lua`'s `sniperFilter` block, written straight from
`snipe_check.SNIPER_FILTER_*` — never re-declare them in Lua.

## Layout

## Generating Data.lua

```
python appearance.py --refresh          # first; the cache is manual by design
python export_addon_data.py --sell 1403 --min-value-g 1000
python export_addon_data.py --sell 1403 --stats-only   # counts, writes nothing
```

Real run against 91 realm sweeps + snapshots for 1403:

```
priced items region-wide : 18699
  no appearance data     : -4013
  shared appearance      : -11861
  below value floor      : -749
  profession slot        : -10
exported                 : 2066      -> 94 KB
```

94 KB is small enough that the paste-in import string stays viable and no
companion app is needed for v1.

**The committed `Data.lua` is the three-row development stub.** Running the
exporter overwrites it locally — that's intended, but don't commit the
result: it's derived data, and it would put realm prices in git history.

| File | Role |
|---|---|
| `Data.lua` | **Generated** by `export_addon_data.py`. `itemID -> {s, r, m}`. Committed copy is a three-row stub. |
| `Rules.lua` | Pure filter evaluation. No API calls, no state. |
| `Scan.lua` | The browse-diff poll loop and search-query budget. |
| `Core.lua` | Lifecycle, saved vars, events, slash commands, output. |

Prices are **copper** everywhere except `Core.FormatMoney`, matching the
rest of the project.

## Verified before writing

- `ReplicateItems()` — 15-min account-wide throttle, so unusable for sniping.
- `SendSearchQuery()` — 100 calls/minute cap. `Scan.lua` rations to 80.
- `SendBrowseQuery()` — returns aggregated summaries in groups of 500,
  paged with `HasFullBrowseResults()` / `RequestMoreBrowseResults()`.
- `IsThrottledMessageSystemReady()` — guard before every query.
- `PlayerCanCollectSource()` is account-wide, not per-character (unused in
  v1, relevant only if the optional collection filter gets built).

## NOT verified — check these first in a live client

Everything below is written from documentation, not observation:

1. **`AuctionHouseBrowseQuery.quality`** — field name and whether quality
   filtering belongs there or in `filters`. Most likely thing to be wrong.
2. **`BrowseResultInfo` fields** — `minPrice`, `totalQuantity`, `itemKey`.
3. **`ITEM_SEARCH_RESULTS_UPDATED` payload** — `Core.lua` assumes `arg1` is
   the itemKey.
4. **`GetNumItemSearchResults` / `GetItemSearchResultInfo`** signatures.
5. **An empty `searchString` browse query returns the whole AH**, which is
   many 500-row pages per poll. This is the main performance unknown —
   narrowing by `itemClassFilters` (armor/weapons only) is probably
   required, not optional. Measure before tuning `BROWSE_INTERVAL`.
6. **`## Interface`** — set from `/dump select(4, GetBuildInfo())`.

## Deliberate v1 omissions

- **Commodities.** `COMMODITY_SEARCH_RESULTS_UPDATED` and the commodity
  result API are unhandled. Transmog gear is never a commodity, so this
  costs nothing for the intended use — but it means the addon is silent on
  the commodity market by design, not by accident.
- **No UI.** Hits print to chat with a sound. A results frame comes later.
- **No TSM dependency.** `Rules.TSMValue` exists as an unused seam. Spec
  open question 1 is still open.
- **No purchasing, ever.** No `PlaceBid`, no `StartCommoditiesPurchase`, no
  input simulation. The addon flags; the human clicks. This is a product
  guardrail from `CLAUDE.md`, not an implementation gap to close later.
