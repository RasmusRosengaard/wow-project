# Feature idea: Watchlist (TSM group import, cross-realm item tracking)

**Status: shipped 2026-08-02.** This file is kept as the original design
sketch/rationale — see `CLAUDE.md`'s `watchlist.py`/`tsm_import.py`/
`static/watchlist.html` rows for what actually got built and how it
resolved the open questions below, and `progress.md`/`history.md` for the
build trace. Left in place rather than deleted since the "why" reasoning
here (especially the TSM-format and delivery-mechanism tradeoffs) isn't
duplicated anywhere else.

## The idea (as pitched, 2026-07-31)

A user pastes in a **TSM (TradeSkillMaster) group export** — a list of
specific items they care about, e.g. their personal "high-tier items I'd
buy on sight" list — and the tool tracks those exact items across every EU
realm, independent of any one sell realm. When one of those items shows up
priced very cheap *somewhere* in the region, the user gets told.

## How this differs from the existing snipe-check flow

The whole product today is built around **one reference price**: the sell
realm's own current cheapest listing (see `CLAUDE.md`'s "What this project
is"). Every snipe is "this other realm's price vs. *my* realm's price."

Watchlist has **no sell realm at all** — the user isn't asking "what's
cheap relative to where I sell," they're asking "is this specific item
cheap anywhere, period." That means the reference price can't be a sell
realm's listing; it has to be some notion of the item's *typical* price
across the whole region.

## What already exists that this could reuse

- **`snipe_check.py`'s `region_stats` CTE already computes exactly this**:
  `region_median_cheapest`, the median of every other realm's own cheapest
  listing for an item — already shown to users today as the informational
  "EU median" column. This is the natural baseline for "is this cheap,"
  with no new data pipeline needed.
- **`scan_region.py`'s region-wide sweep** already has every EU realm's
  current listings, refreshed on the existing ~10-minute cadence, with no
  sell-realm dependency — Watchlist's data need (buy-side listings
  region-wide) is a strict subset of what's already collected.
- **`item_names.py`'s `NameCache`** already resolves names/icons/item
  class for display.

In other words: the *data* this needs mostly already exists. What's
missing is (1) a per-user list of watched items instead of one global
sell-realm query, and (2) a notion of "notify me," which the product has
never had before (today everything is pull — load the dashboard, see
what's there).

## Rough shape (not a spec)

- **Watched item storage**: a new small table, `db.WatchlistItem` or
  similar — `user_id`, `item_id`, maybe `pet_species_id`/`pet_quality_id`
  for pets, added/removed by the user. Matching key would presumably mirror
  the current product's `(item_id, pet_species_id, pet_quality_id)`
  decision (2026-07-26) rather than reopening the bonus/ilvl-matching
  question — though "high-tier items" specifically might be exactly the
  case where a user *does* care about a specific ilvl variant, which cuts
  against that. Worth deciding deliberately, not by default.
- **TSM group import**: parse a pasted TSM export string into a list of
  item ids. Exact format not nailed down here — TSM exports item lists as
  strings like `i:168487` per item (with bonus-id suffixes for variants),
  sometimes with a group-path prefix. Needs a real example export to parse
  against before this is buildable, not just documentation.
- **"Very cheap" detection**: likely a discount vs. `region_median_cheapest`
  (already computed), past some threshold — mirrors the existing
  `min_discount` concept, just with the median as the baseline instead of
  a sell realm's price. Whether the threshold is fixed, human-tuned (like
  every other threshold in this product so far), or user-configurable per
  item is an open question.
- **Notification delivery**: this is the part with no existing precedent
  in the product at all. Options, not decided:
  - In-app only: a badge/highlight when the user loads a dedicated
    Watchlist page — cheapest, but only useful if they check it.
  - Hook into **Phase 4 of the existing roadmap** ("deal score + Discord
    alerts," currently not started, blocked on Phase 3 appearance data) —
    Watchlist and Phase 4's alerting need the same delivery mechanism, so
    building them together (or at least designing the alert pipe once for
    both) is probably more sensible than building two separate notification
    paths.

## Explicit non-goals (for now)

- Not replacing or changing the existing sell-realm snipe flow.
- Not building real-time push/email/Discord delivery as part of this idea
  — that's a shared dependency with Phase 4, not something to build twice.
- Not deciding the exact TSM parse format here — needs a real sample.
- Not deciding pricing/tier gating (free vs. paid feature) here.

## Open questions to resolve before this becomes a real plan

**All resolved 2026-08-02, human decisions — see `CLAUDE.md` for the
implementation each one led to:**

1. Exact TSM group export format to parse (get a real sample first). —
   **Resolved**: LibSerialize (TSM's pinned MINOR=1) + LibDeflate's
   `EncodeForPrint`, confirmed against TSM's real addon source and decoded
   via a vendored Lua runtime (`tsm_import.py`), not a hand-ported
   reimplementation.
2. Discount threshold: fixed constant, human-tuned per launch, or
   user-configurable per watched item? — **Resolved, and reframed**: no
   discount/auto-price logic at all. A plain user-set absolute gold price
   per item, explicit human call ("we only want to trigger for whatever
   price the user wants").
3. Does matching need ilvl/bonus awareness for "high-tier" items? —
   **Resolved: no.** item_id-only (+ pet_species_id), matching the rest of
   the product's 2026-07-26 decision rather than reopening it.
4. Notification delivery mechanism — build alongside Phase 4, or a cheaper
   in-app-only v1 first? — **Resolved**: built now, as a per-user Discord
   webhook URL (no OAuth) — real delivery, not an in-app-only placeholder,
   since a watchlist a user never checks back on defeats the point.
5. Free tier or paid-only? How many watched items does a tier get? —
   **Resolved**: subscribers only (matches the placeholder page's existing
   "premium-only" badge), 500 items/user (a UX cap, not human-tuned like
   most thresholds in this product — worth revisiting with real usage).
6. Does this get its own scan cadence, or ride the existing ~10-minute
   region sweep as-is? — **Resolved: rides the existing cycle**, no new
   cadence.

## Relationship to the existing roadmap

Distinct from Phase 2 (commodities feed), which is explicitly out of
scope elsewhere in this project — Watchlist is about equipment/tracked
items, not the commodities market. Overlaps most with **Phase 4** (deal
score + Discord alerts) on the notification-delivery question above.
