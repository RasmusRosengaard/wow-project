-- Scan.lua -- the live single-realm scan loop.
--
-- Scope is deliberately identical to Point Blank Sniper: ONE realm, the one
-- the player is logged into. The addon never scans other realms and never
-- needs to -- the cross-realm work already happened server-side and arrives
-- as Data.lua.
--
-- WHY THIS IS A DIFF AND NOT A SCAN
--
-- There is no live full-AH mirror and no "an auction was posted" event.
--   * C_AuctionHouse.ReplicateItems() is the only full dump and carries a
--     15-minute ACCOUNT-WIDE throttle. Useless for sniping.
--   * C_AuctionHouse.SendSearchQuery() is capped at 100 calls/minute. It is
--     the scarce resource and must be rationed.
--   * C_AuctionHouse.SendBrowseQuery() returns AGGREGATED summaries -- one
--     row per item key with a min price and total quantity, in groups of 500.
--     No per-listing detail, no timestamps.
--
-- So "instant new post" detection is: poll the cheap aggregated browse, diff
-- it against the previous poll, and spend a precious search query only on the
-- item keys that actually moved. Structurally the same thing diff_snapshots.py
-- does server-side.
--
-- NOTHING HERE BUYS ANYTHING. No PlaceBid, no StartCommoditiesPurchase, no
-- input simulation. The addon highlights and alerts; the human clicks. That
-- is a product guardrail (CLAUDE.md: decision support only), not just a
-- consequence of the client requiring a hardware event.

local ADDON, ns = ...

local Scan = {}
ns.Scan = Scan

-- Blizzard's documented ceiling is 100 search queries/minute. Stay under it:
-- being throttled off mid-sweep costs far more than the queries we skip.
local SEARCH_BUDGET_PER_MIN = 80
local BROWSE_INTERVAL       = 2.0   -- seconds between browse polls

Scan.running   = false
Scan.lastSeen  = {}    -- [key] = { minPrice = copper, qty = n }
Scan.pending   = {}    -- FIFO of itemKeys awaiting a search query
Scan.queued    = {}    -- [key] = true, dedupes Scan.pending
Scan.spent     = {}    -- timestamps of recent search queries (rolling window)

-- Match key. Mirrors the project's (item_id, pet_species_id) decision from
-- 2026-07-26: every bonus/ilvl variant of an item is ONE market. The buy-side
-- listing's own ilvl is display-only, so it must not enter this key.
local function MatchKey(itemKey)
    if not itemKey then return nil end
    return itemKey.itemID .. ":" .. (itemKey.battlePetSpeciesID or 0)
end
Scan.MatchKey = MatchKey

-- ---------------------------------------------------------------- budget --

local function PruneBudget()
    local cutoff, kept = GetTime() - 60, {}
    for _, t in ipairs(Scan.spent) do
        if t > cutoff then kept[#kept + 1] = t end
    end
    Scan.spent = kept
end

function Scan:BudgetRemaining()
    PruneBudget()
    return SEARCH_BUDGET_PER_MIN - #self.spent
end

-- ---------------------------------------------------------------- browse --

-- The narrower this query, the more of the search budget survives for items
-- that matter. Quality/class narrowing is the main lever.
function Scan:BuildBrowseQuery()
    local cfg = ns.db and ns.db.rules or ns.Rules.defaults
    return {
        searchString    = "",
        sorts           = {},
        minLevel        = 0,
        maxLevel        = 0,
        filters         = {},
        itemClassFilters = {},
        quality         = cfg.minQuality,
    }
end

function Scan:PollBrowse()
    if not self.running then return end
    -- Never fire into a throttled system; the query is dropped, not queued.
    if not C_AuctionHouse.IsThrottledMessageSystemReady() then return end
    C_AuctionHouse.SendBrowseQuery(self:BuildBrowseQuery())
end

-- Diff the aggregated summaries against the previous poll. A min price that
-- dropped, or a quantity that rose, means something was posted since we last
-- looked. First sight of a key seeds the baseline WITHOUT flagging -- on the
-- very first poll every item looks "new", and alerting on all of it would
-- bury the user in noise at exactly the moment they start scanning.
function Scan:OnBrowseResults()
    if not self.running then return end

    local results = C_AuctionHouse.GetBrowseResults()
    if not results then return end

    for _, r in ipairs(results) do
        local key = MatchKey(r.itemKey)
        if key then
            local prev = self.lastSeen[key]
            local minPrice, qty = r.minPrice, r.totalQuantity

            if prev then
                local cheaper = minPrice and prev.minPrice and minPrice < prev.minPrice
                local more    = qty and prev.qty and qty > prev.qty
                if (cheaper or more) and not self.queued[key] then
                    -- Cheap pre-check: if the item can't pass the appearance
                    -- gate, never spend a search query resolving it. This is
                    -- what keeps the 100/min budget viable.
                    local entry = ns.Data.items[r.itemKey.itemID]
                    local cfg = ns.db and ns.db.rules or ns.Rules.defaults
                    if entry and entry.s and entry.s <= cfg.maxSourceCount then
                        self.queued[key] = true
                        self.pending[#self.pending + 1] = r.itemKey
                    end
                end
            end

            self.lastSeen[key] = { minPrice = minPrice, qty = qty }
        end
    end

    -- Browse results arrive in groups of 500; pull the rest before deciding
    -- the picture is complete.
    if not C_AuctionHouse.HasFullBrowseResults() then
        C_AuctionHouse.RequestMoreBrowseResults()
    end
end

-- ---------------------------------------------------------------- search --

function Scan:DrainPending()
    if not self.running then return end
    while #self.pending > 0 and self:BudgetRemaining() > 0 do
        if not C_AuctionHouse.IsThrottledMessageSystemReady() then return end
        local itemKey = table.remove(self.pending, 1)
        self.spent[#self.spent + 1] = GetTime()
        C_AuctionHouse.SendSearchQuery(itemKey, {}, true)
    end
end

-- Individual listings for one item key have arrived. Apply the rules.
function Scan:OnItemResults(itemKey)
    local key = MatchKey(itemKey)
    if key then self.queued[key] = nil end
    if not itemKey then return end

    local itemID = itemKey.itemID
    local entry  = ns.Data.items[itemID]
    local cfg    = ns.db and ns.db.rules or ns.Rules.defaults

    local n = C_AuctionHouse.GetNumItemSearchResults(itemKey) or 0
    for i = 1, n do
        local r = C_AuctionHouse.GetItemSearchResultInfo(itemKey, i)
        if r then
            -- Per-unit, never the stack total. Getting this wrong silently
            -- turns a 20-stack into a fake bargain -- the exact class of
            -- copper-units bug CLAUDE.md warns about.
            local qty  = (r.quantity and r.quantity > 0) and r.quantity or 1
            local unit = r.buyoutAmount and (r.buyoutAmount / qty) or nil

            local matched, info = ns.Rules.Evaluate(itemID, unit, entry, cfg)
            if matched then
                info.itemLink  = r.itemLink
                info.auctionID = r.auctionID
                info.quantity  = qty
                ns.Core:OnHit(info)
            end
        end
    end
end

-- ----------------------------------------------------------- start/stop --

function Scan:Start()
    if self.running then return end
    self.running  = true
    self.lastSeen = {}
    self.pending  = {}
    self.queued   = {}
    self.ticker = C_Timer.NewTicker(BROWSE_INTERVAL, function()
        Scan:PollBrowse()
        Scan:DrainPending()
    end)
    ns.Core:Print("scanning this realm. First pass seeds the baseline -- "
        .. "hits start from the second sweep.")
end

function Scan:Stop()
    if not self.running then return end
    self.running = false
    if self.ticker then self.ticker:Cancel() end
    self.ticker = nil
    ns.Core:Print("stopped.")
end
