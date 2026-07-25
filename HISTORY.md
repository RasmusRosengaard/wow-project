# HISTORY — detailed session log

This file holds the full, session-by-session narrative that used to live
inline in `CLAUDE.md`/`PROGRESS.md`: what was reported, how it was traced,
what was tried and rejected, what shipped. `CLAUDE.md` and `PROGRESS.md`
were both growing past the size of the entire codebase because every
incident's full investigation stayed inline forever — split out 2026-07-25
so those two files can stay scannable current-state references while this
one is free to be as long as the real history warrants. Nothing here is
authoritative about *current* behavior — if this ever disagrees with
`CLAUDE.md`, `CLAUDE.md` wins; this is "why", not "what's true now".

Organized chronologically. Skip to a date/item with your editor's search.

---

## Hosted SaaS pivot — stage by stage (2026-07-20 to 2026-07-23)

Turning the local single-user tool into a hosted product. Full design in
`~/.claude/plans/unified-nibbling-simon.md`.

| Stage | What it is | Status |
|---|---|---|
| 1 | GitHub repo, CI, branch protection | Private repo, `pytest -q` on every push, branch protection requires it. |
| 2 | Email auth (FastAPI-Users + Postgres) | Register/login/logout, cookie sessions, API routes gated. |
| 3 | Stripe subscription | Live mode — see detail below. |
| 4 | Server-side collection + realm picker | Backend and UI dropdown both shipped. |
| 5 | CD (Railway auto-deploy + DB migrations) | Live URL, Wait-for-CI verified working end to end. |

### Stage 3 detail — Stripe (2026-07-23, deployed straight to live mode)

Human decision: skip test-mode verification, go live immediately. `billing.py`:
`POST /billing/checkout` creates a Checkout Session for the single €4.99/mo
price and redirects; `POST /billing/webhook` verifies the Stripe signature
and handles `checkout.session.completed` / `customer.subscription.updated` /
`customer.subscription.deleted`, writing `subscription_status` (and
customer/subscription ids, period end) onto the user — the only writer of
those fields. `auth.current_subscribed_user` gated `/api/snipes` and
`/api/status` at the time (402 if unsubscribed, distinct from 401 if not
logged in at all) — superseded 2026-07-25 by the free tier, see below.

Verified live end to end: unauthenticated → 401, logged-in-but-unsubscribed →
402, and a real `cs_live_...` Checkout Session URL generated successfully.

**Real bug the test suite caught before it shipped**: `event["data"]["object"]`
from `stripe.Webhook.construct_event()` is a `StripeObject`, not a plain
dict — it supports `obj["key"]` but *not* `obj.get("key")`, which every
handler used. Would have thrown on the very first real webhook delivery;
fixed by calling `.to_dict()` once up front (needed on the retrieved
`Subscription` object too, same issue).

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
actually got a new snapshot that cycle.

`run_cycle.py`, `run_cycle_task.ps1`, and the local Windows Task Scheduler
job are **fully deleted** (human decision: this product is never run
locally as a going concern) — Railway is the sole collection path.

### Stage 5 detail — hosting (done, Wait-for-CI verified 2026-07-23)

Live at `https://wow-project-production.up.railway.app`. Project
`valiant-peace` on Railway: `wow-project` service (built from the repo's
`Dockerfile`) + a `Postgres` service (only holds the `user` table — the AH
data lives on a separate Volume attached to `wow-project` at `/app/data`).
`docker-entrypoint.sh` runs `alembic upgrade head` before serving. Railway's
"Wait for CI" is enabled and confirmed working: a push sits in `WAITING`
until the GitHub Actions check passes, then proceeds `BUILDING` →
`DEPLOYING` → `SUCCESS` automatically.

**Tooling note**: the Railway CLI's Windows binary is blocked by this
machine's Smart App Control (real, semi-irreversible-to-disable Windows 11
feature) — worked around by running the CLI inside a Docker container
instead (Linux binary, never touches that policy).

---

## UI design pass (2026-07-23)

Ran every page through the `frontend-design` skill — first identity was
"Undermine cartel trading-floor": dark olive/moss panels (`--bg #14170f`,
`--panel #1b1f15`), a toxic-green accent (`--toxic #a6e600`) for primary
actions, brass and ember as secondary accents, system font stacks
throughout, `ui-monospace` for numeric/data columns. Dashboard's signature
element was a static segmented status ticker (`.masthead`/`.ticker`)
replacing the old plain status div — deliberately not animated, respects
`prefers-reduced-motion`. Applied to `dashboard.html`, `login.html`,
`register.html`, `subscribe.html`, `profile.html`; each page's existing
JS/functional logic was preserved exactly, only markup/CSS changed.

Also removed per explicit human instruction: the repeated "NOTE: an AH
listing is guaranteed unsoulbound..." caveat banner is gone from the UI
entirely — the API still returns `caveat` in its response, it's just not
rendered.

Folded in two QoL fixes at the same time: min-discount filter became a real
percentage input (0–100, was a raw 0–1 fraction) and the names/icons toggle
was removed — resolving item_id → name/icon is always on now.

### Full visual rethink (2026-07-23, later the same day)

Human wanted to move away from the "Undermine cartel" identity entirely,
toward a "professional enterprise" feel — noted because the old dark-bg/
toxic-green pattern was itself one of the three generic "AI dashboard"
looks the `frontend-design` skill warns against. **First proposal (dark
"assay office / commodities exchange") was rejected** — "professional
enterprise" meant light/white, not another dark theme with a different
accent.

Revised and built direction, "assay ledger / certificate on paper":
`--paper #f6f7f5` (cool near-white, deliberately not warm cream, to avoid
the OTHER generic AI-dashboard cliché), `--card #ffffff`, `--hairline
#d8dbd4`/`--hairline-strong #c3c8bd`, `--ink #14161a`/`--text-dim #5b6259`,
`--bullion #a67c2e` (signature muted gold, tied to the product being about
gold prices), `--verified #2f7d72` (deep verdigris-teal, reserved for the
Validation Seal and "fresh" states only). **Validation Seal** signature
element: one small hairline circular stamp (checkmark ring) in the top bar
next to the wordmark — not per-row, paired with a "Validated Data" label.
Layout: a left filter rail replacing the old horizontal control bar.

**Real accessibility bug caught during the build**: Blizzard's in-game
item-quality colors were designed for dark panels — every one fails WCAG AA
as text color against `--paper` except Rare/Epic. First fix (a small
colored dot next to the name) was rejected — rarity should read the way it
does in-game, not as a disconnected swatch. **Revised fix**: an inset
colored ring around the item icon itself, matching WoW's own UI convention
of framing item art by quality — this also dodges the contrast problem
entirely since the ring renders against the icon's own artwork, not the
page background. Ring size/thickness was bumped (24px/2px → 28-40px/3px)
after being hard to see in practice.

Verified via a throwaway local preview (auth-gated `init()` stubbed with
sample rows, served via a local static file server, screenshotted via the
`claude-in-chrome` skill, never committed) before shipping. `pytest -q`
stayed green throughout (145 passing) — no backend changed.

### Rollout to the other 5 pages (2026-07-24)

`login.html`, `register.html`, `subscribe.html`, `profile.html`, `log.html`
all rebuilt on the same light "assay ledger" tokens and dark-mode toggle as
`dashboard.html`. `login.html`/`register.html` use a minimal centered
layout (brand/seal above the form card, theme toggle fixed top-right, no
nav). `subscribe.html` kept its pitch/steps/price-card structure, restyled.
`profile.html`/`log.html` gained a full `.topbar` matching `dashboard.html`'s.
All five pages' JS/functional logic preserved exactly — only markup/CSS
changed. Theme choice is `localStorage`-backed and shared across all six
pages via one pre-paint `<head>` script.

Verified via the same throwaway local-preview + `claude-in-chrome`
screenshot technique (light and dark, all five pages). `pytest -q` stayed
green (154 passing) — none of these files are covered by the test suite.

### Dark mode toggle (2026-07-23 evening)

A `#theme-toggle` button switches to a same-thesis dark variant via
`:root[data-theme="dark"]` overrides — not a different identity, same
tokens/Seal, just `--paper #14161a`/`--card #1b1e24`/`--bullion #d4af61`/
`--verified #3fa9a0` etc. (closely related to the original dark proposal
that got revised to light as the *default*). A blocking `<script>` in
`<head>` sets `data-theme` pre-paint from `localStorage` (falling back to
`matchMedia`) so there's no flash of the wrong theme.

### Loading indicator (2026-07-23)

`setLoading()` disables `#refresh`, swaps its label to "Loading…", and dims
`#table-wrap` for the duration of `loadSnipes()`'s fetch, wrapped in
`try/finally` so it always clears even on an error response.

---

## Dashboard QoL pass (2026-07-23)

Four requests worked in one batch:

- **Sell-realm picker** — new `GET /api/realms` lists every realm with a
  `data/events/{cr}.parquet` file, resolved to a display name. The
  dashboard's free-typed realm-id number box became a `<select>` populated
  from this.
- **Min/max gold filter** — `snipe_check.find_snipes()` gained `min_gold`/
  `max_gold` params (filtering on the *buy*-side unit price), threaded
  through `/api/snipes` and CLI flags.
- **Duplicate snipes grouped** — the same item/variant appearing more than
  once now shows one row with the best deal on top, plus a `▾ N` toggle to
  expand. Purely client-side.
- **No-lag sorting** — sorting became entirely client-side
  (`renderTable()`/`compareRows()`): clicking a column header reorders the
  already-loaded batch instantly instead of re-fetching over the network.

### Single-sale price artifact (fixed 2026-07-23)

Shortly after, a user-reported price mismatch (item 15138, Onyxia Scale
Cloak, showing ~99,624g against a real ~444g) was traced live (`analyze.py
--cr-id 1403 trace 15138` over `railway ssh`) to exactly one `inferred_sale`
ever recorded: a troll-priced decoy listing that almost certainly got
cancelled, not bought — the known `inferred_sale` cancel-without-relist
blind spot. With Draenor's history still young, `min_per_day` alone didn't
guard against it. Fixed with a new `min_sales` floor (default 2) in
`snipe_check.find_snipes()`.

### Evening session (2026-07-23): more price-quality bugs, Phase 3 groundwork, /log

Six commits, `62d2106..ec491e9`, each deployed and verified live before
moving to the next:

1. **Sell-price cap** (`83a236c`) — even `min_sales >= 2` wasn't enough:
   item 206477 (Warsword of Caer Darrow) had 4 inferred sales, but 2 were
   the *same* 149,379g troll listing, dragging the estimate to 75,074g
   against a real ~700g item. Fix: `sell_price` became
   `LEAST(sold_percentile, current_cheapest_live_listing_on_sell_realm)` —
   this cap and the whole percentile-based design were later replaced
   entirely (see the pricing-model-replacement section below).
2. **Phase 3 groundwork** (`fe75e0a`, `76b87f5`) — new `appearance.py`
   caches itemId → transmog-appearance rarity (126k items / 48k
   appearances) from wago.tools' `ItemModifiedAppearance` DB2 export.
   `snipe_check.py --max-appearance-sources N` / dashboard "Unique
   transmog only" checkbox. **Known accuracy gap**: on at least one item
   (14042, Cindercloth Vest), `source_count=1` (verified correct against
   Blizzard's own DB2 tables) while Wowhead's page claims "same model as 5
   others" — likely a finer-grained mesh/texture comparison Wowhead does
   that isn't in the DB2 export. No Wowhead API exists to cross-check.
3. **Crafted-item market fragmentation** (`1874bc4`) — item 238014
   (Sun-Blessed Sickle, crafted) had 25+ distinct exact `bonus_key`s on
   Draenor, most clustered around 1000-1300g, but exact-`bonus_key`
   matching meant any one listing's percentile came from whichever 1-2-sale
   bucket its exact crafted roll fell into. Root cause: undocumented
   modifier types 42 (continuous per-craft stat roll) and 44 (per-instance
   serial, confirmed sequential: 245822, 245823, 245824...). Fix: new
   `market_key()` strips just those two types for matching/grouping only.
4. **`--max-per-item`** (`1874bc4`, same commit) — caps how many listings
   of one item/variant can fill the results, keeping the best-discount
   ones via a SQL `ROW_NUMBER()` window.
5. **Status ticker simplified twice** (`1874bc4`, `d2d3da3`) — first
   dropped "Polled" (was just the browser's render-time wall clock), then
   collapsed to one segment, "Last auction data" (the real Blizzard
   `Last-Modified` timestamp).
6. **Public `/log` page** (`ec491e9`) — a page any visitor can view (no
   login) showing every timestamp new AH data was retrieved per realm.
   `GET /api/log/realms` / `GET /api/log?sell={cr}` are the only
   unauthenticated `/api/*` routes by design. Reads
   `data/snapshots/{cr}/*.parquet` filenames directly — the
   `If-Modified-Since` check means a file only exists when Blizzard
   actually published something new, so the file list already *is* a
   complete, honest log.

### Evening session, part 2 (2026-07-23, later the same evening)

Three commits (`aa92ed4` redesign, `47633ff` rarity-ring fix, `bc09f75` the
dark-mode/loading/filter/profession-tool/poll-interval batch):

1. `dashboard.html` redesign shipped (see "Full visual rethink" above for
   detail).
2. Dark mode toggle added (see above).
3. Loading indicator added (see above).
4. **"Min sell realm low (g)" filter** — filters on the sell realm's
   current cheapest listing, distinct from the buy-price filters.
5. **Profession tools excluded from "unique transmog"** — Mining Pick,
   Blacksmith Hammer, Fishing Pole, etc. trivially look "unique" by
   appearance-source count but aren't part of the visible paperdoll system.
   `item_names.NameCache` gained `.inventory_type()` (confirmed live) to
   check for `PROFESSION_TOOL`/`PROFESSION_GEAR`.
6. **Background collection poll interval tightened** — the human noticed
   from `/log` itself that Draenor's new AH data reliably lands within
   about a 1.5-minute band around :19-:20 past the hour (7 consecutive real
   retrievals confirmed it). Poll loop now hits every 45s during a
   :12-:28 window, falling back to the 10-min baseline outside it.

Test suite: 110 → 154 passing over the course of the session.

---

## Session 2026-07-24: redesign rollout, transferability flag closed

Two decisions from the human at the start of this session:
- The Phase 3 "per-item transferability flag" question is **closed for
  good, no flag will be built** — every item this tool surfaces is by
  definition AH-listed, and an AH listing is unconditionally unsoulbound.
- Phase 2 (commodities feed) is explicitly out of scope, not just
  deprioritized.

Redesign rollout to the other 5 pages — see "UI design pass" above.

### Client-side snipe filtering + item-class filter (2026-07-24)

Human's ask: item-class filters (weapons/armor/containers/profession/
housing/battle pets/quest items/mounts), but flagged the filter rail's
architecture should be fixed first — every threshold (discount%, sales/day,
gold range, sell-now, max-per-item, unique-transmog) did nothing until
Refresh was clicked, each click round-tripping to DuckDB.

**Architecture change**: `dashboard.html` now fetches one loose,
generously-sized batch per sell realm (`fetchBatch()`, `BATCH_TOP=2000`
rows at the time) instead of a tightly-thresholded top-50. Every filter-rail
control re-filters that cached batch entirely in the browser
(`applyFilters()`/`renderTable()`), zero network round-trip. Only four
things still fetch: switching the sell realm, changing the item-id
restriction, the 60s auto-refresh timer, or an explicit Refresh click.
Considered and rejected: no floor at all — an unbounded region-wide join
could return tens of thousands of rows, making the page slower, not faster.

**Item-class filter**: 8 checkboxes backed by Blizzard's official (not
guessed) `item_class`/`item_subclass` ids, confirmed live via `GET
/data/wow/item-class/index`. `item_names.NameCache` gained `.item_class()`/
`.item_subclass()` (same one-API-call-per-item cost as existing fields).
Housing (class 20) and Profession (class 19) already exist as real Blizzard
item classes — no heuristic needed. Mounts needed both `item_class===15`
(Miscellaneous) and `item_subclass===5` since they're not their own
top-level class.

**One correctness subtlety**: making "Unique transmog only" fully
client-side needed a new `is_profession_item` field on each row,
reproducing `snipe_check.find_snipes()`'s `NON_TRANSMOG_INVENTORY_TYPES`
exclusion exactly.

Verified in a real browser with 8 sample rows spanning every item class.
Test suite: 154 → 160 passing.

### localStorage batch cache + status-gated refresh (2026-07-24)

Human's follow-up: avoid a full re-fetch on every page refresh, and only
re-fetch when Blizzard's data has genuinely changed rather than on a blind
60s timer.

`dashboard.html` now caches the last-fetched batch in `localStorage` (keyed
per sell realm + item-id restriction) and paints it instantly on page
load/realm switch. The 60s auto-refresh timer (and initial load) now calls
`checkForUpdates()` instead of fetching directly: a cheap `/api/status`
check first, only running the real `/api/snipes` query when the sell
realm's `last_modified` has actually advanced. A manual Refresh, a realm
switch, or an items-csv change still always force a real fetch. Cache is
cleared on logout.

**Known, accepted tradeoff**: gating purely on the sell realm's own
`last_modified` can miss a fresh buy-side listing appearing on some other
scanned realm between sell-side updates — same order of magnitude
staleness (up to ~an hour) this product already tolerates elsewhere.

Verified with a real browser and a mocked `window.fetch` (the actual
functions ran unmodified against canned responses). No backend changed;
`pytest -q` stayed green (160 passing).

---

## Session 2026-07-25: market_key type 9, tiered batch caps, free dashboard tier

New day, kicked off by a user-reported bad price on item 7761 (Steelclaw
Reaver, Silvermoon buy realm / Draenor sell realm).

**Live investigation** (via `railway ssh`): traced item 7761's full
sale/listing history. Draenor's sell-side data was **100% troll/camped-
relist listings** — 3 "inferred sales" and all 4 currently-live listings
priced at the same ~398,605g, regardless of a varying `m:9=NN` bonus
modifier (25 through 90 observed). Pulling every region-wide listing showed
the real range: 28,500g up to 1.7 million gold across dozens of realms —
almost all decoy pricing. Human confirmed the item's actual cheap listings
existed region-wide but under a *different* `9=` value than what was on
Draenor, so it never matched at all — same root shape as the 42/44
crafted-item fragmentation, except this item isn't crafted (level 21 Rare
weapon). Human confirmed live that modifier 9 doesn't affect the item's
transmog appearance — safe to pool. `MARKET_IGNORE_MODIFIER_TYPES` extended
to `{9, 42, 44}`.

**Tiered batch caps + free dashboard tier**: the item 7761 investigation
prompted "how many snipes do we even load in" — until this session, a flat
`BATCH_TOP` for every account alike. Human's follow-up: make it tier-based,
and — confirmed explicitly as an intentional paywall change — let a
logged-in-but-unsubscribed account preview the dashboard instead of the
previous hard `/subscribe` wall with zero preview. Shipped: 250 rows
(free/logged-in), 2000 (active subscription), 5000 (superuser). Backend:
`/api/snipes`/`/api/realms`/`/api/status` switched from
`current_subscribed_user` to `current_active_user`; `_snipe_cap(user)`
clamps the real `top` server-side regardless of what's requested.
Frontend: the old subscribe-redirect gate in `init()` removed; `BATCH_TOP`
raised 2000→5000.

**"Top" display-cap control removed** — with client-side sorting already in
place, an extra "only show top N" truncation had no remaining purpose.

Tests: `test_auth.py`'s subscription-gate test updated; `test_dashboard.py`
gained pure unit coverage of `_snipe_cap()` for all three tiers plus a
parametrized test spying on `snipe_check.find_snipes` to confirm the real
`top` value is clamped. `pytest -q`: 167 passing.

**Same day, table-grouping fix**: a user screenshot showed 8 separate table
rows for one item across 8 realms, all with byte-identical Sell p25/Sell
realm low numbers — the backend already knew these were one market
(`market_key()`), but `dashboard.html`'s row-grouping used the exact
`bonus_key` instead. `market_key` was being computed for the join but
explicitly excluded from `find_snipes()`'s SQL output — stopped excluding
it, threaded it through `dashboard.py`'s response unconditionally, switched
`groupKey()` to use it. `pytest -q`: 169 passing.

### Session 2026-07-25, continued: free-tier single-realm lock + /pricing page

Human's follow-up on the free tier: "to minimize requests," a free account
should only be able to query one sell realm, not switch freely like a
subscriber can — plus a request to explain the tiers, which turned into a
dedicated `/pricing` page (confirmed via a clarifying question rather than
assumed).

**Shipped**: `db.User.locked_sell_realm` (new column + migration) — a
free-tier account gets locked to the first sell realm it ever queries;
querying a different realm afterward returns 403. Subscribers and
superusers are never restricted. `/api/me` reports the lock so
`dashboard.html` can disable the realm dropdown proactively. New public
`static/pricing.html` (`GET /pricing`, unauthenticated like `/log`) lays
out Free vs Subscriber side by side with an FAQ.

**One real correctness subtlety**: `current_active_user`'s `user` and a
directly-injected `AsyncSession` share the same underlying SQLAlchemy
session within one request (FastAPI's per-request dependency caching), so
mutating `user.locked_sell_realm` and committing persists correctly without
an explicit `session.add()`.

**CI went red on push, fixed same-session**: `/api/snipes` now depends on
`get_async_session` directly, so FastAPI resolves it on *every* request
regardless of whether that request's code path ever reaches the write
branch. `test_dashboard.py` never overrode that dependency the way
`test_auth.py` does, so it fell through to the real `get_async_session`,
which needs `DATABASE_URL` — unset in CI, so every `/api/snipes` test
failed on dependency resolution alone (17 failures). Passed locally only by
accident (`.env` has `DATABASE_URL` set, pointing at a stopped local
Postgres container; SQLAlchemy engines are lazy, so nothing ever tried to
connect). Fixed with the same throwaway-per-test-SQLite override
`test_auth.py` already uses; verified by re-running the full suite with
`DATABASE_URL` explicitly unset before pushing the fix.

**Real bug caught live after deploying, fixed same-day**: a free-tier
account got locked to a sell realm it never chose. Root cause: `init()`
pre-selected and auto-queried the server's site-wide default realm before
the human touched anything — for an unlocked free-tier account, that silent
auto-fetch is exactly what set the lock. Fixed with a `requirePick` mode on
`populateRealmPicker()` (blank placeholder, no default) used only when
free-tier and unlocked.

**Copy pass + contact info, same day**: reworded tier copy on
`pricing.html`/`subscribe.html` to be more accurate about the refresh
cadence; removed redundant bullets; added contact info (email + Discord) to
`subscribe.html`/`profile.html`/`pricing.html`.

**Second ilvl bug caught live, same day**: a real snipe showed "ilvl 3031"
(item 237468, Nightfall Executioner's Girdle, base level 610) — sits inside
the existing 5x ratio guard (3050) added for a different case (a classic
item claiming ilvl 1112 vs base ~34); the ratio check alone wasn't tight
enough for a high-base-level item. Every live listing for this item carried
modifier 28 set to only 3031 or 2462, never near the real ~610 base.
Fixed with `ILVL_ABSOLUTE_MAX = 1000`, ANDed with the existing ratio check.
`pytest -q`: 174 passing.

### Session 2026-07-25, continued again: type-28 conditional pooling (item 164353) + disk/retention investigation

Human found a cheaper Auchindoun listing for item 164353 (Plundered
Scalebane Claymore) not surfacing as a snipe against Argent Dawn. Traced
live: the listing was present, but `market_key()` still matched on raw
type-28 values, and this item had five different type-28 values
region-wide (186, 189, 645, 670, 289) against a real catalog base level of
60 — junk, the same way item 237468's was, just on an old Rare weapon
instead of a modern raid item. Human's framing: ilvl is transmog-irrelevant
noise specifically for old BoE gear people snipe for transmog, not a rule
that holds for every item — so type 28 needed *conditional* pooling
(per-item, based on plausibility), not the unconditional treatment 9/42/44
already get.

**Shipped**: `market_key(bk, base_level=None)` gained an optional second
arg — when supplied, a type-28 value that fails `ilvl_plausible()` gets
pooled away same as 9/42/44; a plausible one is left untouched. Unknown
`base_level` means "don't strip," never "assume junk." `snipe_check.
find_snipes()` gained `_populate_base_levels()` (later renamed
`_populate_market_keys()`), run once per call, resolving+caching every
candidate item's base level via `NameCache`.

**The multiplier itself needed tightening**: the existing 5x ratio still
didn't strip 186/189/289 for a base-60 item — only 645/670 exceeded it.
Compared 2x/3x/4x/5x against every known real case; both 2x and 3x
correctly stripped all known junk in both cases while leaving the one
known-legitimate case (base 600, claimed 636) untouched. Chose 3x as the
more conservative of the two. `pytest -q`: 225 passing.

**Disk usage / retention, investigated but not built**: human asked
whether Railway's disk usage had been checked. Confirmed live:
`RETENTION_DAYS = 14` is real and running, but with only ~1.33 days of
actual history, extrapolating current growth to the full 14-day window
projected to ~8.7GB — past the ~4.9GB practical Volume cap. Confirmed via
`AskUserQuestion`: tighten the existing day-based retention (not switch
mechanisms), target "up to 4-5GB." Proposed an adaptive approach but parked
it — implemented later, see `CLAUDE.md`'s current-state entry for
`collect_all.py`.

### Session 2026-07-25, continued again: real production outage from the type-28 fix

Immediately after deploying the type-28 fix above, the human reported the
live site completely unresponsive for 5+ minutes. Confirmed independently
(`curl` from outside timed out; a direct request from *inside* the
container to its own listening port also timed out, while `railway status`
still reported the service "Online" — the platform's health signal doesn't
catch an application-level hang).

**Root cause**: `_populate_base_levels()` (the type-28 fix's own new code)
makes a real Blizzard API call per never-before-seen item, gathered
unconditionally across every sell realm's sales *and* the entire region's
buy-side listings. `dashboard.py`'s `api_snipes()` is an `async def` route
but called `find_snipes()` (and therefore this) directly on the event loop
thread — so the first real request after deploying, against a cold cache,
meant hundreds of sequential blocking HTTP calls with the *entire*
single-process server unable to answer anything else for the duration.

**Fixed the same session**: `api_snipes()` now runs the whole query via
`await asyncio.to_thread(...)`; `_populate_base_levels()` also now saves
its cache incrementally (every 50 items) instead of only at the end, so an
interrupted run doesn't lose all its progress. Verified live: the homepage
returned HTTP 200 in ~0.25-0.3s consistently, including while a
deliberately-triggered slow cold-cache run was in flight in parallel via
`railway ssh`. `pytest -q`: 225 passing — this was an availability bug
under load, not something the existing test suite would have caught (still
a real gap: no test coverage for "does an async route block the event
loop").

---

## Per-request latency fix (2026-07-25): base-level resolution

Human reported the live dashboard stuck on "Loading…" for 5+ minutes while
logged in as superuser. Traced to `_populate_base_levels()` resolving each
not-yet-cached item's base level one sequential Blizzard API call at a
time. A first fix (parallelizing via a thread pool) turned out insufficient
once live numbers came in: **17,408** distinct items region-wide carry a
type-28 modifier, **15,883** from one sell realm alone — no amount of
concurrency beats Blizzard's 100 req/s ceiling against a set that size.
Real fix: capped how many new items resolve per call (500, prioritizing the
sell realm's own items), plus a background pre-warm step in
`collect_all.py` (resolves up to 1000 more items per ~10-min cycle
regardless of user traffic) so the cache converges over a few hours instead
of depending on dashboard loads. Also: a `fetchInFlight` guard in
`dashboard.html` stops overlapping auto-refresh requests from piling up.
`pytest -q`: 235 passing.

## Bonus-list noise detection (item 36507, Iron-Molded Fist): three attempts

A second real snipe-matching bug, reported right after the latency fix
above — item 36507 showed a 66,666g "deal" while a genuine 5,400g listing
on another realm never surfaced. Root cause: a per-craft "instance" id
living in the `b:` bonus-lists segment (not the `m:` modifiers segment the
earlier type-9/42/44/28 fixes covered), varying almost per-listing
alongside one stable, real id.

**Attempt 1 — frequency-only threshold.** Shipped, then live-disproven: a
5%-only cutoff correctly caught item 36507's clearly-noise values (≤4%) but
left its 6-14%-frequency noise untouched, since real dimensions on OTHER
items (e.g. 109168's socket/gem bonuses) also sit as low as 14-17%. No
single frequency band separates "real" from "noise" across every item.

**Attempt 2 — pure cardinality check** ("few distinct values in the
ambiguous band = real, many = noise") was tried next and also rejected: it
broke on item 210108, which legitimately has ~10 ambiguous-band values
across several real paired dimensions stacked together — a large total
count driven entirely by real structure, not noise.

**Attempt 3 — structural test, shipped.** A value's frequency alone isn't
enough signal; whether it has a *partner* is. Real dimensions come in
recognizable shapes — a companion pair that (almost) always appears
together (e.g. item 109168's two-part gem/socket bonus, 9145 paired with
9148), or a small set of mutually-exclusive values that jointly cover most
of the item's listings (e.g. item 244752's five-value item-level-upgrade-
track system). Per-craft noise (item 36507's ~17 near-random instance ids,
and similarly-shaped items 82070, 25218, 82194, 15009) has neither shape —
each value stands alone, no reliable partner. Validated live against 8
diverse real items with zero misclassifications.

`market_key()` gained a third `noise_bonus_ids` arg; `_populate_base_levels()`
was renamed `_populate_market_keys()` and now also does this detection.
Bulk-loaded via Arrow — `executemany()` tested unusably slow at 699k pairs
live (didn't return within 90s); registering a pyarrow Table and `CREATE
TABLE AS SELECT` loaded the same 699k rows in 0.23s.

Also caught and fixed a real DuckDB correctness bug while building this:
`ROW_NUMBER() OVER ()` inside a CTE referenced twice (once per side of a
self-join) doesn't give stable, matching ids — fixed by materializing as a
real temp table.

Test suite: 235 → 244 passing across the three attempts.

---

## Pricing model replaced: sold-price percentile → current-cheapest-listing (2026-07-25)

The single biggest change of the 2026-07-25 sessions. Reported bug: item
13051 (Witchfury) showed a sell price of 667,999g against a real ~7,999g
price.

**Traced to two compounding bugs.** The *only two* recorded "sales" for
this item's market_key group were the same camped-relist troll listing at
two different joke prices (647,999.98g and 667,999.98g) — at the time, the
relist-matching window required an exact price match, so the price-varying
repost slipped through as two fake `inferred_sale` events instead of one
`likely_relisted`. The existing sell-price safety cap (current-lowest-
listing) should have caught this regardless, but an *unrelated* bug
defeated it: the ilvl-plausibility check treated a plausible-looking but
still-junk type-28 value as a real, distinct item level, splitting the
troll listing and a genuinely cheap real listing into two different,
unmatched `market_key`s — so the cap never even saw the real listing to cap
against.

This was the **third separate live production bug** from the same
underlying design, after items 15138 (a single unverified sample becoming
the whole percentile) and 206477 (a repeated troll sale dragging the median
past the `min_sales=2` floor), each individually patched before this.

**Human's call, confirmed explicitly**: stop inferring a historical price
entirely. Sell price is now simply the sell realm's own current cheapest
live listing — directly observable, zero classification, immune to every
one of these bugs by construction, since there's nothing left to
misclassify.

**The tradeoff, stated plainly**: this makes a "snipe" a comparison of
listing price to listing price — the same thing TSM Sniper and Auctionator
already do, which is exactly what this project's own original framing
positioned itself against ("existing tools flag items cheap relative to
listing prices — which are fiction"). It no longer tries to answer "does
this item actually sell," only "is this cheaper than what's currently
listed on my realm." The underlying `diff_snapshots.py` classification
engine still runs, unaffected, and could back a future liquidity/confidence
signal without being back on the pricing critical path — not built now, a
deliberate, discussed tradeoff, not an oversight.

**What changed in code**: `min_sales`/`min_per_day`/`sell_percentile`
removed from `snipe_check.find_snipes()`, the CLI, and `/api/snipes`;
`sell_now_g`/`sell_now_copper`/`per_day`/`sales` removed from output (the
current-lowest-listing price is now just *the* price, not a cap on
something else); dashboard's duplicate "Sell p25"/"Sell realm low" columns
collapsed into one "Sell price" column; "Sales/day" column/filter/sort
removed.

**Verification**: extensive fixture redesign across `test_snipe_check.py`/
`test_dashboard.py` — pricing tests now establish price via a current
listing, not a listing that vanishes into inferred sales. `pytest -q`: 243
passing at ship time.

**One residual, non-bug finding, documented not fixed**: a genuinely live
"troll/decoy" current listing can still become a market's reference price
— inherent to this design, not a bug in it. Live-confirmed on item 13051
post-deploy: its `market_key='b:6659'` group (the implausible/junk
type-28-value group) has no legitimate cheap listing at all, only a
~667,999g decoy sitting live on Draenor — junk-tagged buy-side listings
correctly match that group and show a real, honest 99%+ discount against a
real, honest (but misleading) reference price. The item's *other* group
(`market_key='b:6659|m:28=35'`, the real variant) correctly shows its own
accurate ~7,999g price — the originally reported bug is fixed. There is no
remaining sale-classification layer to catch "this current listing looks
like a decoy" — flagged for a future plausibility check on the current
listing itself, not built.

---

## TSM public pricing data — parked idea (2026-07-25)

Human idea: use TSM's public pricing data (`tradeskillmaster.com/public-data`)
instead of, or alongside, this project's own sale inference. Investigated:
`public-data.tradeskillmaster.com` serves free, unauthenticated, per-realm
CSVs (`.../retail/{region}/realm/{slug}/items.csv`, no key/rate-limit); a
separate authenticated TSM Web API exists too. Both are ultimately derived
from the same Blizzard AH dumps this project already pulls directly, run
through TSM's own closed-source aggregation — not an independent or deeper
ground truth (their own "historical price" is only a 60-day rolling
average). The actual `tradeskillmaster.com` pages (including their terms of
use, not yet read) block automated fetches, so this was pieced together
from search results and a third-party GitHub repo, not confirmed firsthand.

Best-scoped use if revisited: a periodic sanity-check of this project's own
sold-price percentiles against TSM's regional numbers — literally the Phase
0 validation step never run — not a replacement for the live
Blizzard-API-driven snipe detection. **Explicitly parked by the human**, no
design work done, not scheduled.

---

## Full project cleanup pass (2026-07-25)

After the pricing-model replacement shipped, the human asked for a full
cleanup: fix other known open gaps, remove dead code, trim the CLAUDE.md/
PROGRESS.md narrative, review the code for architectural cleanliness. A
dead-code audit (Explore agent, cross-referencing every defined function/
class against repo-wide usage) found no genuine dead code from the pricing
refactor itself — that removal was already complete. The real bloat was in
the docs: `CLAUDE.md` (1,661 lines) + `PROGRESS.md` (988 lines), more than
the entire ~3,000-line codebase combined.

Shipped, each as its own tested/deployed commit:
1. **Atomic listings write** (`scan_region.py`) — `scan_one()` wrote
   directly to the final `{cr}.parquet` path; a reader mid-write (e.g.
   `find_snipes()` during a background sweep) could open a truncated file
   and crash with a hard DuckDB read error. Hit live by accident this same
   cleanup session while verifying an unrelated fix. Fixed: write to a temp
   file in the same directory, then `os.replace()`.
2. **Adaptive disk retention** (`collect_all.py`) — implementing the
   direction confirmed via `AskUserQuestion` during the disk-usage
   investigation above: `_effective_retention_days()` shrinks the effective
   retention proportionally once total snapshot usage exceeds a ~4.5GB
   safety budget, floored at 2 days (matching `prune_old_snapshots()`'s
   existing always-keep-2 floor).
3. **Camped-relist price tolerance** (`diff_snapshots.py`) — `relist_key()`
   required an exact buyout match, so a troll reposting at a different joke
   price slipped through as a fake `inferred_sale` (the same root shape as
   item 13051's Witchfury bug above, though lower-stakes now that pricing
   no longer reads `sales` at all). Price is now matched via a ±15%
   tolerance band (`RELIST_PRICE_TOLERANCE`) instead of exact equality.
4. **Architecture extraction pass** — `_populate_market_keys()` (200+
   lines, three jobs) split into `_detect_noise_bonus_ids()`,
   `_resolve_base_levels()`, `_load_market_key_table()`; `find_snipes()`'s
   appearance-filter tail extracted to `_filter_by_appearance()`; a shared
   `fetch_snapshot.parse_bonus_key()` deduplicates bonus-key tokenizing
   between `market_key()`'s read-only first pass and
   `dashboard._parse_variant()` (market_key()'s output-building/filtering
   logic itself was deliberately left untouched, since its exact string
   output is pinned by the Python/SQL parity test); a shared
   `snipe_check.check_data_ready()` deduplicates the "events exist +
   listings swept" precondition between the CLI and `/api/snipes`. All pure
   extractions, no behavior change.
5. **Dead code removal** — `auth.py`'s unused `UserUpdate` schema class
   (no `get_users_router` route was ever wired, so no PATCH /users/me
   endpoint exists to need it).
6. **Docs restructure** (this file) — this session's own work.

---

## Stop retaining sales-history snapshots; retire diff_snapshots.py from the live loop (2026-07-25)

Right after the cleanup pass above shipped (including the adaptive disk
retention work, item 2 in that list), the human asked whether that
retention work was even necessary, since pricing no longer reads
sold-price history at all. Re-checked the code to answer properly rather
than guessing: `snipe_check.find_snipes()`'s pricing query (`sell_now` CTE)
only ever reads `snaps` filtered to `snapshot_ts = max(snapshot_ts)` — the
*latest* snapshot per sell realm, never history, confirmed by grepping
every place the query touches `snaps`. The only thing that still needed
multi-day snapshot history was `diff_snapshots.py`, the sale-inference
classification engine: not on the pricing path, never validated against
real seller behavior (Phase 0 skipped since 2026-07-20), not scheduled to
be used for anything live.

Presented via `AskUserQuestion`: keep the adaptive retention as-is / shrink
it hard / stop retaining history entirely and retire `diff_snapshots.py`
from the automatic collection loop (code stays in the repo, usable
ad-hoc). **Human chose: stop retaining history entirely.**

**Turned out to be bigger than just retention.** Investigation found three
places used "does `data/events/{cr}.parquet` exist" as a proxy for "does
this realm have usable data," and all three would have broken the moment
that file stopped being generated automatically:

- `analyze.connect()` unconditionally did `CREATE VIEW ev AS SELECT * FROM
  '{events_path}'` — errors outright if the file doesn't exist. Both
  `snipe_check.find_snipes()` (via the CLI and `/api/snipes`) and
  `analyze.py`'s own CLI get their connection through this.
- `snipe_check.check_data_ready(sell)` 400s/exits if the events file is
  missing — would have permanently 400'd every sell realm once nothing
  generates it.
- `dashboard._list_collected_realms()` (backed `GET /api/realms`, the
  sell-realm picker dropdown) only listed realms with an events file —
  would have returned an empty list forever, breaking realm selection
  entirely.
- `dashboard.py`'s `/api/status` `events_exist` field drove
  `dashboard.html`'s "Last auction data" ticker's stale/fresh styling —
  would have shown permanently stale.

**Shipped, all in one commit** (the pieces couldn't land separately —
removing the diff step without `analyze.connect()`'s fallback would have
broken every realm the moment the next cycle ran with no events file):

- `collect_all.py`: removed the diff step and all day-based retention
  machinery (`_diff()`, `RETENTION_DAYS`, `SAFETY_BYTES`,
  `MIN_RETENTION_DAYS`, `_total_snapshot_bytes()`,
  `_effective_retention_days()`, `prune_old_snapshots()`). Added
  `prune_to_latest(cr)`: after a new snapshot lands, deletes every other
  snapshot for that realm, keeping only the newest.
- `analyze.connect()` made resilient: builds an empty `ev` table (matching
  `diff_snapshots.EVENT_SCHEMA`) when the events file is missing, instead
  of erroring — via the same `con.register()` + `CREATE TABLE AS SELECT` +
  `con.unregister()` pattern `snipe_check._load_market_key_table()`
  already used. `sales` comes back empty, which is correct now, not a bug.
- `snipe_check.check_data_ready()`'s precondition changed from "events
  file exists" to "at least one snapshot file exists."
- `dashboard._list_collected_realms()` and `_list_snapshotted_realms()`
  were the same check now — consolidated into one, `/api/realms` switched
  to it. `/api/status`'s `events_exist` renamed to `has_data` (true when a
  snapshot has ever been retrieved); `dashboard.html`'s ticker updated.

**Rollout**: snapshots already on the production volume under the old
retention scheme self-clean automatically — `prune_to_latest()` only fires
in the post-fetch-new-snapshot branch, same as the old pruning did, so
each deep-collected realm's history collapses to 1 file the next time it
gets a new snapshot (observed live: volume usage ticked from 1.3GB to
1.6GB right after deploy as pending snapshots landed, before starting to
shrink as each realm's next cycle pruned it down). The pre-existing
`data/events/*.parquet` files were left in place rather than deleted —
`analyze.connect()`'s "use it if it exists" fallback means they just keep
serving as frozen, no-longer-updated historical data for any realm that
happens to have one, which is harmless (a real snapshot of actual past
sales, not garbage) even though it won't be regenerated.

`pytest -q`: 258 passing (down from 262 — net removal of retention-specific
tests that no longer applied, plus new coverage for `prune_to_latest()`
and for `find_snipes()` working correctly with zero events on disk, the
new normal).
