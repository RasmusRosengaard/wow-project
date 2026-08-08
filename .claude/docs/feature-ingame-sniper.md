# Feature idea: in-game sniper addon (Phase 5)

**Status: sketch, 2026-08-08.** Experimental — branch
`experiment/ingame-sniper-addon`. Nothing built yet. This is the design
record for the addon half of Phase 5 (`roadmap.md` line 45; the web
dashboard half already shipped).

## The idea (as pitched, 2026-08-08)

**Point Blank Sniper's shape, with our filters.**

PBS scans exactly one realm — the one the player is currently logged into
— live, and flags listings against a price rule. We want the same scan
scope, unchanged. What changes is *what the rule can express*:

1. **Unique transmog** — is this appearance *sole-source*, i.e. not
   shared with other items? (Not "have I collected it" — see below.)
2. **Cross-realm reference price** — is this cheap relative to what our
   dashboard already knows the item is worth on my sell realm / across EU?
3. **Percentage thresholds** against either, the way PBS does against
   TSM's `dbregionsaleavg`.

The addon **does not** scan other realms and does not need to. The
cross-realm work already happened server-side; the addon consumes the
answer.

## Why PBS can't already do this

PBS has no price data of its own. It [requires TSM (and Auctionator)
installed](https://gaminghero.io/point-blank-sniper-addon-beginner-guide/)
and reads values through
[`TSM_API.GetCustomPriceValue(customPriceStr, itemString)`](https://api.tradeskillmaster.com/addon/).
Its "five price sources" are just whatever other addons are present. So:

- **Expressions** are already solved by TSM's custom-price DSL
  (`max(dbregionsaleavg*0.3, 1000g)`, `first(...)`, nesting). Cheap to
  inherit via TSM_API, expensive and pointless to reimplement.
- **Sources** are where we win. `dbregionsaleavg` is one coarse
  region-wide number. TSM has no per-realm cross-realm view at all, so
  "≤ 40% of my hub's *current cheapest listing*" is not expressible in
  TSM, by construction. That filter is ours alone.
- **Transmog uniqueness** (`appearance.py`'s `source_count`) has no TSM
  or PBS equivalent at all — they price items, they don't model looks.

## Verified client API facts

Checked 2026-08-08 against Warcraft Wiki / Wowpedia — trust these, don't
re-derive:

| Call | Fact |
|---|---|
| [`C_AuctionHouse.ReplicateItems()`](https://wowpedia.fandom.com/wiki/API_C_AuctionHouse.ReplicateItems) | Full-AH dump. **15-minute account-wide throttle.** Not a sniper path. |
| [`C_AuctionHouse.SendSearchQuery()`](https://warcraft.wiki.gg/wiki/API_C_AuctionHouse.SendSearchQuery) | Per-item listings. **Capped at 100 calls/minute.** The scarce resource. |
| [`C_AuctionHouse.SendBrowseQuery()`](https://warcraft.wiki.gg/wiki/API_C_AuctionHouse.SendBrowseQuery) | Returns *aggregated summaries* — one row per item key (min price, total quantity), in groups of 500. Paged via `HasFullBrowseResults()` / `RequestMoreBrowseResults()`. |
| `C_AuctionHouse.IsThrottledMessageSystemReady()` | Check before querying. |

**There is no "live full AH mirror" and no new-auction event.** "Instant
new post" detection is a *diff*, structurally the same thing
`diff_snapshots.py` does server-side:

```
poll SendBrowseQuery (narrowed: quality + item class + level)
  -> compare summary rows against previous poll
  -> min price dropped OR quantity rose = something new was posted
  -> spend one of the 100/min SendSearchQuery calls on ONLY that item key
  -> apply rule -> highlight + sound
```

The browse-diff is the cheap prefilter that protects the search budget.
That constraint, not filter complexity, is what shapes the addon.

## Transmog uniqueness — the headline filter

**"Unique appearance" means the look is not shared with other items.** It
does *not* mean "the player hasn't collected it" (human clarification,
2026-08-08). An item whose appearance is also granted by a dozen cheaper
items has no transmog value; a sole-source look does. Collection status
is a different, secondary question — see below.

**We already compute this.** `appearance.py` builds
`data/appearances.json` as `item_id -> {appearance_id, source_count}`,
where `source_count` is how many distinct ItemIDs grant that
ItemAppearanceID. **`source_count == 1` is the filter.** Backed by
wago.tools' `ItemModifiedAppearance` DB2 export — there is no Blizzard
endpoint for this.

Consequences, and they're good ones:

- **No client API needed.** This is static game data, not per-account
  state. It ships as *one extra int per item* in the data table —
  negligible size.
- **Same number everywhere.** Discord alerts, dashboard and addon can all
  filter on `source_count` from one server-side source, so the rule
  cannot drift between surfaces (see Non-goals).
- **TSM/PBS have no equivalent.** Combined with cross-realm reference
  prices, this is the differentiated filter set.

Carry `appearance.py`'s own stated caveats — don't oversell the number:

- It's a v1 **rarity proxy, not an obtainability model**: no drop rate,
  no still-farmable check, no BoE vs quest-locked distinction.
- It doesn't perfectly dedupe modifier variants of the *same* gearing
  method (warforged recolors etc.), so `source_count` can overcount for
  recolor families.
- The cache is **manual-refresh only** and deliberately never wired into
  the Railway collection loop (wago.tools sits outside the Blizzard
  rate-limit budget). Weekly is plenty since DB2 dumps only move on
  content patches — but if the addon leans on `source_count`, that manual
  step becomes load-bearing. See open question 6.

### Optional secondary filter: collection status

Only *this* part is client-only — no external service can know what a
given account has collected.

| Call | Use |
|---|---|
| `C_TransmogCollection.GetItemInfo(itemID)` | itemID -> appearanceID / sourceID |
| `PlayerHasTransmogItemModifiedAppearance(id)` | already collected? |
| `PlayerKnowsSource(sourceID)` | known source |
| `PlayerCanCollectSource(sourceID)` | collectable at all |

Useful as an "and I don't own it yet" toggle for players buying for
themselves rather than to resell. Not the headline filter.

**Gotcha if we do build it:** [`PlayerCanCollectSource`](https://wowpedia.fandom.com/wiki/API_C_TransmogCollection.PlayerCanCollectSource)
returns collectable if *any* character on the account could learn it — it
does **not** answer "can this character use it." [CanIMogIt](https://github.com/TorelTwiddler/CanIMogIt)
works around this by caching player class + armor type and checking
manually; read its `code.lua` `CharacterCanLearnTransmog` before
reimplementing.

## Getting our prices into the client

**No HTTP in the addon sandbox** — no sockets, no fetch. The reference
table has to arrive as data loaded at login/`/reload`:

- **Companion app writes a Lua file** (the TSM Desktop App /
  `AppData.lua` pattern). Needed for a full price table.
- **Paste-in import string** — `tsm_import.py` already vendors a Lua
  runtime with LibSerialize + LibDeflate for the *decode* direction; the
  encode direction (`Serialize` -> `CompressDeflate` -> `EncodeForPrint`)
  gives a dashboard-generated import string with no companion app.
  Fine for a watchlist-sized payload, too small for a full region table.

Refresh cadence is `/reload` (~10s), which is fine against the existing
~10-minute region sweep.

**Table size is the real constraint, and `source_count` is what solves
it.** The uniqueness filter prunes the item universe *before* the table
is built: a transmog-focused v1 only needs rows for sole-source-appearance
items, not every item on the AH. Ship `{item_id -> source_count,
reference_price}` for that subset and the payload is small enough for the
paste-in import string, no companion app required.

Further trimming levers if the full table is ever wanted: value floor,
quantized prices, only items actually seen listed. Our `(item_id,
pet_species_id, pet_quality_id)` matching (2026-07-26) already means one
row per item rather than per bonus variant.

## Guardrails (non-negotiable, from `CLAUDE.md`)

- **No automated clicks.** Purchases stay behind a hardware event
  (keypress/click) — which the client enforces anyway, but this is a
  product rule first. Decision support only.
- **The addon ships free.** Monetization stays in the external service.
- Re-read the Blizzard Developer API Terms before anything here couples
  to a paid tier.

## Open questions — human decisions, not to be defaulted

1. **TSM dependency: hard, opportunistic, or none?** Reading TSM_API buys
   the whole custom-price expression language for free and instant PBS
   parity — but makes us a plugin to the competitor `CLAUDE.md` positions
   us against, and forces users to run TSM's desktop app. Standalone is
   clean but means writing a rule evaluator. *Recommendation (not
   decided): opportunistic — use TSM_API if present, ship our
   cross-realm source as the headline filter that works either way.*
2. **Delivery**: companion app vs. import string for v1.
3. **Which reference price is the default rule baseline** — sell realm's
   current cheapest, or `region_median_cheapest`? Both exist in
   `snipe_check.py`. **Mechanism built, calibration still open**
   (2026-08-08): `export_addon_data.py` ships *both* per item (`r` and
   `m`), and `Rules.useRegionFallback` switches between them, defaulting
   **off** (sell realm only). Deciding input: sole-source transmog items
   are often unlisted on any single realm, so leaving the fallback off
   caps what the addon can find at the sell realm's coverage. In the
   first real export (cr 1403, 2066 items), **118 items — 5.7% — had no
   sell-realm listing** and are invisible with the fallback off. Only
   **1** item lacked a region median, so `m` has near-total coverage.
   So the fallback buys ~6% more reachable items, not a transformative
   amount — which is an argument for leaving it off and keeping the
   baseline identical to the dashboard's.
4. **Threshold defaults.** Per `CLAUDE.md`, pricing thresholds are
   human-specified — propose and wait, don't pick.
5. **Tier gating**: is the addon's data feed free-tier or subscriber-only?
6. **Does `appearance.py` need automating?** It's manual-refresh by
   deliberate design (third-party site, outside the Blizzard rate
   budget). If `source_count` becomes the addon's headline filter, a
   stale cache silently degrades the product's main selling point. Either
   accept the manual step with a staleness warning surfaced somewhere, or
   revisit the "never in the collection loop" decision — the latter is a
   change to an existing deliberate call, so human's choice.
7. **Is `source_count == 1` the right cut, or a threshold?** `<= 2` or
   `<= 3` may be the better rule given the known recolor-family
   overcounting. Per `CLAUDE.md` this is a threshold — propose and wait.

## Non-goals

- Not scanning other realms client-side. Ever — the API can't, and the
  server already did it.
- Not automating any in-game action.
- Not replacing the dashboard or Discord alerts. Discord says *which
  realm to log into*; the addon makes the buy fast once you're there.
  Rules should be defined once server-side and exported alongside the
  price table, so the two can't drift.
