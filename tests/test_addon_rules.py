"""Tests for the addon's Rules.lua, run in a real embedded Lua interpreter.

Rules.lua is deliberately pure -- no API calls, no frames, no state -- exactly
so it can be executed outside the game. `lupa` is already a dependency
(tsm_import.py decodes TSM group exports with it), so the filter logic that
decides what the player gets told about is testable rather than trust-me Lua.

The Sniper-filter vectors are the REAL numbers from
tests/test_snipe_check.py::...sniper_filter... -- itself taken from the
human's own example screenshot -- not invented ones, so a divergence between
the addon and the backend shows up as a failing test rather than as a
different set of alerts in game.
"""
from pathlib import Path

import lupa
import pytest

import snipe_check
from export_addon_data import render_lua

ADDON_DIR = Path(__file__).resolve().parent.parent / "addon" / "RealmArbSniper"

G = 10_000  # copper per gold


class Harness:
    """Rules.lua loaded into a live Lua runtime.

    Tables handed to Lua must be real Lua tables, not Python dicts: Lua reads a
    missing field as nil, whereas a dict raises KeyError, which would turn
    'this item has no sell-realm price' into a crash instead of the nil branch
    the code is written against.
    """

    def __init__(self, lua, ns):
        self.lua, self.ns, self.Rules = lua, ns, ns.Rules

    def entry(self, **fields):
        return self.lua.table_from(fields)

    def cfg(self, **overrides):
        merged = dict(self.Rules.defaults)
        merged.update(overrides)
        return self.lua.table_from(merged)

    def evaluate(self, item_id, unit_price, entry, **cfg_overrides):
        return self.Rules.Evaluate(item_id, unit_price, entry, self.cfg(**cfg_overrides))


@pytest.fixture
def h():
    """ns.Data is stubbed with the Sniper-filter thresholds the exporter would
    have written, since Rules reads them off ns.Data rather than re-declaring
    them -- that indirection is the anti-drift guarantee, asserted separately
    in test_addon_thresholds_match_backend."""
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    ns = lua.eval("{}")
    ns.Data = lua.eval("{ sniperFilter = { n = 5, closeMultiple = 1.7, minRealms = 3 } }")
    loader = lua.eval("function(src, addon, ns) return load(src, 'Rules.lua')(addon, ns) end")
    loader((ADDON_DIR / "Rules.lua").read_text(encoding="utf-8"), "RealmArbSniper", ns)
    return Harness(lua, ns)


def test_sniper_filter_rejects_a_crowded_listing(h):
    """The human's own screenshot case: a 400g listing looks like a snipe only
    because the sell realm is pricey -- the next 5 unique realms sit at
    400/428/444/500/500g, median 444g, well inside 1.7x of 400g (680g). The
    item isn't rare there, so it must not be surfaced."""
    entry = h.entry(s=1, r=5000 * G, c=444 * G, cn=5)
    matched, info = h.evaluate(930, 400 * G, entry)
    assert matched is False
    assert info.reason == "sniper-filter"
    assert info.clusterRealms == 5


def test_sniper_filter_allows_a_genuinely_isolated_listing(h):
    """Same 400g buy price, but every other realm sits at 5000g -- 12.5x away.
    Proves the filter is a real threshold check, not always-true."""
    entry = h.entry(s=1, r=5000 * G, c=5000 * G, cn=5)
    matched, info = h.evaluate(931, 400 * G, entry)
    assert matched is True
    assert info.matchedOn == "appearance+price"


def test_sniper_filter_does_not_fire_below_min_realms(h):
    """Fewer than SNIPER_FILTER_MIN_REALMS realms is 'not enough data to judge
    clustering', which snipe_check treats as DON'T flag (its COALESCE(...,0)
    short-circuit) rather than 'unknown, assume clustered'. Same here: a
    2-realm cluster sitting right on the price must still pass."""
    entry = h.entry(s=1, r=5000 * G, c=400 * G, cn=2)
    matched, _ = h.evaluate(932, 400 * G, entry)
    assert matched is True


def test_sniper_filter_can_be_turned_off(h):
    """The crowded listing from the first test passes once the filter is off,
    proving the toggle actually gates that branch and nothing else."""
    entry = h.entry(s=1, r=5000 * G, c=444 * G, cn=5)
    matched, _ = h.evaluate(930, 400 * G, entry, sniperFilter=False)
    assert matched is True


def test_no_high_value_exemption_unlike_the_dashboard(h):
    """snipe_check exempts items where BOTH sell price and region median clear
    SNIPER_FILTER_HIGH_VALUE_EXEMPT_G (200k gold). watchlist.py deliberately
    does NOT honor that on the Discord path, because the exemption only ever
    suppresses the flag and that path's rule is 'never send a flagged item'.
    The addon follows Discord, so a crowded 300k-gold item is still rejected."""
    assert snipe_check.SNIPER_FILTER_HIGH_VALUE_EXEMPT_G == 200_000
    entry = h.entry(s=1, r=300_000 * G, c=300_000 * G, cn=5)
    matched, info = h.evaluate(933, 250_000 * G, entry, maxPriceCopper=400_000 * G)
    assert matched is False
    assert info.reason == "sniper-filter"


def test_shared_appearance_is_rejected_before_anything_else(h):
    """The headline filter: 'unique' means sole-source, not uncollected."""
    entry = h.entry(s=4, r=5000 * G, c=5000 * G, cn=5)
    matched, info = h.evaluate(934, 1 * G, entry)
    assert matched is False
    assert info.reason == "shared-appearance"


def test_unknown_item_never_flags_on_absence_of_evidence(h):
    matched, info = h.evaluate(999999, 1 * G, None)
    assert matched is False
    assert info.reason == "not-in-table"


def test_region_fallback_is_off_by_default(h):
    """An item unlisted on the sell realm is invisible unless the human turns
    the fallback on -- the baseline choice stays theirs (spec open question 3)."""
    entry = h.entry(s=1, m=5000 * G, c=5000 * G, cn=5)

    matched, info = h.evaluate(935, 400 * G, entry)
    assert matched is False
    assert info.reason == "no-reference"

    matched, info = h.evaluate(935, 400 * G, entry, useRegionFallback=True)
    assert matched is True
    assert info.baseline == "region_median"


def test_net_proceeds_applies_the_five_percent_ah_cut(h):
    assert h.Rules.NetProceeds(1000 * G) == pytest.approx(950 * G)


def test_addon_thresholds_match_backend():
    """The exporter must write snipe_check's constants verbatim into Data.lua.
    watchlist.py imports them rather than re-declaring for exactly this reason
    ('so this rule and the dashboard's checkbox can never drift apart'); the
    addon is a third surface and needs the same guarantee. If someone tunes
    SNIPER_FILTER_* and the addon keeps the old numbers, this fails."""
    lua_src = render_lua({}, sell_cr=1403, max_sources=1)
    assert f"n             = {snipe_check.SNIPER_FILTER_N}," in lua_src
    assert f"closeMultiple = {snipe_check.SNIPER_FILTER_CLOSE_MULTIPLE}," in lua_src
    assert f"minRealms     = {snipe_check.SNIPER_FILTER_MIN_REALMS}," in lua_src
