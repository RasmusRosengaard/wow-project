-- Core.lua -- addon lifecycle, saved settings, events, slash commands, output.

local ADDON, ns = ...

local Core = {}
ns.Core = Core

local PREFIX = "|cff33ff99RealmArb|r: "

-- Data.lua's appearance half comes from appearance.py, which is manual-refresh
-- by deliberate design (wago.tools sits outside the Blizzard rate budget). A
-- stale cache silently degrades the headline filter, so say so out loud
-- rather than quietly flagging on bad data. See spec open question 6.
local STALE_AFTER = 14 * 24 * 60 * 60   -- 14 days
local SCHEMA      = 1

function Core:Print(msg)
    print(PREFIX .. msg)
end

-- Copper is the unit everywhere in this project; gold exists only at display
-- boundaries. This is that boundary.
function Core.FormatMoney(copper)
    if not copper then return "?" end
    return GetMoneyString and GetMoneyString(math.floor(copper), true)
        or (math.floor(copper / 10000) .. "g")
end

-- ------------------------------------------------------------------ hits --

-- A hit is reported, never acted on. No purchase call belongs in this file
-- or any other -- the human clicks.
function Core:OnHit(info)
    local link = info.itemLink or ("item:" .. tostring(info.itemID))
    local line = PREFIX .. link .. "  " .. Core.FormatMoney(info.unitPrice)

    if info.reference then
        line = line .. ("  (%d%% of %s"):format(
            math.floor((info.pct or 0) * 100 + 0.5),
            Core.FormatMoney(info.reference))
        if info.netProfit then
            line = line .. ", net " .. Core.FormatMoney(info.netProfit)
        end
        line = line .. ")"
    end
    if info.quantity and info.quantity > 1 then
        line = line .. "  x" .. info.quantity
    end

    print(line)
    PlaySound(SOUNDKIT and SOUNDKIT.RAID_WARNING or 8959)
end

-- --------------------------------------------------------------- startup --

local defaults = {
    rules = ns.Rules.defaults,
}

local function CopyDefaults(src, dst)
    dst = dst or {}
    for k, v in pairs(src) do
        if type(v) == "table" then
            dst[k] = CopyDefaults(v, dst[k])
        elseif dst[k] == nil then
            dst[k] = v
        end
    end
    return dst
end

function Core:OnLoad()
    RealmArbSniperDB = CopyDefaults(defaults, RealmArbSniperDB)
    ns.db = RealmArbSniperDB

    local d = ns.Data
    if d.schema ~= SCHEMA then
        self:Print("|cffff5555Data.lua schema " .. tostring(d.schema)
            .. " but this addon expects " .. SCHEMA
            .. ". Re-export before trusting any result.|r")
    end
    if d.generated == 0 then
        self:Print("|cffffaa00Data.lua is the development stub -- three "
            .. "invented rows. Import real data before using this.|r")
    elseif (time() - d.generated) > STALE_AFTER then
        self:Print(("|cffffaa00Data.lua is %d days old. Appearance data only "
            .. "moves on content patches, but the reference prices are stale.|r")
            :format(math.floor((time() - d.generated) / 86400)))
    end
end

-- ---------------------------------------------------------------- events --

local f = CreateFrame("Frame")
f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("AUCTION_HOUSE_SHOW")
f:RegisterEvent("AUCTION_HOUSE_CLOSED")
f:RegisterEvent("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
f:RegisterEvent("AUCTION_HOUSE_BROWSE_RESULTS_ADDED")
f:RegisterEvent("ITEM_SEARCH_RESULTS_UPDATED")

f:SetScript("OnEvent", function(_, event, arg1)
    if event == "ADDON_LOADED" then
        if arg1 == ADDON then Core:OnLoad() end

    elseif event == "AUCTION_HOUSE_CLOSED" then
        -- Every query in Scan.lua requires an open auction house.
        ns.Scan:Stop()

    elseif event == "AUCTION_HOUSE_BROWSE_RESULTS_UPDATED"
        or event == "AUCTION_HOUSE_BROWSE_RESULTS_ADDED" then
        ns.Scan:OnBrowseResults()

    elseif event == "ITEM_SEARCH_RESULTS_UPDATED" then
        ns.Scan:OnItemResults(arg1)   -- arg1 is the itemKey
    end
end)

-- ----------------------------------------------------------------- slash --

SLASH_REALMARB1 = "/realmarb"
SLASH_REALMARB2 = "/ras"

SlashCmdList.REALMARB = function(msg)
    local cmd, rest = msg:match("^(%S*)%s*(.-)$")
    cmd = (cmd or ""):lower()
    local cfg = ns.db and ns.db.rules or ns.Rules.defaults

    if cmd == "start" then
        if not C_AuctionHouse.IsThrottledMessageSystemReady then
            Core:Print("auction house API unavailable.")
        else
            ns.Scan:Start()
        end

    elseif cmd == "stop" then
        ns.Scan:Stop()

    elseif cmd == "pct" then
        local v = tonumber(rest)
        if v and v > 0 and v <= 100 then
            cfg.pctOfReference = v / 100
            Core:Print(("flagging at <= %d%% of reference."):format(v))
        else
            Core:Print("usage: /realmarb pct 40")
        end

    elseif cmd == "max" then
        local v = tonumber(rest)
        if v and v > 0 then
            cfg.maxPriceCopper = v * 10000    -- entered in gold, stored copper
            Core:Print("max price " .. Core.FormatMoney(cfg.maxPriceCopper))
        else
            Core:Print("usage: /realmarb max 5000   (gold)")
        end

    elseif cmd == "sources" then
        local v = tonumber(rest)
        if v and v >= 1 then
            cfg.maxSourceCount = math.floor(v)
            Core:Print(("appearances with <= %d source item(s)."):format(v))
        else
            Core:Print("usage: /realmarb sources 1")
        end

    elseif cmd == "sniper" then
        cfg.sniperFilter = not cfg.sniperFilter
        Core:Print("sniper filter " .. (cfg.sniperFilter and "ON" or "OFF")
            .. (cfg.sniperFilter and "" or " -- expect corroborated, non-rare listings"))

    elseif cmd == "status" then
        local sf = ns.Data.sniperFilter
        Core:Print(("%s | <= %d%% of ref | max %s | sources <= %d | budget %d/min left"):
            format(ns.Scan.running and "running" or "stopped",
                math.floor(cfg.pctOfReference * 100),
                Core.FormatMoney(cfg.maxPriceCopper),
                cfg.maxSourceCount,
                ns.Scan:BudgetRemaining()))
        if cfg.sniperFilter and sf then
            Core:Print(("sniper filter ON -- reject when >= %d other realms' "
                .. "median sits within %.1fx the price"):format(sf.minRealms, sf.closeMultiple))
        else
            Core:Print("sniper filter OFF")
        end

    else
        Core:Print("/realmarb start | stop | status | pct <n> | max <gold> "
            .. "| sources <n> | sniper")
    end
end
