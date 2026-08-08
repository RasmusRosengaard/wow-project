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
--   c = Sniper filter cluster median, COPPER -- the median of the N cheapest
--       OTHER realms' own floors for this item.
--   cn = how many realms were in that cluster.
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

    -- Sniper-filter thresholds. The exporter writes these straight from
    -- snipe_check.SNIPER_FILTER_* so the addon can never drift from the
    -- backend -- watchlist.py imports the same constants for the same
    -- reason. Never hand-edit; never re-declare them in Rules.lua.
    sniperFilter = {
        n             = 5,
        closeMultiple = 1.7,
        minRealms     = 3,
    },

    -- [itemID] = { s = source_count, r = reference_copper }
    -- STUB DATA -- three real item IDs with invented numbers, purely so the
    -- scan loop has something to match against during development. Replace
    -- wholesale with exporter output; do not hand-maintain.
    items = {
        -- CLAUDE.md's worked example. Isolated cheap listing: the cluster
        -- sits far above any plausible buy price, so the Sniper filter
        -- leaves it alone.
        [152510] = { s = 1, r = 2500000, m = 2100000, c = 2000000, cn = 5 },
        -- Unlisted on the sell realm; only reachable with useRegionFallback.
        [168487] = { s = 1, m = 180000, c = 175000, cn = 4 },
        -- Shared look -- dropped by the appearance gate before anything else.
        [122361] = { s = 4, r = 95000, m = 90000, c = 88000, cn = 5 },
        -- Sole-source and cheap, but 5 other realms cluster right on top of
        -- the price: exactly what the Sniper filter exists to reject.
        [130000] = { s = 1, r = 400000, m = 420000, c = 444000, cn = 5 },
    },
}
