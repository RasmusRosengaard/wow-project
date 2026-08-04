# Matching, inference logic and known pitfalls

How listings are matched and classified, plus the async/blocking failure mode
that has bitten this project more than once.

## Inference logic (change only with tests proving equivalence or improvement)


For each auction present in snapshot N but missing in N+1, `classify_pair()`:

1. `buyout IS NULL` → `bid_only_gone` (can't be insta-bought; excluded).
2. `time_left == SHORT` → `likely_expired`.
3. A matching `(item_id, market_key(bonus_key), pet_species_id,
   pet_quality_id, quantity)` listing appears among brand-new auction ids in
   N+1, **at a buyout within ±15% of the vanished listing's price**
   (`RELIST_PRICE_TOLERANCE`, see `diff_snapshots.py`) → `likely_relisted`
   (consumed from a per-key candidate list, so N identical vanished listings
   need N matching relists).
4. `gap_seconds >= MIN_REMAINING[time_left]` → `ambiguous` (could have expired;
   also absorbs collector-downtime gaps).
5. Else → `inferred_sale`.

**Known blind spot**: a cancel *without* relist is indistinguishable from a
sale. Never formally validated against real seller behavior (Phase 0's gate
was skipped). This is one of several reasons the pricing model no longer
depends on this classification (see "What this project is" above) — the
classification itself is still real, correct code, but (since 2026-07-25)
no longer runs automatically; it's a manual/ad-hoc tool for `analyze.py`'s
debugging commands, not a live signal (see `collect_all.py`'s row in
"Current state").

### `market_key()` — the matching-only coarsening of `bonus_key`

**No longer used by `snipe_check.py`'s live pricing/matching** (changed
2026-07-26 — see "What this project is" above's matching-model note; that
join is now plain `item_id`, no bonus/ilvl pooling logic needed at all).
Still real, still correct, still used by `diff_snapshots.relist_key()`
(needs finer-than-item_id identity for relist detection) and `analyze.py`'s
manual debugging macro. Kept here for that reason, not as dead documentation.

`bonus_key()` is pure and canonical — never changes what's stored/displayed.
`market_key(bk, base_level=None, noise_bonus_ids=None)` is a *separate*,
coarser key used only for matching/grouping (relist detection, and
formerly the buy/sell join in `snipe_check.py`) — real crafted-item
variance and Blizzard's undocumented per-craft/per-instance ids otherwise
fragment one liquid market into dozens of near-unique buckets.

Three independent things it strips, **unconditionally except type 28**:
- `MARKET_IGNORE_MODIFIER_TYPES = {9, 42, 44}` — always stripped. 42 is a
  continuous per-craft stat roll, 44 a per-instance serial (confirmed
  sequential in live data). Type 9 was confirmed by a human not to affect
  transmog appearance before being added (2026-07-24) — unlike 42/44, this
  wasn't self-evident from the data alone.
- Modifier type 28 (claimed "item level") — **conditionally** stripped, only
  when `base_level` is supplied AND the claimed value fails
  `ilvl_plausible()`. A plausible value on current-content ilvl-scaling gear
  is genuinely a different market and is left untouched. `base_level=None`
  (the default, and what `diff_snapshots.relist_key()` still passes) always
  means "don't strip" — never "assume junk."
- `noise_bonus_ids` (a per-item `frozenset[int]` of `b:` bonus-list ids) —
  Python-only. Formerly computed by `snipe_check._detect_noise_bonus_ids()`
  via a **structural** test (not a frequency threshold — a flat cutoff was
  tried and live-disproven, see `history.md`): a bonus-list value was
  treated as real only if it had a *partner* — reliably co-occurring with
  another specific value (a companion pair), or belonging to a small
  mutually-exclusive set that jointly covers most of an item's listings (a
  partition); per-craft noise had neither shape. **Removed 2026-07-26**
  along with the rest of `market_key`-based pricing (see this section's
  intro) — the per-20-sample floor this test needed (`BONUS_NOISE_MIN_SAMPLES`)
  turned out to silently fail on ~1,223 real items post-2026-07-25's
  retention change (see `history.md`'s "Bonus/ilvl matching removed" entry),
  and matching no longer needs bonus-id noise detection at all now that it
  doesn't look at bonus_key. Nothing currently computes this parameter —
  every real caller (`relist_key()`) passes `None`, same as always.

Any **new** modifier type discovered to be junk needs either strong
corroborating evidence from real data (e.g. identical troll price across
different values) or explicit human confirmation it doesn't affect the
thing it might affect (transmog appearance) before being added to the
unconditional ignore set — don't assume.

Mirrored as a SQL macro in `analyze.connect()` (`MARKET_KEY_MACRO_SQL`,
three helper macros) for DuckDB-side grouping — two independent
implementations kept honest by `tests/test_market_key.py`'s parity check
(runs the same real-item vectors through both, asserts identical results).
`noise_bonus_ids` is **not** mirrored in SQL (Python-only, and — since
2026-07-26 — nothing currently computes it at all, see above) — the parity
test only covers the base_level argument shape.

**If you touch any of this**: update both implementations, add a real
(not invented) test vector to `tests/test_market_key.py`, and check the
`project-review` skill's matching-logic checklist before shipping.


## Real production outage, lesson for next time (2026-07-25, recurred 2026-07-26)


`snipe_check._resolve_base_levels()` (since removed 2026-07-26 along with
market_key-based matching, see "What this project is" above — this
narrative describes what was true at the time) could make blocking
Blizzard API calls. `dashboard.py`'s `api_snipes()` is an `async def` route
but was calling it directly on the event loop thread — on a cold cache,
hundreds of sequential blocking calls froze the *entire* single-process
server, including unrelated routes, for the call's full duration. Fixed via
`asyncio.to_thread(...)`. **Full incident in `history.md`.**

**The same bug recurred 2026-07-26 at a second call site the first fix
didn't cover**: the `names=true` per-row translation added to `api_snipes()`
after the original fix (`NameCache.get()`/`.icon()`/`.quality()`/etc., each
a cache-miss fallback to a blocking Blizzard call) ran directly on the event
loop, never wrapped in `to_thread` at all. Symptom: switching the dashboard's
sell realm to one never queried before hung/timed out, while an
already-warmed realm (Draenor) always worked — the tell that it's *this*
class of bug, not a data issue. Fixed the same way (`asyncio.to_thread`),
plus closed a second gap found in the process: `.icon()` was never covered
by `NameCache.ensure_many()`'s concurrent batch at all (icons are a separate
media endpoint) — added `.ensure_icons_many()` alongside it. See
`history.md`'s "Realm-switch hang/timeout" entry for the full trace.

**Lesson for next time a route gains a synchronous, possibly-slow
dependency** (a new network call, a large one-time computation): ask
whether it can block *other* requests, not just whether it's correct or
fast on a warm cache. An `async def` FastAPI route does not protect you
from this by itself — it only helps if the blocking work is actually
offloaded, and **that check has to be repeated for every new blocking call
site added later, not just the one that triggered the original fix** — this
is exactly how the 2026-07-26 recurrence happened. There is still no test
coverage for "does this route block the event loop" — worth a regression
test (a slow stub swapped into `_resolve_base_levels()` or `NameCache`,
asserting a concurrent lightweight request still completes quickly) if this
class of bug recurs a third time.
