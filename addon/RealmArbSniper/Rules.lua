-- Rules.lua -- filter evaluation. Deliberately pure: no API calls, no frames,
-- no state. Everything it needs is passed in, so the rules can be reasoned
-- about (and eventually tested) without a running client, mirroring the
-- project's "pure functions where possible" convention.
--
-- PRICES ARE COPPER throughout.

local ADDON, ns = ...

local Rules = {}
ns.Rules = Rules

-- Defaults are intentionally conservative placeholders, NOT calibrated.
-- Per CLAUDE.md, pricing/matching thresholds are human-specified -- these
-- exist so the addon runs during development, not as recommended values.
-- maxSourceCount especially: see spec open question 7 (== 1 vs <= 2/3, given
-- appearance.py's known recolor-family overcounting).
Rules.defaults = {
    enabled          = true,
    maxSourceCount   = 1,        -- 1 = sole-source appearances only
    maxPriceCopper   = 50000000, -- absolute ceiling, 5000g
    pctOfReference   = 0.40,     -- flag at <= 40% of the reference price
    requireReference = true,     -- skip items with no reference price at all
    minQuality       = 2,        -- Uncommon+. Enum.ItemQuality.Uncommon

    -- Fall back to the region median (entry.m) when the sell realm has no
    -- listing for the item (entry.r is nil). OFF by default: the exporter
    -- ships both numbers so the switch exists, but which baseline the product
    -- flags on is a human call (spec open question 3), not a default for the
    -- addon to quietly assume. Worth knowing before deciding: sole-source
    -- transmog items are frequently unlisted on any one realm, so leaving
    -- this off means the sell realm's coverage caps what the addon can find.
    useRegionFallback = false,
}

-- Blizzard takes a 5% cut on sale. Net proceeds, not gross, are what a
-- resale is actually worth -- same 0.95 factor snipe_check.py applies.
local AH_CUT = 0.05

function Rules.NetProceeds(copper)
    return copper * (1 - AH_CUT)
end

-- Evaluate one candidate listing.
--
--   itemID     number
--   unitPrice  number, COPPER, per-unit (never the stack total)
--   entry      ns.Data.items[itemID], or nil if the item isn't in the table
--   cfg        a table shaped like Rules.defaults
--
-- Returns: matched (boolean), info (table) -- info carries the numbers the
-- UI wants to show, and on a rejection, `reason` says which gate stopped it.
-- Rejections are returned rather than swallowed so the debug view can explain
-- *why* something wasn't flagged, which is most of what makes a sniper
-- trustworthy to its user.
function Rules.Evaluate(itemID, unitPrice, entry, cfg)
    cfg = cfg or Rules.defaults

    local info = { itemID = itemID, unitPrice = unitPrice }

    if not cfg.enabled then
        return false, { reason = "disabled" }
    end
    if not unitPrice or unitPrice <= 0 then
        return false, { reason = "no-buyout" }   -- bid-only listing
    end

    if not entry then
        -- Unknown item: no appearance data and no reference price. Never
        -- flag on absence of evidence -- that would spam the user with
        -- everything the exporter happened to omit.
        return false, { reason = "not-in-table" }
    end

    info.sourceCount = entry.s

    -- Which reference this item gets judged against. entry.r is the sell
    -- realm's own current cheapest listing (the product's baseline); entry.m
    -- is the region median of per-realm cheapest listings.
    local reference, baseline = entry.r, "sell_realm"
    if not reference and cfg.useRegionFallback then
        reference, baseline = entry.m, "region_median"
    end
    info.reference = reference
    info.baseline  = baseline

    -- 1. Unique appearance. THE headline filter: "unique" means the look is
    --    sole-source, not that the player hasn't collected it.
    if not entry.s or entry.s > cfg.maxSourceCount then
        return false, { reason = "shared-appearance", sourceCount = entry.s }
    end

    -- 2. Absolute ceiling. A hard "never show me anything above this" cap,
    --    independent of any percentage.
    if unitPrice > cfg.maxPriceCopper then
        return false, { reason = "over-max-price" }
    end

    -- 3. Cross-realm reference comparison -- the filter TSM/PBS cannot
    --    express, because they have no per-realm cross-realm view.
    if not reference or reference <= 0 then
        if cfg.requireReference then
            return false, { reason = "no-reference" }
        end
        info.matchedOn = "appearance-only"
        return true, info
    end

    local threshold = reference * cfg.pctOfReference
    if unitPrice > threshold then
        return false, { reason = "over-pct-of-reference", threshold = threshold }
    end

    info.threshold  = threshold
    info.pct        = unitPrice / reference
    info.netProfit  = Rules.NetProceeds(reference) - unitPrice
    info.matchedOn  = "appearance+price"
    return true, info
end

-- Seam for spec open question 1 (TSM dependency). If TSM is installed we can
-- read its price sources for free rather than reimplementing its expression
-- DSL. Nothing calls this yet -- v1 is standalone -- but the shape is here so
-- adding it later doesn't mean reworking Evaluate.
--
--   Rules.TSMValue("dbregionsaleavg", 152510)
function Rules.TSMValue(priceSource, itemID)
    if not TSM_API then return nil end
    local itemString = TSM_API.ToItemString("i:" .. itemID)
    if not itemString then return nil end
    local value = TSM_API.GetCustomPriceValue(priceSource, itemString)
    return value  -- copper, or nil if TSM can't evaluate it
end
