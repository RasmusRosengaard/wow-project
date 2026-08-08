-- Data.lua -- GENERATED FILE, DO NOT EDIT BY HAND.
--
-- Produced server-side from the existing pipeline and shipped to the client.
-- There is no HTTP in the addon sandbox, so this arrives either as a
-- regenerated copy of this file or via a paste-in import string, and is
-- re-read on login / "/reload". See .claude/docs/feature-ingame-sniper.md.
--
-- Schema, per item:
--   s = source_count      -- how many distinct ItemIDs grant this appearance
--                            (appearance.py / data/appearances.json).
--                            1 == sole-source == "unique appearance".
--   r = sell realm's own current cheapest listing, COPPER. Absent when the
--       sell realm has nothing listed for the item.
--   m = region_median_cheapest, COPPER -- the median of every other EU
--       realm's own cheapest listing. Absent when nothing is listed anywhere.
--
-- Both ship because sole-source transmog items are frequently unlisted on any
-- one realm. Rules.lua uses `r` and only consults `m` if useRegionFallback is
-- turned on -- spec open question 3.
--
-- PRICES ARE COPPER, end to end, matching the rest of the project. Format as
-- gold only at the display boundary (see Core.FormatMoney).

local ADDON, ns = ...

ns.Data = {
    -- Bumped by the exporter whenever the schema changes; Core warns on mismatch.
    schema = 1,

    -- Unix timestamp of generation. Core warns when this is old, because a
    -- stale appearance cache silently degrades the headline filter.
    -- (appearance.py is manual-refresh by design -- spec open question 6.)
    generated = 0,

    -- Which reference baseline the exporter used, for display honesty.
    baseline = "unset",

    -- [itemID] = { s = source_count, r = reference_copper }
    -- STUB DATA -- three real item IDs with invented numbers, purely so the
    -- scan loop has something to match against during development. Replace
    -- wholesale with exporter output; do not hand-maintain.
    items = {
        [152510] = { s = 1, r = 2500000, m = 2100000 },  -- CLAUDE.md's worked example
        [168487] = { s = 1, m = 180000 },                -- unlisted on the sell realm
        [122361] = { s = 4, r = 95000, m = 90000 },      -- shared look; must be filtered out
    },
}
