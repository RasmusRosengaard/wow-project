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
--   r = reference_copper  -- cross-realm reference price, COPPER.
--                            Which baseline this is (sell realm's current
--                            cheapest vs region_median_cheapest) is open
--                            question 3 in the spec -- the exporter decides,
--                            the addon just compares.
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
        [152510] = { s = 1, r = 2500000 },   -- used as the worked example in CLAUDE.md
        [168487] = { s = 1, r = 180000 },
        [122361] = { s = 4, r = 95000 },     -- shared appearance; should be filtered out
    },
}
