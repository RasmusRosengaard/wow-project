# PROGRESS — WoW AH Snipe Validator

Living status doc: what's built, what's not, what's next. `CLAUDE.md` is
still the authoritative brief (architecture, conventions, full roadmap,
API facts) — this file is the scannable summary, kept in sync with it.

Last updated: 2026-07-25 (type-28 fix + the production outage it caused, fixed same session; disk/retention investigated; a follow-up per-request latency bug in the same code path, fixed same day; a second market-fragmentation bug via `b:` bonus-list ids, also fixed same day).

## Status at a glance

**Live and working right now**, at `https://wow-project-production.up.railway.app`:
- Register / log in / log out; subscribe/cancel/status all on the profile page.
- Subscribe via Stripe (**live mode, real payments** — human decision
  2026-07-23) → dashboard access unlocks automatically via webhook.
- Server-side data collection running every ~10 minutes for FULL/HIGH-pop EU
  realms, no human machine required.
- Auto-deploy on push to `main`, gated on tests passing (CI → Railway
  "Wait for CI" → build → deploy → DB migration, all automatic).
- Every page (dashboard, login, register, subscribe, profile) runs one
  consistent designed look — see "UI design pass" below.
- Sell-realm dropdown (`/api/realms`), min/max gold budget filter, grouped
  duplicate-item snipes (best deal on top, expandable), and instant
  client-side table sorting — see "Dashboard QoL pass" below.
- **New this session (2026-07-23 evening, part 1)**: sold-price estimates are
  now capped at the sell realm's current cheapest live listing; a Phase 3
  transmog-rarity filter (`appearance.py`, "Unique transmog only" checkbox);
  a "Sell realm low" price column; crafted items no longer fragment into
  dozens of 1-2-sale buckets (`market_key()` pools Blizzard's undocumented
  per-craft modifiers out of matching); `--max-per-item` caps duplicate
  listings of one item; the status ticker is one honest timestamp instead of
  three overlapping/misleading ones; a **public, no-login** `/log` page shows
  every time new AH data was actually retrieved.
- **New this session (2026-07-23 evening, part 2)**: `dashboard.html`
  redesigned to a light "assay ledger" identity (full rethink from the old
  dark "Undermine cartel" theme — see "UI design pass" below) with a dark
  mode toggle; a loading indicator on Refresh/auto-refresh; a "Min sell
  realm low (g)" filter; profession tools/accessories (Mining Pick,
  Blacksmith Hammer, etc.) no longer count as "unique transmog"; background
  collection now polls every 45s (not 10min) during the realm's observed
  publish window instead of a flat cadence. See "Evening session" below for
  full detail on both parts.

**Not built yet**, in priority order — see "Next up" below for detail:
1. Restricted Stripe key (still the full `sk_live_...` secret). Human-only, not scheduled.
2. Adaptive disk retention — current `RETENTION_DAYS = 14` projects to
   ~8.7GB at full history, over the Volume's ~4.9GB practical cap.
   Investigated 2026-07-25, proposed but not built — see "Known gaps" below.
3. A camped-relist false-positive still slips through occasionally (separate,
   older bug from the crafted-item fragmentation fix — see "Known gaps" below).

**New this session (2026-07-25, continued yet again)**: human reported the
live dashboard stuck on "Loading…" for 5+ minutes while logged in as
superuser. Traced to `_populate_base_levels()` (the type-28 fix's own
helper) resolving each not-yet-cached item's base level one sequential
Blizzard API call at a time. A first fix (parallelizing via a thread pool)
turned out insufficient once live numbers came in: **17,408** distinct
items region-wide carry a type-28 modifier, **15,883** from one sell realm
alone — type 28 is common on modern gear, not rare, so no amount of
concurrency beats Blizzard's 100 req/s ceiling against a set that size.
Real fix: `_populate_base_levels()` now caps how many new items it
resolves per call (500, prioritizing the sell realm's own items) so no
request can hang again, plus a new background pre-warm step in
`collect_all.py` (runs every ~10-min cycle, resolves up to 1000 more items
regardless of user traffic) so the cache actually converges over a few
hours instead of depending on dashboard loads. Also: a `fetchInFlight`
guard in `dashboard.html` stops overlapping auto-refresh requests from
piling on top of each other. See `CLAUDE.md`'s "Per-request latency fix"
section for the full trace, both fix passes, and verification. `pytest -q`:
235 passing.

**New this session (2026-07-25, continued yet again, part 2)**: a second
real snipe-matching bug, reported right after the latency fix above — item
36507 (Iron-Molded Fist) showed a 66,666g "deal" while a genuine 5,400g
listing on another realm never surfaced. Root cause: a per-craft "instance"
id living in the `b:` bonus-lists segment (not the `m:` modifiers segment
the earlier type-9/42/44/28 fixes covered), varying almost per-listing
alongside one stable, real id. Investigated at scale first (6,640 items
show a similar collapsible pattern region-wide) before choosing a fix —
document-frequency detection per item, not a blanket rule, since inspecting
two of those items showed most of their bonus-list dimensions are real,
price-relevant features (quality tiers, socket/gem choices) that must NOT
be pooled. `market_key()` gained a third `noise_bonus_ids` arg;
`_populate_base_levels()` was renamed to `_populate_market_keys()` and now
also does this detection + precomputes market_key in Python for every
distinct (item_id, bonus_key) pair, bulk-loaded via Arrow (executemany()
tested unusably slow at this row count — 699k pairs — live). See
`CLAUDE.md`'s matching section for the full trace, the "why not a static
list" reasoning, and the confidence-level caveat (data-driven, not
human-confirmed like type 9). `pytest -q`: 242 passing.

**New this session (2026-07-25, continued yet again, part 3)**: the
frequency-only fix from part 2 was live-checked against the full production
sample and found insufficient — item 36507's own noise reached 14%
frequency, overlapping real dimensions on other items also observed as low
as 14-17%. Human asked for a real fix rather than living with the gap.
Replaced with a structural test: a bonus-list value is treated as real only
if it has a *partner* — reliably co-occurring with another specific value
(a companion pair, like item 109168's two-part gem bonus), or belonging to
a small mutually-exclusive set that jointly covers most of an item's
listings (a partition, like item 244752's real 5-value item-level-upgrade-
track system, found during this investigation and recurring across
multiple different items). A pure cardinality count ("few distinct values
= real") was tried in between and also rejected — it broke on item 210108,
which legitimately has ~10 real ambiguous-band values from several stacked
dimensions. Validated live against 8 diverse real items (noise-heavy and
multi-dimension-real alike) with zero misclassifications; the originally
reported item 36507 case now resolves correctly. Also caught and fixed a
real DuckDB correctness bug while building this: `ROW_NUMBER() OVER ()`
inside a CTE referenced twice (once per side of a self-join) doesn't give
stable, matching ids — fixed by materializing as a real temp table.
`pytest -q`: 244 passing.

**New this session (2026-07-25, continued again)**: a real snipe-matching
bug (item 164353, cheap Auchindoun listing not surfacing as a snipe) traced
to `market_key()` matching on raw, junk type-28 "ilvl" values — fixed with
per-item conditional pooling (a plausible ilvl on current-content gear
stays distinct; an implausible one on old transmog gear pools away), which
also required tightening `ILVL_PLAUSIBILITY_MULTIPLE` from 5x to 3x. Also
investigated (not built) an adaptive disk-retention scheme after confirming
current growth would exceed Railway's volume cap at the existing 14-day
retention. See "Session 2026-07-25, continued again" below for both.

**New this session (2026-07-24)**: the light "assay ledger" redesign (with
dark-mode toggle) rolled out to all 5 remaining pages — `login.html`,
`register.html`, `subscribe.html`, `profile.html`, `log.html` — matching
`dashboard.html`. All six pages now share one visual identity. See
`CLAUDE.md`'s "Full visual rethink" note for detail. Also: the Phase 3
per-item transferability flag question is **closed, not just deferred** —
human decision that no flag will ever be needed, since every item this tool
surfaces is by definition AH-listed and therefore unconditionally
unsoulbound (see `CLAUDE.md`'s "Transferability flag, resolved" note).

**Also new this session**: the dashboard's filter rail is now entirely
client-side — every threshold (discount%, sales/day, gold range, sell-now,
max-per-item, unique-transmog) re-filters an already-fetched batch instantly
instead of round-tripping to the server on every change, plus a new 8-way
item-class filter (weapons/armor/containers/profession/housing/battle pets/
quest items/mounts). See "Client-side snipe filtering + item-class filter"
below for the full design. Same day, also: the fetched batch is now cached
in `localStorage` and the 60s auto-refresh only does the expensive query
when Blizzard's data actually changed — see "localStorage batch cache"
below.

**New this session (2026-07-25)**: investigating a user-reported bad price
(item 7761, Steelclaw Reaver) found `market_key()` was missing another
undocumented modifier type (9) that fragmented matching the same way 42/44
did — fixed, human-confirmed safe (doesn't affect transmog) before shipping.
The same investigation exposed a deeper, still-open gap: if a sell realm's
*entire* observed history for an item is troll/camped listings, no existing
guard can rescue the estimate (see "Known gaps" below). Also: the dashboard
now has a **free tier** — any logged-in account can preview real (capped)
data instead of hitting a hard subscribe wall, with row budgets tiered by
account (250 free / 2000 subscribed / 5000 superuser); the "Top" display-cap
control was removed as redundant once tiering made the real budget visible
server-side. See "Tiered batch caps + free dashboard tier" below for both.
Same day, also: dashboard table rows now group by the pooled `market_key`
instead of the exact bonus string (fixes a real case of one market showing
as several separate rows); a **free-tier account is now locked to one sell
realm** (bounds real query cost) with a new public **`/pricing`** page
explaining free vs. subscriber; `subscribe.html`'s copy was rewritten to
match. See "Free-tier single-realm lock + /pricing page" below.

## Next up (short list, do these in roughly this order)
1. Adaptive disk retention — `collect_all.py`'s `prune_old_snapshots()`/`RETENTION_DAYS`; scope confirmed with the human 2026-07-25 (tighten day-based retention, budget "up to 4-5GB") but not yet implemented — see "Known gaps".
2. Decide whether to fix the remaining camped-relist false-positive bug (relist-matching window logic — deferred, not started, see "Known gaps").
3. TSM/Auctionator buylist export idea (see "Future work" below) — no design done yet.
4. Restricted Stripe key — swap the full `sk_live_...` secret key for a key restricted to just Checkout/Customers/Subscriptions/Webhooks (Stripe's own current guidance, real bug-radius reduction but low urgency). **Human-only, asked explicitly 2026-07-23**: do not rotate/swap this live credential without the human present, even when otherwise told to keep working autonomously.
5. Phase 2 (commodities feed) — explicitly out of scope per human direction 2026-07-24, not being pursued.

## Future work (ideas, not scheduled)

Not prioritized, not started, no design work done — just captured so they
aren't lost.

- **Export selected snipes as a buylist for Auctionator or TSM** (human
  idea, 2026-07-23). Let a user select rows in the dashboard ledger and
  export them in whatever import format those addons expect (TSM has a
  documented shopping-list string format; Auctionator has its own import
  format) so a validated snipe can be queued up in-game without manually
  retyping item names/realms. Needs: research into the exact TSM/Auctionator
  string formats, a UI selection mechanism (checkboxes per row?), and a
  client-side export button (no backend change likely needed — this is
  formatting already-fetched row data, not new data).

- **TSM public pricing data as a cross-check, not a replacement** (human
  idea, 2026-07-25). Investigated: `public-data.tradeskillmaster.com`
  serves free, unauthenticated, per-realm CSVs (`.../retail/{region}/realm/
  {slug}/items.csv`, no key/rate-limit); a separate authenticated TSM Web
  API exists too. Both are ultimately derived from the same Blizzard AH
  dumps this project already pulls directly, run through TSM's own
  closed-source aggregation — not an independent or deeper ground truth
  (their own "historical price" is only a 60-day rolling average, same
  order of magnitude as what this project could accumulate itself). The
  actual `tradeskillmaster.com`/`support.tradeskillmaster.com` pages
  (including their terms of use — not yet read, would need to be before
  building anything on this) block automated fetches, so this was pieced
  together from search results and a third-party GitHub repo, not
  confirmed firsthand. Best-scoped use if revisited: a periodic sanity-
  check of this project's own `per_day`/sold-price percentiles against
  TSM's regional numbers — literally the Phase 0 validation step
  `README.md`'s verification protocol has called for since the start and
  never run — not a replacement for the live Blizzard-API-driven snipe
  detection. Parked, no design work done, not scheduled.

**Remember**: if a custom domain ever replaces the `railway.app` subdomain, the Stripe webhook endpoint URL (Stripe Dashboard → Developers → Webhooks) needs updating to match by hand — it won't happen automatically.

## Hosted SaaS pivot — stage by stage

Turning the local single-user tool into a hosted product. Full design in
`~/.claude/plans/unified-nibbling-simon.md`. All 5 stages are now **done**:

| Stage | What it is | Status |
|---|---|---|
| 1 | GitHub repo, CI, branch protection | **Done** — private repo, `pytest -q` on every push, branch protection requires it. |
| 2 | Email auth (FastAPI-Users + Postgres) | **Done** — register/login/logout, cookie sessions, API routes gated. |
| 3 | Stripe subscription | **Done, live mode** — see detail below. |
| 4 | Server-side collection + realm picker | **Done** — backend and UI dropdown both shipped. |
| 5 | CD (Railway auto-deploy + DB migrations) | **Done** — live URL, Wait-for-CI verified working end to end. |

### Stage 3 detail — Stripe (done 2026-07-23, deployed straight to live mode)

Human decision: skip test-mode verification, go live immediately. `billing.py`:
`POST /billing/checkout` creates a Checkout Session for the single €4.99/mo
price and redirects; `POST /billing/webhook` verifies the Stripe signature
and handles `checkout.session.completed` / `customer.subscription.updated` /
`customer.subscription.deleted`, writing `subscription_status` (and
customer/subscription ids, period end) onto the user — the only writer of
those fields. `auth.current_subscribed_user` gates `/api/snipes` and
`/api/status` (402 if unsubscribed, distinct from 401 if not logged in at
all, so the frontend can send you to `/subscribe` vs `/login` correctly).

Verified live end to end: unauthenticated → 401, logged-in-but-unsubscribed →
402, and a real `cs_live_...` Checkout Session URL generated successfully.

**Real bug the test suite caught before it shipped**: `event["data"]["object"]`
from `stripe.Webhook.construct_event()` is a `StripeObject`, not a plain
dict — it supports `obj["key"]` but *not* `obj.get("key")`, which every
handler used. Would have thrown on the very first real webhook delivery;
fixed by calling `.to_dict()` once up front (needed on the retrieved
`Subscription` object too, same issue).

Still open: a restricted (`rk_live_...`) key instead of the full secret key
(see "Next up").

### Stage 4 detail — server-side collection (backend done 2026-07-23)

Scope deliberately narrowed same-day: deep-collects **FULL/HIGH population
realms only**, not literally all ~100 EU realms (human's call — less
overhead, more sniping-relevant liquidity). The region-wide listings sweep
stays unscoped (every EU realm), since the cross-realm thesis specifically
needs cheap listings from low-pop realms too.

`collect_all.py` runs in-process inside `dashboard.py`, polling every ~10
minutes rather than hourly — Blizzard republishes at no fixed clock time, so
a fixed hourly poll from container-boot could drift out of phase with the
real update by up to an hour; `fetch_once()`'s `If-Modified-Since` check
keeps no-op polls cheap, and `diff_snapshots` only re-runs when a realm
actually got a new snapshot that cycle (not every tick — it recomputes from
scratch each time, so re-running it for no new information would waste
real CPU at this polling frequency).

`run_cycle.py`, `run_cycle_task.ps1`, and the local Windows Task Scheduler
job are **fully deleted** (human decision: this product is never run
locally as a going concern) — Railway is the sole collection path. Local
dev Postgres stopped, not removed, for local Stage-3+-adjacent dev only.

The `/api/realms` + dropdown UI piece is done — see "Dashboard QoL pass" below.

### UI design pass (done 2026-07-23)

Ran every page through the `frontend-design` skill — an "Undermine cartel
trading-floor" identity: dark olive/moss panels (`--bg #14170f`, `--panel
#1b1f15`), a toxic-green accent (`--toxic #a6e600`) for primary actions, brass
and ember as secondary accents, system font stacks throughout (no external
fonts — keeps the "no build step, offline-capable" convention), `ui-monospace`
for all numeric/data columns. The dashboard's signature element is a static
segmented status ticker (`.masthead`/`.ticker`) replacing the old plain status
div — deliberately not animated, respects `prefers-reduced-motion`. Applied
consistently to `dashboard.html`, `login.html`, `register.html`,
`subscribe.html`, `profile.html`; each page's existing JS/functional logic
was preserved exactly, only markup/CSS changed.

Also removed per explicit human instruction: the repeated "NOTE: an AH
listing is guaranteed unsoulbound..." caveat banner is gone from the UI
entirely (was CSS + HTML + a JS line populating it from `data.caveat`) — the
API still returns `caveat` in its response, it's just not rendered anymore.

Folded in the two QoL fixes from the old "Next up" #4 at the same time:
min-discount filter is now a real percentage input (0–100, was a raw 0–1
fraction) and the names/icons toggle is gone — resolving item_id → name/icon
is always on now, there was no real reason to ever turn it off.

### Dashboard QoL pass (done 2026-07-23)

Four requests worked in one batch, all client-facing on `dashboard.html`
plus the two small backend additions they needed:

- **Sell-realm picker** — new `GET /api/realms` (`dashboard.py`) lists every
  realm with a `data/events/{cr}.parquet` file (i.e. actually collected),
  each resolved to a display name via the existing `_realm_info()` cache.
  The dashboard's free-typed realm-id number box is now a `<select>`
  populated from this on load, falling back to a plain "no realms collected
  yet" option if the list comes back empty.
- **Min/max gold filter** — `snipe_check.find_snipes()` gained `min_gold`/
  `max_gold` params (filtering on the *buy*-side unit price, i.e. what
  you'd actually spend — that's the number a budget cap means for an "AH
  sniper"), threaded through `/api/snipes` and the `--min-gold`/`--max-gold`
  CLI flags. Two new number inputs in the dashboard's filter bar, both
  optional (blank = no bound).
- **Duplicate snipes grouped** — when the same item/variant shows up as a
  snipe more than once (different buy realms, or several auctions on one),
  the ledger now shows one row per item with the single best deal (highest
  discount%) on top, plus a `▾ N` toggle to expand and see the rest, still
  best-first. Purely a client-side `static/dashboard.html` change — the API
  still returns the flat row list, `renderTable()` groups it.
- **No-lag sorting** — every sort click used to re-fetch `/api/snipes` over
  the network just to reorder rows the browser already had. Sorting is now
  entirely client-side (`renderTable()`/`compareRows()`): clicking a column
  header reorders the already-loaded batch instantly, with an ascending/
  descending toggle on repeat clicks (▲/▼ indicator). All seven columns are
  sortable now, not just the three the server's `SORT_COLUMNS` supported
  before (buy realm, item name, and variant are new). The trade-off: the
  fetched batch is always the server's discount-ranked top N, so re-sorting
  by e.g. sell price reorders *within* that batch rather than asking the
  server for a differently-ranked top N — acceptable for the "reorder what's
  on screen instantly" goal this was solving; Refresh/auto-refresh still
  re-fetches the accurate discount-ranked set.

### Real bug caught live: single-sale price artifact (fixed 2026-07-23)

Shortly after the QoL pass shipped, a user-reported price mismatch (item
15138, Onyxia Scale Cloak, showing a sell price of ~99,624g against a real
value around 444g) was traced live against production (`analyze.py --cr-id
1403 trace 15138`, over a `railway ssh` session — see `CLAUDE.md`'s "Remote
debugging note") to exactly one `inferred_sale` ever recorded for that item:
a troll-priced decoy listing that almost certainly got cancelled, not
bought — the known, documented `inferred_sale` cancel-without-relist blind
spot. With Draenor's collection history still young, `min_per_day` alone
didn't guard against it (a single sale over a short `days` window can round
up past the 0.5 threshold easily). Fixed with a new `min_sales` floor
(default 2) in `snipe_check.find_snipes()` — requires at least N inferred
sales before trusting the percentile at all, on top of (not instead of)
`min_per_day`. Not a full fix for the underlying blind spot (Phase 0's gate
is still skipped, the signal is still unvalidated), just a floor against the
single-sample case specifically.

### Evening session (2026-07-23): more price-quality bugs, Phase 3 groundwork, /log

Continuation of the same day, picking up after the single-sale artifact fix.
Six commits, `62d2106..ec491e9`, each deployed and verified live before
moving to the next:

1. **Sell-price cap** (`83a236c`) — even `min_sales >= 2` wasn't enough: item
   206477 (Warsword of Caer Darrow) had 4 inferred sales, but 2 were the
   *same* 149,379g troll listing, dragging the estimate to 75,074g against a
   real ~700g item. Fix: `sell_price` is now `LEAST(sold_percentile,
   current_cheapest_live_listing_on_sell_realm)` — you can't realistically
   sell above what's already listed cheaper on your own realm anyway, and
   it's real-time data, not inference. New `sell_now_g`/`sell_now_copper`
   fields; a "Sell realm low" dashboard column (`8933af0`).
2. **Phase 3 groundwork** (`fe75e0a`, `76b87f5`) — new `appearance.py`
   caches itemId → transmog-appearance rarity (126k items / 48k appearances)
   from wago.tools' `ItemModifiedAppearance` DB2 export (not a Blizzard API
   — none exists for this). `snipe_check.py --max-appearance-sources N` /
   dashboard "Unique transmog only" checkbox. **Known accuracy gap**: on at
   least one item (14042, Cindercloth Vest), this said `source_count=1`
   (verified correct against Blizzard's own two DB2 tables) while Wowhead's
   page claims "same model as 5 others" — likely a finer-grained
   mesh/texture comparison Wowhead does that isn't captured in the DB2
   export we use. No Wowhead API exists to cross-check against (confirmed
   — their page data is client-JS-rendered, not statically embedded).
   Flagged as an approximation, not fixed.
3. **Crafted-item market fragmentation** (`1874bc4`) — a bigger, structural
   version of the same problem: item 238014 (Sun-Blessed Sickle, crafted)
   had 25+ distinct exact `bonus_key`s on Draenor, most clustered around
   1000-1300g, but exact-`bonus_key` matching meant any one listing's
   percentile came from whichever 1-2-sale bucket its exact crafted roll
   fell into. Root cause: Blizzard's undocumented modifier types 42 (a
   continuous per-craft stat roll) and 44 (a per-instance serial number,
   confirmed sequential in live data: 245822, 245823, 245824...) make
   `bonus_key` itself near-unique per crafted item. Fix: new `market_key()`
   (`fetch_snapshot.py`) strips just those two modifier types for
   matching/grouping only — relist detection (`diff_snapshots.py`) and
   sold-price/current-lowest/buy-sell matching (`snipe_check.py`) all use
   it now; the raw `bonus_key` is still stored and displayed everywhere.
   Verified fixed live via `railway ssh`. **One sub-case remains open**: if
   a specific crafted variant's *entire* sales history is a camped relist
   that keeps slipping through the relist-matching window (an older,
   separate bug), pooling can't rescue it — there's nothing legitimate to
   pool against. Not fixed this session; human was told explicitly and it's
   parked, not forgotten.
4. **`--max-per-item`** (`1874bc4`, same commit) — caps how many listings
   of one item/variant can fill the results (by `market_key`, keeping the
   best-discount ones via a SQL `ROW_NUMBER()` window), so one popular item
   can't crowd out variety. `--top 500 --max-per-item 1` = up to 500
   distinct items. New dashboard "Max per item" input.
5. **Status ticker simplified twice** (`1874bc4`, `d2d3da3`) — first
   dropped "Polled" (was just the browser's render-time wall clock, not
   real data), then — per direct feedback that the remaining 3 segments
   still didn't answer "when was new data actually retrieved" clearly —
   collapsed to one segment, "Last auction data" (the real Blizzard
   `Last-Modified` timestamp).
6. **Public `/log` page** (`ec491e9`) — human's explicit ask: a page any
   visitor can view (no login) showing every timestamp new AH data was
   retrieved per realm. New `GET /api/log/realms` / `GET /api/log?sell={cr}`
   are the only unauthenticated `/api/*` routes in the app by design (realm
   names and raw retrieval timestamps aren't the paid product). Reads
   `data/snapshots/{cr}/*.parquet` filenames directly — `fetch_snapshot.py`'s
   `If-Modified-Since` check means a file only exists when Blizzard actually
   published something new, so the file list already *is* a complete,
   honest log with zero new logging infrastructure.

A **full visual redesign** was discussed at length (all 6 pages) — see
`CLAUDE.md`'s "Full visual rethink" note. Unlike the first part of this
session, this one didn't stay purely conceptual: `dashboard.html` was
actually rebuilt and shipped (part 2, below) after the human reacted to a
live preview and follow-up feedback.

### Evening session, part 2 (2026-07-23, later the same evening)

Continuation after part 1 above, picking up with the human's design
feedback and a batch of dashboard requests. Three commits (`aa92ed4`
redesign, `47633ff` rarity-ring fix, `bc09f75` the dark-mode/loading/
filter/profession-tool/poll-interval batch), each deployed and
spot-checked before moving on:

1. **`dashboard.html` redesign shipped** — first proposal was a dark
   "assay office" theme; human corrected it to light/white ("professional
   enterprise" meant white, not another dark theme). Rebuilt as a light
   "assay ledger" identity: white/near-white palette, a gold "Validation
   Seal" signature mark in the top bar, a left filter rail replacing the
   horizontal control bar. Verified in a real browser before shipping (not
   just `pytest`) — a throwaway local preview with sample data, screenshotted
   via the `claude-in-chrome` skill, never committed. Caught and fixed a
   real accessibility bug this way: Blizzard's item-quality colors were
   designed for dark panels and fail contrast on white (Common/white is
   literally invisible) — rarity now renders as a colored ring around the
   item icon (matching WoW's own UI convention) rather than the name's text
   color, which also naturally sidesteps the contrast problem since the
   ring shows against the icon's own artwork, not the page background.
   First attempt at this was a small separate dot, which the human said
   lost the "rarity at a glance" feel — revised to the icon-ring approach,
   then the ring itself needed thickening (24px/2px → 28-40px/3px) after
   still being hard to see.
2. **Dark mode toggle added** — same identity, a dark variant via
   `:root[data-theme="dark"]`, persisted to `localStorage`, pre-paint
   `<head>` script avoids a theme-flash. Reused most of the originally-
   proposed (then-rejected-as-default) dark palette as the toggle target.
3. **Loading indicator** on Refresh/auto-refresh — was previously silent,
   giving no feedback that a fetch was in flight.
4. **"Min sell realm low (g)" filter** — filters on the sell realm's
   current cheapest listing (distinct from the existing min/max buy-price
   filters, which filter what you'd spend, not what the sell realm asks).
5. **Profession tools excluded from "unique transmog"** — Mining Pick,
   Blacksmith Hammer, Fishing Pole, etc. trivially look "unique" by
   appearance-source count (few items share those slots) but aren't part of
   the visible paperdoll/transmog system at all. `item_names.NameCache`
   gained `.inventory_type()` (confirmed live against Blizzard's API) to
   check for `PROFESSION_TOOL`/`PROFESSION_GEAR` and exclude them.
6. **Background collection poll interval tightened** — the human noticed,
   from the live `/log` page itself, that Draenor's new AH data reliably
   lands within about a 1.5-minute band around :19-:20 past the hour (7
   consecutive real retrievals confirmed it). `dashboard.py`'s collection
   loop now polls every 45s (not the 10-min baseline) during a generous
   :12-:28 window, catching a real update within under a minute instead of
   up to 10. Falls back to the original 10-min cadence outside that window
   so total request volume for the other 44 minutes/hour barely changes.

A **future-work idea was captured, not built**: exporting selected snipes
as a TSM/Auctionator-format buylist — see "Future work" above.

Test suite: 110 → **154 passing** over the course of the session (new:
`tests/test_appearance.py`, `tests/test_market_key.py`, plus additions to
`test_snipe_check.py`/`test_dashboard.py`/`test_diff.py`/`test_item_names.py`).

### Session 2026-07-24: redesign rollout to the other 5 pages, transferability flag closed

Picking up "Next up" item #2 from the prior handoff. Two decisions from the
human at the start of this session, both closing open questions rather than
deferring them further:
- The Phase 3 "per-item transferability flag" question is **closed for
  good, no flag will be built** — every item this tool ever surfaces is by
  definition AH-listed, and an AH listing is unconditionally unsoulbound, so
  there's no non-AH item in scope for a Warbound/BoP distinction to matter.
  See `CLAUDE.md`'s "Transferability flag, resolved" note.
- Phase 2 (commodities feed) is explicitly out of scope, not just
  deprioritized.

**Redesign rollout, done**: `login.html`, `register.html`, `subscribe.html`,
`profile.html`, `log.html` rebuilt on the same light "assay ledger" tokens
and dark-mode toggle as `dashboard.html` (the light `--paper`/`--card`/
`--hairline`/`--ink`/`--bullion`/`--verified`/`--alert` palette, plus its
`:root[data-theme="dark"]` variant and pre-paint `<head>` script). All six
pages now read as one product instead of five old dark "Undermine cartel"
pages next to one redesigned dashboard. Specifics:
- `login.html`/`register.html`: minimal centered layout — brand/seal above
  the form card, theme toggle fixed top-right, no nav (nothing to navigate
  to pre-auth).
- `subscribe.html`: kept its pitch/steps/price-card structure exactly,
  restyled; the price figure now uses `--bullion` (the one signature gold
  accent) instead of a separate token.
- `profile.html`/`log.html`: gained a full `.topbar` matching
  `dashboard.html`'s (brand/seal, theme toggle, nav links), replacing their
  old single "&larr; Back to dashboard" text link.
- All five pages' JS/functional logic preserved exactly (same element ids,
  same fetch calls, same redirects) — only markup/CSS changed, same
  discipline as the original `dashboard.html` redesign.
- Theme choice is `localStorage`-backed and shared across all six pages via
  the same pre-paint script, so switching theme on one page carries across
  navigation to any other.

**Verified** the same way the original dashboard redesign was: a throwaway
local preview (this time all 5 files, `profile.html`/`log.html` had their
auth-gated `init()` fetches stubbed with sample data) served via
`python -m http.server`, screenshotted in both light and dark via the
`claude-in-chrome` skill, checked for contrast/consistency against
`dashboard.html` before considering it done. Never committed. No backend
touched; `pytest -q` stayed green (154 passing) throughout — none of these
files are covered by the test suite (only the API layer is asserted on).

### Session 2026-07-24, continued: client-side snipe filtering + item-class filter

Same day, picking up right after the redesign rollout above. Human's ask:
item-class filters (weapons/armor/containers/profession/housing/battle pets/
quest items/mounts), but flagged that the filter rail's underlying
architecture should be fixed first, since every new filter would otherwise
inherit the same problem — changing any threshold did nothing until the user
clicked Refresh (each click round-tripping to DuckDB), the same shape of lag
already fixed for column sorting in the earlier "Dashboard QoL pass."

**Architecture change**: `dashboard.html` now fetches one loose, generously-
sized batch per sell realm (`fetchBatch()`, `BATCH_TOP=2000` rows, `/api/
snipes` called with `min_discount=0`/`min_per_day=0`/no gold-or-appearance
narrowing) instead of a tightly-thresholded top-50. Every filter-rail
control — discount%, sales/day, gold range, sell-now, max-per-item, unique-
transmog, and the new item-class checkboxes — now re-filters that cached
batch entirely in the browser (`applyFilters()`/`renderTable()`), with zero
network round-trip. Only four things still fetch: switching the sell realm,
changing the item-id restriction, the 60s auto-refresh timer, or an explicit
Refresh click — all of which either change the underlying SQL candidate pool
or are an explicit "get fresh data now" request. Considered and rejected:
literally "load all possible snipes" with no floor at all — an unbounded
region-wide join could return tens of thousands of rows, which would make
the page slower rather than faster; `BATCH_TOP` is a deliberate, generous-
but-real cap instead.

**Item-class filter, the actual feature ask**: 8 checkboxes backed by
Blizzard's official (not guessed) `item_class`/`item_subclass` ids,
confirmed live via `GET /data/wow/item-class/index` — `item_names.NameCache`
gained `.item_class()`/`.item_subclass()` (same one-API-call-per-item cost
as the existing name/quality/level/inventory_type fields, since they're all
one combined fetch). Notably, **Housing (class 20) and Profession (class 19)
already exist as real, current Blizzard item classes** — no heuristic
guessing needed for either, confirmed by directly querying Blizzard's own
item-class index rather than assuming.

**One correctness subtlety**: making "Unique transmog only" fully
client-side needed a new `is_profession_item` field on each row
(`dashboard.py`), reproducing `snipe_check.find_snipes()`'s existing
`NON_TRANSMOG_INVENTORY_TYPES` exclusion exactly (a Mining Pick can have
`appearance_sources == 1` and still not be a real "unique transmog" in any
meaningful sense) — verified in the browser test below that this exclusion
still fires correctly under the new client-side path.

**Verified in a real browser**: a stubbed `dashboard.html` preview with 8
sample rows spanning every item class confirmed (a) every filter re-renders
instantly with no loading flicker, (b) the Mounts checkbox isolates exactly
the mount row, (c) "Unique transmog only" correctly excludes the profession-
tool sample row despite its `appearance_sources` looking unique, and (d) the
Refresh button's round-trip path doesn't throw (checked via the browser
console). No backend behavior changed for existing callers — `snipe_check.py`
CLI flags are untouched; only `dashboard.html`'s fetch strategy and three new
always-present `names=true` response fields
(`item_class`/`item_subclass`/`is_profession_item`).

Test suite: 154 → **160 passing** (new: `NameCache.item_class()`/
`.item_subclass()` coverage in `test_item_names.py`, plus `item_class`/
`item_subclass`/`is_profession_item` response-shape tests in
`test_dashboard.py`).

### Session 2026-07-24, continued again: localStorage batch cache + status-gated refresh

Same day, right after the item-class filter above. Human's follow-up ask:
avoid a full re-fetch on every page refresh, and only actually re-fetch when
Blizzard's AH data has genuinely changed rather than on a blind 60s timer.

`dashboard.html` now caches the last-fetched batch in `localStorage` (keyed
per sell realm + item-id restriction) and paints it instantly on page
load/realm switch — no more blank table during the network round trip. The
60s auto-refresh timer (and initial load) now calls a new `checkForUpdates()`
instead of fetching directly: it does one cheap `/api/status` check (a file
timestamp, no DuckDB join) and only runs the real, expensive `/api/snipes`
query when the sell realm's `last_modified` has actually advanced since the
cache — Blizzard republishes roughly hourly, so this cuts real query volume
from ~60 checks/hour down to ~1. A manual Refresh click, a realm switch, or
an items-csv change still always force a real fetch (explicit "give me
fresh/different data" actions). Cache is cleared on logout since
`localStorage` is per-browser, not per-account.

**Known, accepted tradeoff**: this gates purely on the sell realm's own
`last_modified`, so a fresh buy-side listing that appears on some other
scanned realm between sell-side updates won't trigger an automatic refresh
— same order of magnitude staleness (up to ~an hour) this product already
tolerates elsewhere; Refresh is always available for anyone who wants it
immediately.

**Verified with a real browser and a mocked `window.fetch`** (the actual
`fetchBatch()`/`checkForUpdates()` functions ran unmodified against canned
responses, not a facsimile) — confirmed exactly one real fetch on first
load, zero real fetches on a reload with unchanged mocked data, and exactly
one real fetch after flipping the mocked `last_modified`.

**UI fix in the same pass**: human flagged (from a live screenshot) that the
"Item class" checkbox group's heading had negative spacing crowding it
against the checkboxes — fixed with a top divider and real spacing. (A
second piece of feedback in that same message, about making the checkboxes
single-select, was explicitly retracted before being acted on — they stay
multi-select/OR'd together, as designed.)

No backend changed this round; `pytest -q` stayed green (160 passing)
throughout since this is a pure `dashboard.html` frontend change.

### Session 2026-07-25: market_key type 9 fix, tiered batch caps, free dashboard tier

New day, kicked off by a user-reported bad price on item 7761 (Steelclaw
Reaver, Silvermoon buy realm / Draenor sell realm).

**Live investigation** (via `railway ssh`, same technique as the earlier
production bug hunts): traced item 7761's full sale/listing history.
Draenor's sell-side data was **100% troll/camped-relist listings** — 3
"inferred sales" and all 4 currently-live listings priced at the same
~398,605g, regardless of a varying `m:9=NN` bonus modifier (25 through 90
observed). Pulling every region-wide listing for the item showed the real
range: 28,500g up to 1.7 million gold across dozens of realms — almost all
decoy pricing, with the genuinely cheap listings sitting at the bottom.
Human confirmed the item's actual cheap listings existed region-wide but
under a *different* `9=` value than what was on Draenor, so it never
matched at all — same root shape as the already-fixed 42/44 crafted-item
fragmentation, except this item isn't crafted (level 21 Rare weapon), so
the "crafted stat roll" explanation didn't apply. Human confirmed live
(before shipping, not assumed) that modifier 9 doesn't affect the item's
transmog appearance — safe to pool. `MARKET_IGNORE_MODIFIER_TYPES` extended
to `{9, 42, 44}` in both `fetch_snapshot.py` and the `analyze.py` SQL macro
mirror; new test vectors in `tests/test_market_key.py` using item 7761's
real bonus strings.

**Still open, not fixed this session**: even with pooling, if a sell
realm's *entire* history for an item is troll listings (as Draenor's was
here), there's no legitimate sell-side data to fall back to — `min_sales`
and the current-lowest-listing cap both assume some real data exists. A
region-wide cross-check against the buy-side listings data (already
collected) was discussed as the principled fix; parked, not built, flagged
in "Known gaps."

**Tiered batch caps + free dashboard tier**: the item 7761 investigation
prompted "how many snipes do we even load in" — until this session, a flat
`BATCH_TOP` for every account alike. Human's follow-up: make it tier-based,
and — confirmed explicitly as an intentional paywall change, not inferred —
let a logged-in-but-unsubscribed account preview the dashboard instead of
the previous hard `/subscribe` wall with zero preview. Shipped: 250 rows
(free/logged-in), 2000 (active subscription), 5000 (superuser). Backend
(`dashboard.py`): `/api/snipes`/`/api/realms`/`/api/status` switched from
`current_subscribed_user` to `current_active_user`; `_snipe_cap(user)`
clamps the real `top` server-side regardless of what's requested.
`auth.current_subscribed_user` itself stays defined, just unused for now.
Frontend (`dashboard.html`): the old subscribe-redirect gate in `init()` is
gone; `BATCH_TOP` raised 2000→5000 (the ceiling across every tier, server
enforces the real amount); the now-unreachable 402 handling in `fetchBatch()`
removed as dead code.

**"Top" display-cap control removed**, human's own follow-up once tiering
made the real row budget visible server-side — with client-side sorting
already in place, an extra "only show top N" truncation added a control
with no remaining purpose. Removed the input, its `LIVE_FILTER_IDS` entry,
and the capping logic in `renderTable()`.

**Tests**: `test_auth.py`'s subscription-gate test updated for the new
reachability (logged-in-unsubscribed now reaches business logic, 400 for an
uncollected realm, instead of the old 402); `test_dashboard.py` gained pure
unit coverage of `_snipe_cap()` for all three tiers plus a parametrized test
spying on `snipe_check.find_snipes` to confirm the real `top` value is
clamped regardless of what was requested. One now-obsolete test guarding
the deleted client-side redirect logic was removed outright. `pytest -q`:
167 passing.

**Verified in a real browser**: a mocked-fetch preview simulating a
logged-in, non-superuser, non-subscribed account confirmed it reaches the
dashboard and renders real rows (the actual behavior change), and that the
"Top" field and its logic are fully gone with no console errors.

**Same day, one more fix**: a user screenshot showed 8 separate table rows
for one item across 8 realms, all with byte-identical Sell p25/Sell realm
low numbers — the backend already knew these were one market
(`market_key()`), but `dashboard.html`'s row-grouping used the exact
`bonus_key` instead, which differs per listing instance even when the
market is the same. `market_key` was being computed for the join but
explicitly excluded from `find_snipes()`'s SQL output — stopped excluding
it, threaded it through `dashboard.py`'s response unconditionally, switched
`groupKey()` to use it. Verified live with a mocked preview reproducing the
exact reported shape (3 realms, identical sell-side numbers, different
exact bonus strings) — now correctly collapses into one expandable group.
`pytest -q`: 169 passing.

### Session 2026-07-25, continued: free-tier single-realm lock + /pricing page

Same day, human's follow-up on the free tier: "to minimize requests," a
free account should only be able to query one sell realm, not switch freely
like a subscriber can — plus a request to explain the tiers somewhere,
which turned into a dedicated `/pricing` page (confirmed via a quick
clarifying question rather than assumed: new page + route, not folded into
`/subscribe`).

**Shipped**: `db.User.locked_sell_realm` (new column + migration) — a
free-tier account gets locked to the first sell realm it ever queries via
`/api/snipes`; querying a different realm afterward returns 403 with an
upgrade message. Subscribers and superusers are never restricted.
`/api/me` now reports the lock so `dashboard.html` can disable the realm
dropdown and explain the restriction *before* the user hits a failed
request, not after. New public `static/pricing.html` (`GET /pricing`,
unauthenticated like `/log`) lays out Free (€0, 250 results, one locked
realm) vs Subscriber (€4.99/mo, 2,000 results, any realm) side by side with
an FAQ explaining the lock is about bounding real compute cost, not an
arbitrary paywall — and that both tiers read identical, equally-fresh data.
Linked from every page's nav. `subscribe.html`'s old feature-bullet list
(which described things the free tier now also has) was rewritten to focus
on what a subscription actually changes, plus the funding narrative,
linking to `/pricing` instead of duplicating the comparison.

**One real correctness subtlety**: `current_active_user`'s `user` and a
directly-injected `AsyncSession` share the same underlying SQLAlchemy
session within one request (FastAPI's per-request dependency caching), so
mutating `user.locked_sell_realm` and committing persists correctly without
an explicit `session.add()` — same pattern `billing.py`'s webhook already
uses, confirmed rather than assumed.

**Tests**: three new real-DB-persistence tests in `test_auth.py`
(deliberately not `test_dashboard.py` — its dependency-override pattern
bypasses the real session-backed user object the lock depends on, so it
can't actually prove persistence) covering free/subscribed/superuser
behavior, plus a `/pricing` reachability test. `pytest -q`: 173 passing.

**Verified in a real browser**: a mocked preview with a free-tier account
already locked to Draenor, where the server's own generic default pointed
at a *different* realm (Silvermoon) — confirmed the dropdown still locks to
Draenor specifically and is genuinely disabled, not just visually greyed.

**CI went red on push, fixed same-session**: `/api/snipes` now depends on
`get_async_session` directly (for the realm lock above), so FastAPI
resolves it on *every* request regardless of whether that request's code
path ever reaches the write branch. `test_dashboard.py` never overrode that
dependency the way `test_auth.py` does, so it fell through to the real
`get_async_session`, which needs `DATABASE_URL` — unset in CI, so every
`/api/snipes` test failed on dependency resolution alone (17 failures).
Passed locally only by accident: `.env` happens to have `DATABASE_URL` set
(pointing at a stopped local Postgres container), and SQLAlchemy engines
are lazy, so nothing ever actually tried to connect since none of those
tests exercise the write path. Fixed with the same throwaway-per-test-
SQLite override `test_auth.py` already uses; verified by re-running the
full suite with `DATABASE_URL` explicitly unset, matching CI exactly,
before pushing the fix. `pytest -q`: 173 passing, confirmed green in CI
(`aef383c`).

**Real bug caught live after deploying, fixed same-day**: human reported a
free-tier account got locked to a sell realm it never chose. Root cause:
`init()` pre-selected and auto-queried the server's site-wide default realm
before the human touched anything -- for an unlocked free-tier account,
that silent auto-fetch is exactly what set the lock. Not an old-vs-new-
account issue (confirmed) -- `locked_sell_realm` starts NULL for every
account regardless of age, so this hits anyone's first free-tier load.
Fixed with a `requirePick` mode on `populateRealmPicker()` (blank "Choose a
sell realm..." placeholder, no default) used only when free-tier and
unlocked, so nothing fetches until a genuine explicit selection is made.
Verified live with a mocked preview: zero `/api/snipes` calls on load
(previously 1, silently locking to the server default), exactly one call
after simulating a real dropdown pick, correctly locking to that realm
instead.

**Copy pass + contact info, same day**: reworded the "Up to N validated
snipes per query" tier copy on `pricing.html`/`subscribe.html` to "N snipes
in total, refreshes every hour when new AH data comes" (more accurate —
ties the number to the real hourly refresh cadence); removed redundant
bullets ("No card required", "Everything in Free"); removed an em-dash from
the funding sentence, split into two plain sentences. `log.html`'s lede
shortened from a 3-sentence explainer to one line. Added contact info
(email + Discord) to `subscribe.html`/`profile.html`/`pricing.html` —
scoped to pages that touch money/account state, not every page. Verified
all four pages in a real browser (a mocked `/api/me` for `profile.html`,
since it's auth-gated) before shipping.

**Second ilvl bug caught live, same day**: human reported "ilvl 3031" on a
real snipe (item 237468, Nightfall Executioner's Girdle). Traced live: base
level 610, so 3031 sits inside the existing 5x ratio guard (3050) added for
a different case (a classic item claiming ilvl 1112 vs base ~34) — the
ratio check alone wasn't tight enough for a high-base-level item. Every
live listing for this item carried modifier 28 set to only 3031 or 2462,
never near the real ~610 base, suggesting that modifier isn't ilvl at all
for this item's itemization. Fixed with a new `ILVL_ABSOLUTE_MAX = 1000` in
`dashboard.py`, ANDed with the existing ratio check — real WoW item levels
have never approached four digits, so this catches implausible claims on
high-base-level items the ratio check missed, while the ratio check still
covers low-base-level items an absolute cap alone wouldn't catch. New
regression test using this item's real numbers. `pytest -q`: 174 passing.

### Session 2026-07-25, continued again: type-28 conditional pooling (item 164353) + disk/retention investigation

Human found a cheaper Auchindoun listing for item 164353 (Plundered
Scalebane Claymore) not surfacing as a snipe against Argent Dawn despite
looking like an obvious discount, and asked whether it was a real data gap
or a limit cutting it off. Traced live: the listing was present, but
`market_key()` still matched on raw type-28 values, and this item had five
different type-28 values region-wide (186, 189, 645, 670, 289) against a
real catalog base level of 60 — junk, the same way item 237468's was, just
on an old Rare weapon instead of a modern raid item. Human's framing: ilvl
is transmog-irrelevant noise specifically for old BoE gear people snipe for
transmog, not a rule that holds for every item — so type 28 needed
*conditional* pooling (per-item, based on plausibility), not the
unconditional treatment 9/42/44 already get, since a genuinely different
ilvl on current-content gear is a genuinely different market.

**Shipped**: `market_key(bk, base_level=None)` gained an optional second
arg — when supplied, a type-28 value that fails `ilvl_plausible()` gets
pooled away same as 9/42/44; a plausible one is left untouched. Unknown
`base_level` (the default, and what `diff_snapshots.relist_key()` still
passes) means "don't strip," never "assume junk." `analyze.MARKET_KEY_MACRO_SQL`
rebuilt into three small macros mirroring this. `snipe_check.find_snipes()`
gained `_populate_base_levels()`, run once per call, which gathers every
candidate item with a type-28 modifier and resolves+caches its base level
via `NameCache` into a temp table the main query joins against — the one
place this pipeline can now make a network call where it never did before
(mitigated by the existing cache, so it's a one-time cost per new item).

**The multiplier itself needed tightening**: the existing 5x ratio
(`ILVL_PLAUSIBILITY_MULTIPLE`, from the item 237468 fix earlier the same
day) still didn't strip 186/189/289 for a base-60 item — only 645/670
exceeded it — so the conditional-pooling fix alone would have only
partially closed the bug. Compared 2x/3x/4x/5x against every known real
case; both 2x and 3x correctly stripped all known junk in both cases
(164353 and 237468) while leaving the one known-legitimate case (base 600,
claimed 636) untouched. Chose **3x** as the more conservative of the two
working values. `ILVL_PLAUSIBILITY_MULTIPLE` moved from `dashboard.py` into
`fetch_snapshot.py` (single source of truth, now that `market_key()` needs
it too, not just the dashboard's display logic). `tests/test_market_key.py`
encodes the real vectors from both items plus the legitimate case.
`pytest -q`: 225 passing (also verified against CI's exact environment,
`env -u DATABASE_URL pytest -q`, given this session's earlier CI incident).

**Disk usage / retention, investigated but not built**: same day, human
asked whether Railway's disk usage had been checked. Confirmed live:
`RETENTION_DAYS = 14` is real and running, but with only ~1.33 days of
actual history collected so far, extrapolating current growth to the full
14-day window projects to ~8.7GB — past the ~4.9GB practical cap on the
attached Volume. Asked the human two clarifying questions via
`AskUserQuestion`: confirmed the fix should tighten the existing day-based
retention (not switch to a different mechanism), and confirmed a target
budget of "up to 4-5GB," with 14 days as the hoped-for depth if it fits.
Proposed an adaptive approach — keep targeting 14 days by default, trim
more aggressively if total usage approaches a safety threshold (e.g.
4.5GB) — but this was explicitly parked, not implemented this session. See
`CLAUDE.md`'s matching section for the exact next-session starting point
(`collect_all.py`'s `prune_old_snapshots()`/`RETENTION_DAYS`).

### Session 2026-07-25, continued again: real production outage from the type-28 fix, fixed same session

Immediately after deploying the type-28 fix above, the human reported the
live site completely unresponsive for 5+ minutes. Confirmed independently
(`curl` from outside timed out; a direct request from *inside* the
container to its own listening port also timed out, while `railway status`
still reported the service "Online" the whole time — the platform's health
signal doesn't catch an application-level hang).

**Root cause**: `_populate_base_levels()` (the type-28 fix's own new code)
makes a real Blizzard API call per never-before-seen item, gathered
unconditionally across every sell realm's sales *and* the entire region's
buy-side listings. `dashboard.py`'s `api_snipes()` is an `async def` route
but called `find_snipes()` (and therefore this) directly on the event loop
thread — so the first real request after deploying, against a cold cache,
meant hundreds of sequential blocking HTTP calls with the *entire*
single-process server unable to answer anything else for the duration.

**Fixed the same session**: `api_snipes()` now runs the whole query via
`await asyncio.to_thread(...)`, so a slow first-time call no longer blocks
other requests; `_populate_base_levels()` also now saves its cache
incrementally (every 50 items) instead of only at the end, so an
interrupted run (exactly what happened when this fix itself deployed)
doesn't lose all its progress. Verified live: the homepage returned HTTP
200 in ~0.25-0.3s consistently, including while a deliberately-triggered
slow cold-cache run was in flight in parallel via `railway ssh` — the exact
condition that caused the outage. `pytest -q`: 225 passing (this was an
availability bug under load, not something the existing test suite would
have caught — noted in `CLAUDE.md` as a real gap).

### Stage 5 detail — hosting (done, Wait-for-CI verified 2026-07-23)

Live at `https://wow-project-production.up.railway.app`. Project
`valiant-peace` on Railway: `wow-project` service (built from our
`Dockerfile`) + a `Postgres` service (only holds the `user` table — the AH
data lives on a separate Volume attached to `wow-project` at `/app/data`,
unrelated to Postgres's own 5GB limit). `docker-entrypoint.sh` runs
`alembic upgrade head` before serving. Railway's "Wait for CI" is enabled
and confirmed working: a push now sits in `WAITING` until the GitHub
Actions check passes, then proceeds `BUILDING` → `DEPLOYING` → `SUCCESS`
automatically — a red CI run actually blocks the deploy now, not just runs
in parallel with it.

**Tooling note**: the Railway CLI's Windows binary is blocked by this
machine's Smart App Control (real, semi-irreversible-to-disable Windows 11
feature) — worked around by running the CLI inside a Docker container
instead (Linux binary, never touches that policy).

## Longer-term roadmap (beyond the hosted pivot)

| Phase | What it is | Status |
|---|---|---|
| 0 — Validate the sale-inference signal | 48h manual verification protocol | **Gated, skipped** (human decision 2026-07-20) — signal still unvalidated against real seller behavior. |
| 1 — Cross-realm engine + hardening | Region scanner, snipe-check, orchestration | **Mostly done.** Remaining: sell/scan realm config file, `--since` incremental diff. |
| 2 — Commodities feed | Region-wide, quantity-delta inference | **Out of scope** (human decision, 2026-07-24) — not being pursued. |
| 3 — Appearance layer | ItemModifiedAppearance scarcity mapping | **Groundwork started 2026-07-23**, ahead of Phase 1's remaining hardening (deliberate skip-ahead, see `CLAUDE.md`'s process-deviation notes). Done: itemId→appearance-rarity mapping (`appearance.py`), wired into `snipe_check.py`/dashboard as a "unique transmog" filter. Not done: static-API fallback, real obtainability flags (source_count is a rarity proxy, not a farmability check — known to diverge from Wowhead's own "same model as" data on at least one item), region-wide AH scarcity *of currently listed* appearances. The originally planned "warband transferability flag" is **closed, no flag will be built** (human decision 2026-07-24): every item this tool surfaces is by definition AH-listed, hence unconditionally unsoulbound — there's no non-AH item in scope for a Warbound/BoP distinction to ever matter. |
| 4 — Deal score + Discord alerts | Second paid feature | Not started — blocked on Phase 3 data. |
| 5 — Free companion addon | In-game tooltip overlay | Not started. The *web dashboard* half of this phase already shipped as part of the hosted pivot above; only the addon itself remains. |

**Phase 2 (commodities feed) is explicitly out of scope** (human decision, 2026-07-24) — not being pursued, not just deprioritized.

## Known gaps / risks

- **`b:` bonus-list noise pooling — resolved 2026-07-25, same day as the partial-fix entry this replaces.** The first attempt (a flat 5% frequency threshold) was live-confirmed insufficient: item 36507's own noise reached 14% frequency, overlapping real dimensions on other items also observed as low as 14-17%, so no single frequency band worked. Human asked for a real fix rather than accepting the gap. Replaced with a structural test — a value is real only if it has a *partner* (reliably co-occurs with another specific value, or belongs to a small mutually-exclusive set that jointly covers most of an item's listings), not if it merely clears a frequency bar. Validated live against 8 diverse real items (single-dimension noise, binary flags, N-way tier systems, multiple stacked real dimensions on one item) with zero misclassifications. See `CLAUDE.md`'s matching-logic section for the full investigation, including two earlier approaches (frequency-only, cardinality-only) that were tried and rejected before this one, and pending live re-verification once deployed.
- **`scan_region.sweep()` writes `data/listings/{cr}.parquet` non-atomically** — found by accident 2026-07-25 while live-verifying the base-level latency fix: a query reading that file at the exact moment a background sweep is overwriting it can hit a hard DuckDB read error (`Prefetch registered for bytes outside file`), not just a slow response. Rare window (a sweep's write is fast), but real and reproducible. Not fixed this session — the fix is straightforward (write to a temp file, then atomic rename) but out of scope for the latency work in progress. Worth doing before it's hit in front of a real user.
- Sale-inference classification (`inferred_sale` especially) has never been checked against real seller behavior. Partial mitigation added 2026-07-23: `snipe_check.find_snipes()`'s `min_sales` floor (default 2) stops a single unverified sample from becoming the whole sold-price percentile, after exactly that happened live (item 15138) — but two bad samples can still both be false positives, so this reduces rather than closes the risk.
- No sell/scan realm config file — `--exclude`/`--items` CLI flags are the manual stand-in.
- The AH `modifiers` type-28 field ("item level") isn't Blizzard-documented; the dashboard sanity-checks it against the item's catalog level, and `market_key()` now conditionally pools implausible values into matching too (2026-07-25, `ILVL_PLAUSIBILITY_MULTIPLE = 3x`), but the underlying meaning is still community-sourced, not official. Same caveat applies to modifier types 9/42/44 (`market_key()`'s unconditional ignore-list) — inference from real data, not documented facts (type 9 was additionally human-confirmed not to affect transmog before pooling, 2026-07-24).
- **No test coverage for "does an async route block the event loop"** — the type-28 fix's outage (2026-07-25, fixed same session, see "Session 2026-07-25, continued again" above) was an availability bug the existing test suite couldn't have caught, since `TestClient` calls are synchronous and don't exercise concurrent-request behavior. Worth a regression test (e.g. a slow stub swapped into `_populate_base_levels()`, asserting a concurrent request to a lightweight route still completes quickly) if this class of bug recurs — not built yet.
- **Disk usage will exceed the Railway Volume's practical ~4.9GB cap at the current `RETENTION_DAYS = 14`** — confirmed 2026-07-25 by extrapolating early growth (~1.33 days of history projects to ~8.7GB at full retention). An adaptive retention scheme (target 14 days, trim harder if usage nears a safety threshold) was proposed and confirmed in direction by the human but not built — see `collect_all.py`'s `prune_old_snapshots()`/`RETENTION_DAYS` for the starting point.
- **If a sell realm's entire observed history for an item is troll/camped listings, no existing guard can rescue the estimate** (found live 2026-07-24, item 7761/Steelclaw Reaver on Draenor: all 3 "sales" and all 4 current listings were the same ~398,605g decoy price). `min_sales` and the current-lowest-listing cap both assume *some* legitimate data exists on the sell realm to fall back to — when literally everything observed is bogus, there's nothing to fall back to. A region-wide cross-check against `data/listings/*.parquet` (already collected for the buy side) was proposed as the principled fix but not built — parked, same status as the camped-relist window bug above.
- A camped-relist can still occasionally slip through the relist-matching window and get misclassified as `inferred_sale` (seen live on item 238014's `29=77` sub-variant, 2026-07-23 evening) — separate from, and not fixed by, the same-day crafted-item pooling fix. No mitigation yet beyond the existing `min_sales` floor.
- `appearance.py`'s rarity signal (`source_count`) is known to diverge from Wowhead's own "same model as" data on at least one item (14042) — see "Evening session" above. No Wowhead API exists to reconcile against.
- The tightened background-poll window (`:12-:28` past the hour, `dashboard.py`) is based on ~7 observed data points from one realm (Draenor). It's a shared, global schedule across every deep-collected realm, not learned per-realm — other realms may publish at a different offset that the window doesn't cover as tightly. Revisit with per-realm learned offsets if `/log` history across more realms shows the window is miscalibrated.
- Stripe is on the full `sk_live_...` secret key, not a restricted key scoped to what `billing.py` actually needs (see "Next up").

## What's built (file-level)

| Component | File(s) |
|---|---|
| Sale-inference engine (core IP) | `diff_snapshots.py` |
| Realm collector | `fetch_snapshot.py` (called by `collect_all.py`, not run standalone anymore) |
| Region scanner | `scan_region.py` |
| Snipe-check logic | `snipe_check.py` |
| Server-side collection | `collect_all.py` |
| Web dashboard | `dashboard.py`, `static/dashboard.html` |
| Auth | `auth.py`, `db.py`, `static/login.html`/`register.html` |
| Billing | `billing.py`, `static/subscribe.html` |
| Item name/icon/quality cache | `item_names.py` |
| Appearance-rarity cache (Phase 3 groundwork) | `appearance.py` |
| Public retrieval-time log | `static/log.html`, `GET /api/log`/`GET /api/log/realms` in `dashboard.py` |
| Hosting | `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore` |
| Tests | `tests/` — 154 passing (`pytest -q`), no external services needed |

## Where to look for more

- `CLAUDE.md` — architecture, conventions, full roadmap, Blizzard API facts (authoritative).
- `README.md` — human-facing setup/usage.
- `git log` — commit-level history.
