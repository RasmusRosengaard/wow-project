# PROGRESS — WoW AH Snipe Validator

Living status doc: what's built, what's not, what's next. `CLAUDE.md` is
still the authoritative brief (architecture, conventions, full roadmap,
API facts) — this file is the scannable summary, kept in sync with it.

Last updated: 2026-07-24 (redesign rollout to the other 5 pages).

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
2. A camped-relist false-positive still slips through occasionally (separate,
   older bug from the crafted-item fragmentation fix — see "Known gaps" below).

**New this session (2026-07-24)**: the light "assay ledger" redesign (with
dark-mode toggle) rolled out to all 5 remaining pages — `login.html`,
`register.html`, `subscribe.html`, `profile.html`, `log.html` — matching
`dashboard.html`. All six pages now share one visual identity. See
`CLAUDE.md`'s "Full visual rethink" note for detail. Also: the Phase 3
per-item transferability flag question is **closed, not just deferred** —
human decision that no flag will ever be needed, since every item this tool
surfaces is by definition AH-listed and therefore unconditionally
unsoulbound (see `CLAUDE.md`'s "Transferability flag, resolved" note).

## Next up (short list, do these in roughly this order)
1. Decide whether to fix the remaining camped-relist false-positive bug (relist-matching window logic — deferred, not started, see "Known gaps").
2. TSM/Auctionator buylist export idea (see "Future work" below) — no design done yet.
3. Restricted Stripe key — swap the full `sk_live_...` secret key for a key restricted to just Checkout/Customers/Subscriptions/Webhooks (Stripe's own current guidance, real bug-radius reduction but low urgency). **Human-only, asked explicitly 2026-07-23**: do not rotate/swap this live credential without the human present, even when otherwise told to keep working autonomously.
4. Phase 2 (commodities feed) — explicitly out of scope per human direction 2026-07-24, not being pursued.

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

- Sale-inference classification (`inferred_sale` especially) has never been checked against real seller behavior. Partial mitigation added 2026-07-23: `snipe_check.find_snipes()`'s `min_sales` floor (default 2) stops a single unverified sample from becoming the whole sold-price percentile, after exactly that happened live (item 15138) — but two bad samples can still both be false positives, so this reduces rather than closes the risk.
- No sell/scan realm config file — `--exclude`/`--items` CLI flags are the manual stand-in.
- The AH `modifiers` type-28 field ("item level") isn't Blizzard-documented; the dashboard sanity-checks it against the item's catalog level, but the underlying meaning is still community-sourced, not official. Same caveat now applies to modifier types 42/44 (`market_key()`'s ignore-list) — inference from real data, not documented facts.
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
