# HISTORY — detailed session log

This file holds the full, session-by-session narrative that used to live
inline in `CLAUDE.md`/`progress.md`: what was reported, how it was traced,
what was tried and rejected, what shipped. `CLAUDE.md` and `progress.md`
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
progress.md narrative, review the code for architectural cleanliness. A
dead-code audit (Explore agent, cross-referencing every defined function/
class against repo-wide usage) found no genuine dead code from the pricing
refactor itself — that removal was already complete. The real bloat was in
the docs: `CLAUDE.md` (1,661 lines) + `progress.md` (988 lines), more than
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

## Realm-switch hang/timeout: a second blocking-event-loop site (2026-07-26)

Human reported live: switching the dashboard's sell-realm dropdown away from
Draenor (the realm every prior session's testing had already warmed the
cache against) loaded indefinitely and then errored out/timed out, while
Draenor itself always loaded fine.

Traced to `dashboard.api_snipes()`: the 2026-07-25 outage fix
(`asyncio.to_thread(_run_query)`, see "Per-request latency fix" above and
`CLAUDE.md`'s "Real production outage" section) only covered
`find_snipes()`'s own blocking Blizzard calls. The `names=true` row-building
step added afterward (`[_row_to_json(r, name_cache) for r in rows]`) was
never wrapped the same way — it ran directly on the event loop, and every
row's `name_cache.get()`/`.icon()`/`.quality_color()`/`.quality()`/
`.item_class()`/`.item_subclass()`/`.inventory_type()` calls fall back to a
blocking Blizzard API call on a cache miss (`item_names.py`'s
`_ensure_item_details`/`_fetch_icon`). Draenor's cache was warm from every
prior session, so this path was always a cache hit there and the bug never
surfaced; any realm queried for the first time hit potentially hundreds of
distinct never-before-seen items, each doing at least one (often two,
counting the separate icon fetch) sequential blocking HTTP round trip,
directly on the event loop — freezing the whole single-process server for
the duration, same failure mode as the original outage, just a different
call site.

Fix: wrapped the whole row-building step in `asyncio.to_thread`, and added
`NameCache.ensure_icons_many()` (icon lookups were never covered by
`ensure_many()`'s concurrent `_fetch_item_details` batch at all — a
separate gap, not just a missing `to_thread`). Both `ensure_many()` and
`ensure_icons_many()` now run concurrently (`max_workers=24`) over the
batch's distinct item ids before the per-row translation, so a cold realm's
first query resolves its items in parallel instead of one blocking call at
a time. Addresses the "no test coverage for 'does an async route block the
event loop'" gap noted in `progress.md`'s "Known gaps" partially — added
unit coverage for `ensure_icons_many()`'s concurrency/dedup/cache-skip
behavior in `tests/test_item_names.py`, though a true event-loop-blocking
regression test (a slow stub proving a concurrent lightweight request still
completes quickly) is still not built.

`pytest -q` and `env -u DATABASE_URL pytest -q`: 266 passing, both envs.

## Realm switch also skipped the "is this actually stale" check (2026-07-26, same session)

Follow-up question from the human after the fix above landed: "if the user
already has a relevant snapshot for the desired/locked realm, no reason to
query again, right?" Checking `static/dashboard.html` confirmed this wasn't
actually true: `$("sell").addEventListener("change", fetchBatch)` and the
items-csv equivalent called `fetchBatch()` directly, which always issues a
real `/api/snipes` call (it paints from `localStorage` first for instant
feedback, but then unconditionally re-fetches regardless). Only the 60s
auto-refresh timer and the initial page load went through `checkForUpdates()`
— the cheap `/api/status` check that skips the expensive query when
`last_modified` hasn't changed since the cached copy. So switching back to a
realm already viewed this session, with no new Blizzard data since, still
re-ran the full DuckDB `find_snipes()` query every time.

Fixed by adding `onCandidatePoolChange()` (paints from cache, then calls
`checkForUpdates()` instead of `fetchBatch()` directly) and wiring both the
sell-realm and items-csv change listeners to it instead. `fetchBatch()`
itself is unchanged — it's now only ever invoked by `checkForUpdates()`.

**Verified in an actual browser** (this project's established technique:
scratchpad copy of `dashboard.html`, `window.fetch` stubbed with two fake
realms and controllable `last_modified` values, served via
`python -m http.server`, driven via `claude-in-chrome`'s `javascript_tool`
dispatching real `change` events on the `#sell` select — not just reading
the diff): confirmed all three cases — first-time switch to a realm fetches
(1 call), switching back to an already-cached *unchanged* realm makes zero
new `/api/snipes` calls, and a realm whose stubbed `last_modified` was then
advanced correctly triggered a fresh fetch and rendered the updated data.

No Python changed (`static/dashboard.html` only) — `pytest -q` unaffected,
266 passing.

## Bonus/ilvl matching removed from live pricing (2026-07-26, same session)

Human report: "I think I encounter some sell/buy pricing bugs because of
bonuses? Like an item should always show cheapest listings on any EU realm,
if it's a snipe same as selling price = sell realm's lowest listings
completely ignoring bonuses, as it is the same item."

**Investigated live before touching anything** (this project's standing
convention for matching-logic changes). Hypothesis: `snipe_check.
_detect_noise_bonus_ids()`'s structural per-craft-noise test only ran per
item above a 20-sample floor (`BONUS_NOISE_MIN_SAMPLES`), fed by `sales`
(historical inferred sales) + `snaps` (latest snapshot only) + `listings`
(region-wide current). Since 2026-07-25's retention change made `sales`
permanently empty and `snaps` only ever the single latest snapshot, this
sample pool shrank a lot from whatever it was calibrated against. Queried
real Draenor data to check: of 12,219 items with any `b:` bonus data,
**2,011 sat under the 20-sample floor**, and **1,223 of those showed the
exact per-craft-noise shape** the heuristic was built to catch (e.g. item
36322: 19 samples, 18 distinct bonus_keys, a stable companion id 6654
paired with a different "instance" id 1678-1716 almost every time — the
identical shape as the originally-documented item 36507 case, just never
detected because this item's sample count landed one below the floor). At
least 14 other items showed the same numeric-range pattern. Confirmed: the
noise-detection heuristic was silently failing for a meaningful share of
real items, specifically lower-liquidity ones -- exactly what this product
cares about.

Presented three fix directions (re-tune the sample floor; detect the
recurring noise shape directly regardless of per-item sample count; just
document and defer). **Human pushed further**: bonus differences shouldn't
gate a match at all, full stop -- "it doesn't matter if an item has 1 or 2
or different, it's the same item." Pushed back once with the concrete
counter-case already in this project's own history (item 164353,
Plundered Scalebane Claymore: real listings existed at both an implausible
tier (28=186/189/289) and a genuinely different, more valuable tier
(28=645/670) -- ignoring bonuses entirely would compare a cheap base
listing against the sell realm's cheapest when that happens to be the
upgraded version, or vice versa, silently proposing a bad trade). Human's
final answer: **merge them together in one listing regardless, lowest
price is the snipe, ilvl still displayed per row** -- an explicit,
informed decision to accept that tradeoff, not an oversight.

**Implementation** (`snipe_check.py`, `dashboard.py`, `static/dashboard.html`):
- `find_snipes()`'s match key changed from `(item_id, market_key(bonus_key),
  pet_species_id, pet_quality_id)` to plain `(item_id, pet_species_id,
  pet_quality_id)`. `sell_now`'s `cheapest_now` is now the sell realm's
  overall cheapest current listing for an item_id, across every bonus_key
  it has. `max_per_item`'s `ROW_NUMBER()` partition changed to match.
- Deleted four now-unreachable helpers from `snipe_check.py`:
  `_detect_noise_bonus_ids()`, `_resolve_base_levels()`,
  `_load_market_key_table()`, `_populate_market_keys()`, plus
  `MAX_BASE_LEVEL_LOOKUPS_PER_CALL` and the now-dead `BONUS_NOISE_*`
  constants + their large calibration comment in `fetch_snapshot.py` (sole
  consumer gone; full methodology preserved above in this file's own
  "Bonus-list noise detection" entry if ever needed again).
  `fetch_snapshot.market_key()` itself is untouched and still real --
  `diff_snapshots.relist_key()` (needs finer-than-item_id identity to
  detect an actual relist) and `analyze.py`'s manual debugging macro are
  its only remaining callers.
- `dashboard.py`'s `/api/snipes` output dropped `market_key`, added
  `pet_species_id`/`pet_quality_id` (previously computed internally but
  never exposed) so the frontend can still tell pet species/quality apart
  -- every caged pet shares one item_id (82800), so without these two
  fields the new item_id-only grouping would have wrongly merged every
  pet species into one display group (a latent bug that already existed
  in the old `market_key`-based `groupKey()` too, since `market_key` was
  always empty for pets same as `bonus_key` -- fixed as a side effect of
  this change, not separately).
- `static/dashboard.html`'s `groupKey()` changed from `market_key` to
  `(item_id, pet_species_id, pet_quality_id)`.
- The raw `bonus_key`/`variant_raw`/`variant` (ilvl or bonus-count summary)
  path is completely unchanged -- `dashboard._variant_label()` still shows
  a per-row ilvl when plausible, exactly as the human asked.

**Tests**: removed/replaced five `tests/test_snipe_check.py` tests whose
whole point was the old design's deliberate non-pooling (companion pair
kept distinct, partition tiers kept distinct) -- under the new model these
are supposed to merge, so the assertions were inverted, not just updated.
Added `test_find_snipes_merges_price_tiers_using_overall_cheapest`
(explicitly proves three real price tiers now collapse to the overall
minimum) and `test_find_snipes_pools_bonus_list_noise_with_no_sample_floor`
(the item-36507 shape now works with as few as 2 samples, no floor to
clear). `tests/test_dashboard.py`'s market_key existence test replaced with
one asserting `pet_species_id`/`pet_quality_id` ride along instead.
`pytest -q` and `env -u DATABASE_URL pytest -q`: 264 passing, both envs
(net -2 from the prior 266: several old market_key-pooling tests collapsed
into fewer, more direct ones under the simpler model).

**Unrelated small request folded into the same session**: superuser tier
cap raised from 5000 to 10000 (`dashboard.SNIPE_TIER_CAPS`), with
`static/dashboard.html`'s `BATCH_TOP` raised to match (it's the ceiling the
frontend ever requests -- leaving it at 5000 would have silently kept
superuser responses capped at the old value regardless of the server-side
change).

## New public landing page; sniper tool moved to /snipes (2026-07-26, same session)

Human requests, handled together since they're the same underlying change:
remove the redundant "← Back to dashboard" link from the pricing page,
add a real public marketing landing page at `/`, and move the sniper tool
itself to `/snipes`.

**Routing** (`dashboard.py`): `GET /` now serves the new `static/landing.html`
instead of `static/dashboard.html`; a new `GET /snipes` route serves
`static/dashboard.html` (unauthenticated at the route level, same as
before -- `dashboard.html`'s own `init()` still enforces auth client-side
by checking `/api/me`). Every other page's "Dashboard" nav link and
`login.html`'s post-login redirect were updated from `/` to `/snipes`.

**New landing page** (`static/landing.html`): consulted the
`frontend-design` skill before building it, since a marketing page has
real design stakes and this project already has an established, deliberate
visual identity ("assay ledger" -- see the "UI design pass" entry above)
that a fresh page should extend, not reinvent. Reused the exact existing
color tokens/type stack for everything except one restrained addition: the
hero `<h1>` alone uses a serif system stack (`Georgia, "Times New Roman"`)
against the sans-serif body everywhere else, meant to read like a heading
on a certified/appraisal document -- the same "validated data, not
decoration" thesis the seal mark already carries, not a new one. Signature
element: a static "sample ledger row" in the hero, reusing the real
dashboard's own coin-icon/quality-ring/discount% visual language rather
than a generic illustration, explicitly labeled "Example listing --
illustrative, not live data" so it can never be mistaken for real numbers
(this project has an existing "not a demo, not fake data" ethic on
`pricing.html` that a misleading hero mockup would have quietly
contradicted). A numbered 3-step "how it works" section is used
deliberately -- it's a genuine sequential process (pick realm → scan →
results), not decorative numbering. First pass silently redirected a
logged-in visitor away from `/` straight to `/snipes` (checked `/api/me` in
the background) -- **reversed the same session** on human feedback: a hard
redirect means nobody signed in can ever actually see their own marketing
page (a shared link, checking the pitch copy, etc.). Replaced with CTA
personalization instead -- the same `/api/me` check swaps `#nav-cta`/
`#hero-cta`/`#closing-cta` to "Go to dashboard" (pointing at `/snipes`) and
removes the "Log in" nav link (`#nav-login`) when already authenticated,
but the page itself always stays visible either way.

**Two follow-up requests during the same build**, applied consistently
across all seven other static pages (`dashboard.html`, `login.html`,
`register.html`, `subscribe.html`, `profile.html`, `log.html`,
`pricing.html`):
1. Removed the `seal-label` "Validated Data" text next to the seal icon in
   `dashboard.html`'s topbar (the only page that had it) -- the seal icon
   itself is unchanged.
2. Wrapped every page's brand mark (seal + wordmark) in a link to `/`
   (`.brand-link`, `display: contents` so the link itself takes no box in
   the existing flex layout -- only the svg/h1 participate in `.brand`'s
   flex row, so nothing about the existing spacing changes). `pricing.html`'s
   separate "Dashboard" nav link was removed outright per the original
   request (not repointed, unlike `profile.html`/`log.html`'s); the exact
   quoted "← Back to dashboard" text turned out to live on `subscribe.html`,
   not `/pricing` as first described -- removed there too, since the
   literal text match was the stronger signal once both pages were checked.

**Verified in an actual browser**: the new landing page in both themes
(dark/light toggle, all CTA hrefs correct via DOM inspection); `dashboard.html`'s
header confirmed to have no `seal-label`/"Validated Data" text and a real
`<a href="/">` wrapping the brand mark, with no visual layout disruption.

`pytest -q`/`env -u DATABASE_URL pytest -q`: 265 passing (new
`test_index_serves_landing_page` replacing the old `test_index_serves_html`,
plus a new `test_snipes_serves_dashboard_html`).

**Two more small follow-ups, same session**: (1) landing page's auto-
redirect for logged-in visitors reversed after human feedback ("shouldn't
logged-in users just have a different landing page?") -- a hard redirect
meant nobody signed in could ever actually see the page. Replaced with CTA
personalization: the same `/api/me` check swaps `#nav-cta`/`#hero-cta`/
`#closing-cta` to "Go to dashboard" and removes `#nav-login`, but the page
itself always stays visible. Verified both states in an actual browser
(stubbed `/api/me` for logged-in, unstubbed 404 for logged-out). (2)
Removed the `Pricing` nav link from `dashboard.html`'s own topbar --
that navbar is only ever seen by already-logged-in users inside the tool,
unlike `landing.html`'s navbar (the marketing frontpage), which keeps
`Pricing` regardless of login state -- a deliberate distinction the human
was explicit about ("not the navbar on the frontpage"). No Python touched
either time; `pytest -q`: 265 passing throughout.

## Landing/pricing page polish pass (2026-07-26, same session)

Four more human requests against the pages just shipped, handled in one
pass:

1. **`pricing.html`'s "Log in" nav link was unconditionally shown, even to
   an already-authenticated visitor** landing there from `/` (clicking
   "See pricing" while logged in). Fixed the same way `landing.html`
   already personalizes its own CTAs: on load, `fetch("/api/me")` and
   remove `#nav-login` if the session is active -- no nav link put back in
   its place, since the "Dashboard" link on this page was deliberately
   removed earlier in the session, not an oversight to restore. Verified
   both states in an actual browser.
2. **The hero's "sample listing" was a paraphrased custom card, not the
   real thing** -- human asked for it to actually look like the sniper.
   Rebuilt using `dashboard.html`'s own table/`item-cell`/`item-icon`/
   `money`/`coin` markup and class names verbatim (headers: Buy realm/
   Item/Variant/Buy/Sell price/Discount %, a real quality-ring icon, real
   gold-coin formatting), with one caption row still marking it "Example
   listing: illustrative, not live data" so it's never mistaken for a real
   query result. This changed `test_index_serves_landing_page`'s
   distinguishing assertion: it used to check for the *absence* of any
   `<table>` element to tell the landing page apart from the real
   dashboard, which broke once the landing page legitimately grew its own
   table -- switched both route tests to check for `id="rows"` (the real
   dynamic ledger body) instead, which only ever exists on the actual tool.
3. **Removed the entire "Not another sniper" TSM/Auctionator comparison
   section** (human request) -- its now-unused `.compare` CSS removed
   alongside it.
4. **Removed every em-dash from `landing.html`'s visible copy** (human
   request) -- replaced with periods or colons depending on the sentence,
   never restructured to lose meaning. Hyphenated compound words
   ("cross-realm", etc.) were left untouched -- the request was about
   dash-as-punctuation, not legitimate hyphenation.

`pytest -q`/`env -u DATABASE_URL pytest -q`: 265 passing, both envs.
Verified in an actual browser throughout (the real-table sample listing,
and both logged-in/logged-out states of `pricing.html`'s nav).

One more small request in the same pass: removed the "Most popular" badge
from the Subscriber plan card on `pricing.html`, plus its now-unused
`.plan-badge` CSS and the `position: relative` on `.plan.highlight` that
existed only to anchor that badge.

## Two more landing page copy edits (2026-07-26, same session)

Removed the closing "Every account gets real data" section entirely
(heading, paragraph, and its "Create free account" CTA) -- human request.
Cleaned up its now-unused `.closing` CSS, and removed the dangling
`#closing-cta` reference from the `/api/me` CTA-personalization script
(the earlier round's logged-in-visitor handling), which would otherwise
have called `.textContent`/`.href` on `null` once the element was gone --
verified no console error after the fix by reloading in an actual browser.

Reworded the "how it works" step 2 heading: "We scan every other EU
realm" -> "Scans every EU realm" (human iterated on the exact phrasing
mid-request, landing here). `pytest -q`: 265 passing, no Python touched.

## Hero copy rewritten to stop overclaiming "validated" (2026-07-26, same session)

Human asked for the hero subheading to be redescribed: emphasize that the
tool *calculates the price difference* (the literal "arbitrage" the
product is named for), and be honest that not every flagged item is
necessarily a good snipe -- the number could reflect a troll/camped
listing from another player, not real value.

This is a genuine accuracy fix, not just copy polish: the old wording
("If it's flagged, it's already validated") oversold the product relative
to a real, still-open, already-documented gap -- `progress.md`'s "Known
gaps" section has tracked since earlier today that a genuinely live troll/
decoy listing can become a market's reference price, with no
classification layer to catch it (see the "decoy-listing crowd-out"
finding). The hero was quietly claiming a guarantee the product doesn't
actually make.

New copy: "Realm Arbitrage calculates the real price difference, the
arbitrage, between your own realm's current price and every other EU
realm's listings for the same item. Not every gap is a genuine bargain:
some listings are troll prices or camped jokes from other players, not
real value. If it looks worth it, buy it there, move it home through the
warband bank, sell it here." The `<meta name="description">` tag (same
overclaiming phrase) was updated to match. Verified in an actual browser;
no em-dashes introduced (still holding the earlier em-dash-free request).
`pytest -q`: 265 passing, no Python touched.

## Experimental "legacy jewelry" bait filter (2026-07-31)

Human report: old, low-value jewelry (rings/necklaces/trinkets from
long-superseded expansions, e.g. MoP-era jewelcrafting BoEs, TBC vendor
junk) generates obvious bait "100% discount" snipes cluttering results,
naming four concrete examples: Shadowfire Necklace, Ornate Band, Charm of
Potent and Powerful Passions, Polished Pendant of Edible Energy.

Two candidate framings were considered and ruled out first, in an earlier
part of the same session: the human initially asked about a filter for
"removed items," which turned out to mean two different things across the
conversation. First, "a listing that's been bought/expired should stop
showing" — already a real, separate bug, fixed the same session (see
`checkForUpdates()`'s `listings_updated` fix, and the `no_cache_html`
middleware fix for a stale-browser-cache compounding factor). Second, when
asked to clarify, the human meant "no longer obtainable in-game except via
the AH" — investigated and shelved: no clean, ToS-safe, bulk-programmatic
data source exists for this (Blizzard's API has no such field; wago.tools'
DB2 exports don't include loot/vendor/quest tables since those are
server-side, never shipped to the client; Wowhead has no official API and
the "no longer obtainable" tag is manually curated per-item; the AllTheThings
addon was identified as the one plausible real candidate but pulling its Lua
data is a genuinely separate, bigger integration, not attempted). The human
chose to hold off on that one rather than pursue a user-curated list or the
AllTheThings option — documented as a known gap, same as `appearance.py`'s
`source_count` caveat, in case it's revisited later.

This session's actual ask, once clarified, was different and much more
tractable: an experimental, opt-in filter for specifically old jewelry
(neck/ring/trinket), reasoned as follows. Unlike weapons/armor, these slots
have **no transmog-appearance value at all** — WoW's transmog system
doesn't cover neck/finger/trinket — so there's no collector-value angle
that could make an ancient one worth flagging the way a rare old weapon
look might be. This is a stronger, narrower case than the appearance-rarity
filter already shipped.

Live-verified against Blizzard's real API (`blizz.api_get()`, real `.env`
creds, not guessed) for the human's exact examples:

| item | id | ilvl | quality | inventory_type |
|---|---|---|---|---|
| Charm of Potent and Powerful Passions | 27982 | 26 | Common | NECK |
| Polished Pendant of Edible Energy | 27976 | 22 | Common | NECK (sold by an NPC vendor for 25g) |
| Ornate Band | 83793 | 37 | Uncommon | FINGER |
| Shadowfire Necklace | 83794 | 37 | Uncommon | NECK |

Also confirmed live: `inventory_type.type = "TRINKET"` is the real enum
value for trinkets (via `/data/wow/search/item?inventory_type.type=TRINKET`).
`item_subclass` turned out to be useless as a discriminator — it's `0`
("Miscellaneous") for neck/finger/trinket/cloak/shirt/tabard alike;
`inventory_type.type` is the real per-slot signal. Current-expansion
jewelry sits at ilvl 600+; these ancient items are rescaled by Blizzard's
level-squish down into the 20s-30s — a wide, clean separation, comfortably
supporting a conservative `LEGACY_JEWELRY_ILVL_MAX = 150` cutoff.

A design-review pass (before implementation) surfaced a real blind spot the
initial framing missed: low ilvl does not mean no real market. Level-bracket
"twink" builds specifically prize old, low-ilvl neck/ring/trinket items — no
level requirement to out-level them, no transmog competition to crowd them
out of the conversation either — and some twink BiS jewelry is genuinely
valuable. This heuristic cannot distinguish dead vendor jewelry from a
genuinely twink-valuable item via Blizzard's API alone. Same house style as
every other heuristic in this project (`sell_price_suspect`, `market_key`'s
old noise-detection): **annotate, never silently filter server-side** — the
UI wording says "possibly obsolete," deliberately not "bait" or "worthless."

Shipped: `snipe_check.is_legacy_jewelry(inventory_type, base_level)` plus
`LEGACY_JEWELRY_INVENTORY_TYPES`/`LEGACY_JEWELRY_ILVL_MAX` (near the existing
`NON_TRANSMOG_INVENTORY_TYPES`, the actual precedent here — a
`NameCache.inventory_type()`-keyed boolean, not SQL, unlike
`sell_price_suspect`). `dashboard.py`'s `_row_to_json()` exposes
`legacy_jewelry_suspect` inside the existing `if names is not None:` block,
right after `is_profession_item` — zero extra Blizzard API cost, since
`base_level()`/`inventory_type()` are resolved by the same
`_fetch_item_details()` call that already backs `item_class`/`quality`.
`static/dashboard.html` gained a new "Hide flagged (legacy jewelry,
experimental)" checkbox (unchecked by default, same convention as "Hide
flagged (suspect price)"), a distinct `⌛`/`.legacy-flag` marker (a muted
brown, deliberately not `.suspect-flag`'s amber — one flag is about the
*price*, this one about the *item's identity*, kept visually separate) shown
in the item-cell rather than stacked into the sell-price cell, a tooltip
note row, and `CACHE_VERSION` bumped 2 → 3 (a cache from before this shipped
would otherwise repaint with a flag that never shows until it happened to
expire). Dashboard-only for now, not wired into the bare CLI — the bare CLI
path never touches `NameCache` today outside `_filter_by_appearance()`; a
future `--hide-legacy-jewelry` flag is cheap to add later precisely because
the predicate already lives in `snipe_check.py`, tracked in `progress.md`'s
"Next up."

New tests: `tests/test_snipe_check.py` (pure-function, no DuckDB) covers the
ilvl boundary, non-jewelry slots, and `None`-safety; `tests/test_dashboard.py`
mirrors the existing `item_class`/`is_profession_item` test pattern (flags
true for an old NECK item, false for current-tier ilvl, false for a
non-jewelry slot, absent entirely without `names=true`). `pytest -q` and
`env -u DATABASE_URL pytest -q`: 305 passing.

## sell_price_suspect removed; legacy_jewelry_suspect renamed/broadened to legacy_gear_suspect, adding class-starter armor (2026-07-31, same day)

Human request, two parts: (1) remove `sell_price_suspect` (the 500x-region-
mean scam-price flag from 2026-07-27, traced live to Draenor item 36519/
Moonlit Katana) outright, and (2) broaden the same-day `legacy_jewelry_suspect`
experimental filter to cover "starting items... for every slot/class," citing
Paladin's Girdle as the example.

**Part 1 — removing `sell_price_suspect`.** A deliberate human decision to
consolidate two overlapping anti-bait signals into one, not silent drift —
recorded here per this project's convention for such reversals (see the
"Process deviations" pattern in `CLAUDE.md`). Removed: `SELL_PRICE_SCAM_MULTIPLE`;
the `region_avg_cheapest` computation in `region_stats` (the CTE now computes
only the median, which `region_median_g`/`region_median_copper` still use --
unaffected); the `sell_price_suspect` column in `matches`; the `⚠`/`flag`
column in `print_snipes()`'s CLI output (nothing else populated it); the
`.suspect-flag` CSS, `hide_suspect` checkbox, and its `applyFilters()`/
`buildRowHtml()` logic in `dashboard.html`; three dedicated test cases in
`tests/test_snipe_check.py` (the Moonlit-Katana-calibration test, the
mechanism test, and the 500x boundary test). `region_median_g`'s own test
(`test_find_snipes_region_median_uses_median_not_mean`) needed only a
docstring fix -- the median computation itself was never affected.

**Part 2 — generalizing to `legacy_gear_suspect`.** Kept the field/checkbox
name honest: a flag that also catches `Paladin's Chestplate` (a chest piece)
can't stay named "jewelry" without misleading. Investigated the user's
"Paladin's Girdle" example live via `blizz.api_get()` (real `.env` creds):
ilvl 1, Plate, WAIST, item id 187726. This turned out to be part of a real,
systematic Blizzard content addition -- patch 9.1.5 (2021-11-02) introduced
a unified `"{Class}'s {Slot}"` ilvl-1 Common starter-armor set per class,
replacing older race-specific starting gear. Found the rest by probing the
contiguous id range around it (187690-187778): confirmed 9 classes --
Hunter, Shaman, Paladin, Warrior, Warlock, Mage, Priest, Rogue, Druid.
Seven get all 6 slots (CHEST/LEGS/HAND/WRIST/FEET/WAIST); Rogue and Druid
only get 5 (no WRIST piece -- checked every gap in the id range and searched
by exact name for "Rogue's Bracers"/"Druid's Bracers" and near-variants,
found nothing, so this is a real asymmetry in Blizzard's own data, not a
missed id). 52 items total. Death Knight/Demon Hunter/Monk/Evoker were also
checked (same id-range probing plus name-pattern search for each) and NOT
found -- not an oversight on this project's part; those hero classes have
bespoke starting gear tied to their own class-launch expansion instead of
this unified revamp.

Deliberately a **curated item-id set**
(`CLASS_STARTER_ARMOR_ITEM_IDS`, a `frozenset`), not a threshold rule, unlike
the jewelry half: ilvl alone can't discriminate here the way it can for
jewelry -- a live check confirmed a plain `inventory_type=WAIST, level=1`
Blizzard search returns roughly 900 items across 9 pages, the overwhelming
majority unrelated cosmetic/replica/toy items that also happen to sit at
ilvl 1. Only a real, individually-confirmed id list is safe.

`snipe_check.is_legacy_jewelry(inventory_type, base_level)` became
`is_legacy_gear(item_id, inventory_type, base_level)`, returning true if
*either* the item_id is in `CLASS_STARTER_ARMOR_ITEM_IDS` *or* the existing
jewelry-slot-plus-ilvl rule matches -- the two conditions are genuinely
complementary (a Plate waist piece isn't a jewelry slot at all, so it can
only ever be caught by the id-set path). `dashboard.py`'s
`legacy_jewelry_suspect` field/`dashboard.html`'s `hide_legacy_jewelry`
checkbox were renamed to `legacy_gear_suspect`/`hide_legacy_gear` throughout
(CSS class `.legacy-flag` was already generic enough to keep as-is). Same
non-authoritative, annotate-only convention as before -- the twink-market
blind spot on the jewelry half is unchanged and still the reason this stays
opt-in rather than a server-side filter; the class-starter-armor half has
no equivalent blind spot (a curated, individually-verified id list, not a
proxy).

`CACHE_VERSION` bumped 3 -> 4 in the same pass (removing a field and
renaming another both change the row shape old caches would carry).

New tests: `tests/test_snipe_check.py`'s `is_legacy_jewelry_*` tests renamed
to `is_legacy_gear_*` (now calling the 3-arg signature with a fixed
non-starter `item_id` so they exercise only the jewelry rule), plus two new
tests for the id-set path (a representative id per confirmed class flags
true regardless of slot; a random id at the same ilvl 1 in the same slot,
*not* in the curated set, flags false -- proving it's a real id match, not
"any ilvl-1 item"). `tests/test_dashboard.py`'s equivalent tests renamed the
same way, plus a new integration test building a custom snapshot/listing
fixture around Paladin's Girdle's real id (187726) end-to-end through
`/api/snipes?names=true`, confirming `legacy_gear_suspect=True` for a WAIST
item purely via the id-set path (no jewelry-slot condition satisfied).
`pytest -q` and `env -u DATABASE_URL pytest -q`: 306 passing.

## legacy_gear_suspect renamed to sus_item_suspect (2026-07-31, same day again)

Human naming preference, immediately after the above: not "legacy gear,"
call it "sus items." Purely a rename, no behavior change --
`snipe_check.is_legacy_gear()` -> `is_sus_item()` (same signature, same two
conditions: the jewelry-ilvl rule and `CLASS_STARTER_ARMOR_ITEM_IDS`),
`dashboard.py`'s `legacy_gear_suspect` field -> `sus_item_suspect`,
`dashboard.html`'s `hide_legacy_gear` checkbox -> `hide_sus_items` with
label text "Hide flagged (sus items)", `.legacy-flag` CSS class ->
`.sus-flag`. `CACHE_VERSION` bumped 4 -> 5 (the row-shape field name
changed again). All tests and docs renamed to match. Also fixed a real
count error found while renaming: the class-starter-armor tooltip copy and
one progress.md bullet said "~54 confirmed" pieces; the actual verified
count is 52 (Rogue and Druid each get 5 slots, not 6 -- no WRIST piece,
confirmed real via gap-checking and name search, see the previous entry).
`pytest -q` and `env -u DATABASE_URL pytest -q`: 306 passing.

## NameCache lost-update race fixed -- items "randomly jumping" (2026-08-01)

Human report: dashboard rows appearing/disappearing between refreshes with
no filter changes, worst with a small `class_quotas` bucket (recipes) or
with `sus_item_suspect` flickering under "hide flagged." Traced (not
assumed) by reading every `NameCache()` call site together: at least three
independent instances race on `data/item_names.json` within the same
process -- `snipe_check._register_class_quota_maps()` and
`dashboard._build_rows()` each make their own within a single `/api/snipes`
call, plus another on `collect_all._prewarm_item_base_levels()`'s
background loop, running concurrently with live requests. `save()` was a
blind `write_text()` of the whole in-memory `_cache`, no locking, no atomic
temp-file+rename (unlike `scan_region.py`'s sweep writes) -- a classic
lost-update race: instance B loads the file before instance A resolves and
saves item X, then B finishes unrelated work and saves, overwriting the
file with a snapshot that never saw X, silently reverting it to unresolved.
Since `has_class_info()`/`_class_bucket()` gate bucket membership on cache
presence, this directly flipped which items appeared in a class-quota'd
bucket between otherwise-identical requests, with zero change in the
underlying auction data.

Fix: `NameCache` now tracks each instance's own new writes separately
(`self._pending`, via a `_set()` helper every write site goes through) and
`save()` re-reads the current file and merges in only those keys, instead
of overwriting wholesale. `__init__`'s file read also wrapped in
`try/except` -- a concurrent writer's non-atomic write could leave the file
mid-write for a reader to catch; previously an unhandled
`json.JSONDecodeError` there crashed the request instead of falling back
to an empty cache. New tests reproduce the exact interleaving
(`test_save_does_not_clobber_a_concurrent_instances_write`) and the torn
read (`test_save_tolerates_a_torn_read_of_the_cache_file`).

## Sus items: Slithershell + Black Tooth Grunt's added (2026-08-01)

Two human-named item sets added to `CURATED_SUS_ITEM_IDS`, same pattern as
the class-starter-armor set. Both confirmed live via `blizz.api_get()`, not
guessed: **Slithershell** (searched `name.en_US=Slithershell`, a single
page, 10 total results) is a naga-themed leveling quest-reward set -- 8
Leather pieces + 1 Cloth cloak, UNCOMMON, ilvl 58, required level 50; the
10th result (Slithershell Warglaive, a weapon) deliberately excluded since
the human asked for "armors" specifically. **Black Tooth Grunt's** (the
Plate counterpart) needed a different search technique -- Blizzard's search
API ORs individual words, so `name.en_US=Black Tooth` alone matched
hundreds of unrelated items; scanned all 8 result pages for the exact
substring "Black Tooth Grunt" instead. 8 Plate pieces, UNCOMMON, ilvl 60,
required level 50, no cloak this time; a related weapon (Plundered Black
Tooth Face-Splitter, a different naming pattern and quality tier) excluded
the same way.

## Junk/decoy value floor added, then raised 500 -> 2000 (2026-08-01)

Human request: filter obviously-not-worth-sniping rows out of the SQL
candidate pool before `class_quotas`/`max_per_item`/`top` ever see them, so
a tier's limited row budget isn't spent on junk. `snipe_check.MIN_VALUE_FLOOR_G`
drops a row only when **both** the sell price and the EU region median are
under the floor -- an OR-to-keep, AND-to-drop threshold (the human
explicitly corrected an initial AND-to-keep misreading mid-conversation): an
item can be genuinely worth sniping on the strength of just one of the two
numbers, so requiring both to be healthy would wrongly cut those. `None`
(the default, every CLI call, every existing test) preserves prior
behavior; `dashboard.py`'s `/api/snipes` always passes the real value,
unconditionally, not user-configurable (the existing `min_sell_now` query
param remains the user-adjustable floor; this is a baseline beneath it).
Shipped at 500, then raised to 2000 the same day, both human-specified
numbers -- the test that exercises the OR/AND logic
(`test_find_snipes_min_value_floor_keeps_row_if_either_price_clears_it`) was
rewritten to derive its gold amounts from the real constant instead of
hardcoding values tuned to the old threshold, so a third change doesn't
silently break it again.

## Production incident: Postgres pool exhaustion + Blizzard rate-limit storm (2026-08-01)

The single biggest incident this project has had. A human report of
"wrong sell prices, pictures missing, login failing" led to two real,
independent, pre-existing architectural weaknesses being triggered
back-to-back, both exposed by the assistant's own live-debugging activity
against production that day (not a code deploy) -- the first genuine
"load test" this system had ever experienced.

**Trigger**: investigating an unrelated question ("why no housing items")
via `railway ssh`, the assistant ran an uncapped `item_names.NameCache.
ensure_many()` call resolving thousands of items directly against the live
Blizzard API, with zero coordination with anything else in the process.
Confirmed live via production logs: this immediately starved
`collect_all.py`'s background collector of its share of the shared rate
budget (`HTTP 429` errors, several real realms' collection cycles failing
outright -- e.g. `collect_all: realm 1390 failed`, `scanner: cr 3681: sweep
failed`).

**Cascade**: the resulting resource contention degraded overall request
latency, which pushed real `/api/snipes` calls (already documented
elsewhere as 30-175s cold) even longer than their normal worst case. Each
one held a Postgres connection checked out for its *entire* duration --
`current_active_user`'s `Depends()` chain only releases a connection after
FastAPI has fully sent the response (confirmed by reading
`fastapi/routing.py`'s `AsyncExitStack` handling), and `/api/snipes` was
the one route in the app doing that much unrelated slow work after
authenticating. The connection pool had never been explicitly tuned
(SQLAlchemy's bare defaults: `pool_size=5, max_overflow=10`, 15 total).
Enough overlapping slow requests holding connections finally exceeded that
ceiling for the first time ever, breaking login for everyone
(`sqlalchemy.exc.TimeoutError: QueuePool limit ... reached`).

Mitigated same-day with an immediate service restart (`railway restart`)
to clear the exhausted pool, then two rounds of real fixes:

**Round 1 (stopgap + first structural fix, commit `831c0b9`)**: `db.py`'s
`engine()` raised the pool 15 -> 60 connections (`pool_size=20,
max_overflow=40`) plus `pool_pre_ping=True` -- headroom, not a fix for
connections being held during unrelated work. `blizz.py`'s `api_get()`
gained two shared, thread-safe token buckets (`_burst_limiter`: 90/s,
`_hourly_limiter`: ~9.44/s sustained) so every caller in the process --
collector, scanner, `NameCache`, `AppearanceCache`, any future ad-hoc
script -- shares one throttled budget instead of being able to starve each
other. Confirmed live the regular 10-minute collection cadence only ever
uses ~2.2% of the hourly budget (~810 of 36,000 req/h), so it was
deliberately left unchanged -- the incident was an uncoordinated *burst*,
not the steady-state cadence, and slowing the cadence to hourly would cost
real data freshness for negligible safety benefit.

**The rate limiter introduced its own regression**: confirmed live via
`railway logs --http`, `/api/snipes` against Draenor started taking
158-300+ seconds on a cold cache. Root cause: before the limiter,
`NameCache.ensure_many()`/`.ensure_icons_many()` silently gave up fast on a
429 and moved on; the limiter turned that into waiting patiently for the
shared budget instead, turning a bounded-latency path into an unbounded
one. Diagnosed live (a single `/api/snipes` call's timing before/after
killing the assistant's own interfering diagnostic scripts) before being
designed around, not guessed at.

**Round 2 (the real structural fixes, planned via `/plan` and approved
before implementation, commit `17f2efb`)**:
- `auth.py` gained `resolve_user_from_request(request)`, which resolves the
  user via a session opened and closed in milliseconds -- not
  `Depends(current_active_user)`'s request-scoped chain. Mirrors
  `fastapi_users.current_user(active=True)`'s real behavior exactly
  (confirmed by reading the installed package's `Authenticator._authenticate()`/
  `JWTStrategy.read_token()` -- only the `active` check applies, since this
  app never uses `verified=True`/`superuser=True`). `dashboard.py`'s
  `_enforce_realm_lock()` deliberately kept its *exact* prior signature
  (still takes an explicit `session`) so its existing test coverage,
  including a real two-independent-sessions TOCTOU race regression test,
  stayed untouched -- only *where* `api_snipes()` gets that session from
  changed (a short-lived block opened just for that one call).
- `item_names.py`'s `ensure_many()`/`.ensure_icons_many()` gained an
  optional `deadline_seconds` param (`LIVE_RESOLVE_DEADLINE_SECONDS=15`,
  human-specified) -- once elapsed time exceeds the deadline, the resolution
  loop stops waiting and returns with whatever's resolved so far, same
  self-healing pattern as `CLASS_QUOTA_RESOLVE_LIMIT`/`PREWARM_BASE_LEVEL_CAP`.
  `None` (background callers, e.g. the prewarm loop) preserves unbounded
  patience; both live-request call sites (`dashboard._build_rows()`,
  `snipe_check._register_class_quota_maps()`) pass the real deadline.
  Required abandoning `with ThreadPoolExecutor(...) as pool:` in favor of
  explicit `pool.shutdown(wait=False, cancel_futures=True)`, since the
  context manager's default `__exit__` blocks on every submitted future
  regardless of any deadline.

24 new/updated tests across `test_auth.py`, `test_dashboard.py`,
`test_item_names.py`, `test_snipe_check.py` for round 2 alone. 328 passing
both with and without `DATABASE_URL` after round 2; live-verified after
deploy that a cold Draenor load now returns in bounded time instead of
158-300+ seconds, and that login/pool behavior held under the same
sequential-slow-request pattern that broke it originally.

**Also traced and fixed during the same investigation**: `fetch_snapshot.py`/
`scan_region.py`'s `setup_logging()` used `logging.StreamHandler()` with no
argument, which defaults to `sys.stderr` -- Railway's log platform tags
anything on stderr `"severity":"error"` regardless of the real Python log
level, so ordinary `log.info("scanner: cr X: Y listings")` lines were
showing up error-tagged, burying genuine errors in noise. Pre-existing, not
from anything shipped that day. **First fix didn't actually work**: those
functions are only invoked when the files run standalone via their own
`__main__` block, not when `collect_all.py` calls their functions as a
library from inside `dashboard.py`'s background loop -- in that path
(the real, running-in-production one), the `collector`/`scanner` loggers
have no handlers of their own and propagate to `dashboard.py`'s *own*
root-logger `logging.basicConfig()`, which was independently still
defaulting to stderr. Confirmed still happening live after the first fix
deployed; fixed for real by also passing `stream=sys.stdout` there.

## Active-users admin endpoint added (2026-08-01)

Human request, arising from the incident investigation above ("is it
whenever a user fetches data?" -> "how many users are even on the site").
`dashboard.py` gained a `track_activity` middleware recording a per-client-IP
last-seen timestamp for every `/api/*` request, and a superuser-gated
`GET /api/admin/active-users` surfacing IPs seen in the last 15 minutes.
`_client_ip()` reads `X-Forwarded-For`'s first entry rather than
`request.client.host` -- confirmed live the latter shows Railway's internal
edge-proxy hop (an `fd12:...` address, matching `railway logs --http`'s own
`upstreamAddress` field), not the real visitor, while `X-Forwarded-For`
matches that same command's `srcIp`. Deliberately IP-based, not resolved to
a full account identity (cheap, no per-request DB lookup) and deliberately
in-memory only, resetting on redeploy -- an observability convenience for a
genuinely low-traffic period (confirmed live at the time: 1 distinct
visitor, 15 requests over 2 hours, matching the dashboard's 60s
auto-refresh polling), not a new analytics system. `railway logs --http
--filter "@srcIp:..."` already gives the same underlying data ad hoc; this
just makes "how many distinct visitors right now" a one-click authenticated
check instead.

`pytest -q` and `env -u DATABASE_URL pytest -q`: 333 passing.

## TSM sale-rate filter added (2026-08-01)

Human request: "Lets work having a sellrate for each item from TSM, is this
possible?" -- clarified via a follow-up question to "a filter, where the
user can set a minimum sellrate," then confirmed the specific fields
wanted ("Yes it is the region saleRate and soldPerDay") after checking
what TSM's public feed actually exposes.

**ToS check before building**: `WebFetch` was blocked (403) on every TSM
URL tried (`tradeskillmaster.com/terms`, `robots.txt`,
`public-data.tradeskillmaster.com/`, the support-docs page) -- worked
around by switching to a real `claude-in-chrome` browser session, which
wasn't blocked. First guessed ToS URL (`/terms-of-service`) 404'd; the
real one, found via the page's own footer link, is `/terms`. Read both the
real ToS (generic e-commerce boilerplate, no clause against third-party
tools consuming the public feed) and the Public Data API docs
(`support.tradeskillmaster.com/en_US/api-documentation/tsm-public-web-api`,
explicitly invites third-party tools to pull the feed, and separately
invites "more programmatic usage" to reach out to
`admin@tradeskillmaster.com` first). No hard blocker found; that email
hasn't been sent yet, worth doing before this sees heavier production
traffic.

**Schema discovery**: TSM's public CSVs come in two shapes --
per-realm (`realm/{slug}/items.csv`, updates ~3h, has `marketValue`/
`minBuyout`/`recent`/`historical` but *no* sale-rate fields) and
region-wide (`region/items.csv`, updates ~daily, the only place
`saleRate`/`soldPerDay` actually live). This fixed the feature's shape
before any code was written: EU-region-wide liquidity, not sell-realm-
specific, same relationship `region_median_g` already has to the sell
realm's own price.

**Implementation** (four pieces, same review/confirm-before-building
pattern as the incident-prevention plan earlier the same day):

1. `tsm.py` (new): `SaleRateCache`, same `AppearanceCache`/`NameCache`
   display/filter-only convention -- never raises, `_fetch_csv()` returns
   `{}` on any failure, `refresh_if_stale()` keeps stale data on a failed
   refetch rather than clearing it. **Single-writer by design from the
   start**, not found live and fixed later like `item_names.py`'s same-day
   race: only `collect_all.py`'s background loop calls
   `refresh_if_stale()`/writes the cache file; every live-request read
   only calls `.get()`. Live-tested directly (`tsm._fetch_csv()` against
   the real feed): 30,984 items parsed successfully.
2. `collect_all.py`: refreshes the cache once per background cycle (after
   the base-level prewarm block), wrapped in its own `try/except` so a TSM
   outage can't break realm collection, reported in the cycle summary as
   `tsm_refreshed`.
3. `snipe_check.py`: `find_snipes()` gained `min_sale_rate`, applied via a
   new `_filter_by_sale_rate()` -- same post-SQL-pool shape as
   `_filter_by_appearance()`, attaches `region_sale_rate`/
   `region_sold_per_day` to every row unconditionally (both `None` when
   TSM has no data: never tracked, a caged pet since `item_id=82800` is
   shared across every species, or a cold cache) and, when a minimum is
   set, excludes rows with no TSM data rather than letting them through.
   Triggers the same SQL-widening path `max_appearance_sources` already
   uses, since a post-SQL filter can only shrink what the `LIMIT` already
   cut down to.
4. `dashboard.py`/`dashboard.html`: the two new fields pass through
   `_row_to_json()` unconditionally (not gated behind `names=true`, same
   as `region_median_g`); a new "Min sale rate %" filter-rail input
   (0-100, converted to a fraction, matching `min_discount_pct`'s
   convention) and a "Sale rate" hover-tooltip row. `CACHE_VERSION`
   bumped 6 -> 7 for the row-shape change. The percentage-input parsing
   deliberately uses the same `.trim() ? ... : null` pattern (not
   `Number(x) || null`) as the "Max per item" fix from the day before --
   `"0"` here is a real filter (excludes untracked items), not the same
   as leaving the field blank.

**Test isolation gap found three times, same root cause each time**:
`tsm.CACHE_PATH` is computed once at import time from `tsm.DATA`, so
patching `DATA` after import doesn't retroactively move it -- the exact
same lesson already learned for `item_names.py`/`appearance.py` earlier in
the project, re-learned here because a new module reintroduced the same
pattern. First found in `test_collect_all.py`'s `env` fixture (confirmed
live: suite runtime dropped 1.75s -> 0.75s once real TSM network calls
during tests were actually stopped), then found missing the same way
(silently reading whatever was in the real project's `data/
tsm_sale_rates.json` instead of crashing, since the read paths are all
non-raising) in `test_snipe_check.py` and `test_dashboard.py`. Fixed all
three with an `isolate_tsm_cache` autouse fixture matching the established
`isolate_appearance_cache`/`isolate_item_names_cache` pattern exactly.

**Frontend verified in a real browser**, not just by reading the diff (per
this project's own "Definition of done"): no local Postgres was running,
so a throwaway `postgres:16-alpine` Docker container was spun up, migrated
with `alembic upgrade head`, and torn down again after -- registered a
real local account, logged in, loaded real Draenor data (a genuine cold
`/api/snipes` call against real local snapshot/listing parquet files), and
confirmed live: the filter input renders in the rail, the tooltip shows a
"Sale rate" row with a real percentage against real data, typing a high
threshold (50%) correctly empties the table, a low threshold (1%) narrows
it without erroring, and clearing the field restores the full unfiltered
set exactly. No console errors during any of it.

`pytest -q` and `env -u DATABASE_URL pytest -q`: 352 passing.

## Dedicated Sale Rate % column added (2026-08-01, same day)

Human follow-up after the deploy above went out but hadn't populated data
yet: "i dont see the sellrates on the dashboard???". Checked production
via `railway ssh` first rather than assuming a code bug -- the deploy had
restarted the app (18:37:58) and the first post-deploy background cycle
was still mid-sweep through the ~100 EU realms; `tsm.SaleRateCache().
refresh_if_stale()` runs after the deep-collect/scan/prewarm steps in that
cycle, so `data/tsm_sale_rates.json` genuinely didn't exist yet. Explained
the timing, plus that the shipped design deliberately had no dedicated
table column (tooltip + filter only, matching `min_sell_now`'s
no-column precedent) -- and scheduled a wakeup to confirm the cache
populated.

Before that wakeup fired, a second, more specific request landed: "There
needs to be a sellrate columnn... in 0,00003 ever deci is important" --
a dedicated column was wanted after all, and with real decimal precision,
not the tooltip's original `toFixed(0)` whole-number percent (which would
round a real `0.00003%` down to `0.0`, indistinguishable from "no TSM
data"). Added a "Sale rate %" column (`static/dashboard.html`) between
"EU median" and "Discount %", sortable via a new `sale_rate` key in
`SORT_VALUE`/`NUMERIC_SORT_KEYS` (nulls sort last, reusing `compareRows()`'s
existing null-handling, no new logic needed there). New shared
`formatSaleRate()` helper (fixed at 5 decimal places, matching the human's
own example precision exactly) used by both the column cell and the
tooltip row, so the two can never show different-looking numbers for the
same value; `null` renders as `-`, matching the `variant` column's
"nothing to show" convention rather than a misleading `0.00000`. Column
widths across all eight columns were redistributed (mostly taken from Item
and the three money columns, which had the most headroom) to fit the new
one at 100% total.

Verified live in the same throwaway-Postgres local-browser setup as the
filter/tooltip work earlier the same day: real Draenor data painted from
`localStorage` cache (the fetch was skipped since `/api/status` hadn't
advanced since the last real fetch -- confirms `checkForUpdates()`'s
existing skip-if-unchanged logic still behaves correctly with the new row
rendering), the column shows real 5-decimal values (e.g. `5.90000`),
clicking the header sorts descending by sale rate correctly, and the
tooltip's "Sale rate" row matches the column's value exactly via the
shared formatter.

`pytest -q`: 352 passing (no Python-side change, `static/dashboard.html`
only).

## Multi-WoW-account registration + "Your account" column added (2026-08-02)

Human request: "Lets add a 'multi-wow-account-service'. So the idea is,
that a user on their profile can 'add' wow accounts... on each account
there should be the option to add realms, where the user is has snipe
toons on... display to the user, when a realm with a snipe pops up on the
dashboard, the user should be able to see which wow account they actually
should log into." Confirmed via a follow-up AskUserQuestion round: a
dedicated table column (not just a badge/tooltip), gated behind
`current_subscribed_user` (not the free tier -- same restriction as the
paid sniping feature itself), and human-specified caps (10 WoW accounts
per user, 50 realms per account).

**Full Plan Mode workflow** (two parallel Explore agents for backend/
frontend research, one Plan agent for the design, then a manual review
pass reading the actual code before finalizing): the backend Explore agent
confirmed realm identity throughout this app is always an integer
Blizzard connected-realm id (`snipe_check.find_snipes()` aliases it
`buy_realm`), never a name, so the new feature's matching key had to be
that same integer, not anything typed in by hand. The frontend Explore
agent confirmed there was no existing "add another X / remove X"
repeatable-list UI pattern anywhere in this codebase to mirror -- this
was genuinely new interaction, not an adaptation.

**One real correction found only by directly reading the code, after the
Plan agent's first draft assumed otherwise**: `/api/realms` (the existing
sell-realm-list endpoint) turned out to be a plain `def`, not `async def`
-- Starlette runs synchronous route handlers in a worker thread
automatically, so it was already safe from the "blocking Blizzard call on
the event loop" class of bug this app has hit twice before (that lesson
specifically applies to blocking calls made *inside* an `async def`
route, e.g. `api_snipes()`, which is why that one needs explicit
`asyncio.to_thread()`). The new `GET /api/realms/eu` endpoint (full EU
realm list, not just snapshotted ones -- backs the new realm picker on
`profile.html`) was built the same plain-`def` way instead of the
originally-planned `async def` + `asyncio.to_thread()` wrapper --
simpler, and correct for the same underlying reason.

**Data model** (`db.py`): `WowAccount` (`owner_id` FK to `user.id`,
`label`) and `WowAccountRealm` (`wow_account_id` FK, `connected_realm_id`,
`UniqueConstraint(wow_account_id, connected_realm_id)`). No ORM
`relationship()` on either side, matching this file's existing
plain-FK-column convention; no DB-level cascade delete (SQLite test
fixtures don't enable FK enforcement) -- `wow_accounts.delete_account()`
deletes child realm rows explicitly in the same transaction instead.
New Alembic migration `b3e0f41fe4e9` verified by hand against a real
scratch SQLite *and* a real scratch Postgres (`docker run
postgres:16-alpine`, `alembic upgrade head`, then `\d wow_account` /
`\d wow_account_realm` to eyeball the schema) -- confirmed important
during planning: every test fixture in this suite builds its schema via
`Base.metadata.create_all` directly against the ORM models, and CI never
runs `alembic upgrade head` at all (only `docker-entrypoint.sh` does, at
real deploy time), so the migration file's own correctness had no other
safety net.

**Backend** (`wow_accounts.py`, new): full CRUD -- list/create/rename/
delete accounts, add/remove realms per account. The 10-account/50-realm
caps are enforced via `_insert_account_atomic()`/`_insert_realm_atomic()`,
a single `INSERT ... SELECT ... WHERE (SELECT COUNT(*) ...) < cap`
statement rather than a separate `SELECT COUNT(*)` followed by a
Python-side `if` -- this app has two recorded TOCTOU bugs from exactly
that read-then-write pattern (`dashboard._enforce_realm_lock`'s old race,
`item_names.NameCache.save()`'s lost-update race), both fixed the same
way. Exposed as standalone functions specifically so tests can drive the
exact interleaving directly with two independent sessions, mirroring
`_enforce_realm_lock`'s own testability precedent -- new regression tests
(`test_create_account_concurrent_requests_only_ten_win`,
`test_add_realm_concurrent_duplicate_only_one_wins`) prove the atomic
statement is the real guard, not anything held in a Python object. The
duplicate-realm case doesn't need a second atomic check at all --
`WowAccountRealm`'s `UniqueConstraint` is the actual atomic source of
truth, caught as an `IntegrityError`.

**Frontend**: `profile.html` gained a second `.card` (widened to 640px)
with the account/realm management UI -- genuinely new interaction for
this codebase, built from scratch rather than adapted from an existing
pattern, reusing the inline-edit convention already proven by
`#nickname`/`#nickname-save` for each account's rename control, and
`dashboard.html`'s `populateRealmPicker()` fetch-then-build-`<option>`s
shape for the realm picker (against the new `/api/realms/eu` instead of
the existing `/api/realms`). Added a real regex-based `escapeHtml()`
(not the `div.textContent`->`innerHTML` trick that caused this project's
one real stored-XSS incident, see `snipeboard.html`'s own fix) since this
page interpolates untrusted strings into `innerHTML` for the first time.
`dashboard.html` gained a 9th "Your account" column, computed entirely
client-side from a new `userRealmAccounts` Map (populated once in
`init()`, skipped for free-tier accounts since they'd only ever get a
402) cross-referenced against each row's existing `buy_realm` field --
deliberately kept out of the cached per-realm row JSON and `CACHE_VERSION`
(this is per-user data; baking it into the shared `localStorage`
snipe-batch cache would leak one user's account labels into another
user's cache-inspection surface).

**Real bug found and fixed the same day, live in a real browser**: a
human report immediately after shipping -- "the diagram rows and columns
should not have a scroll bar... everything should be visible all the
time" -- traced to `th`'s unconditional `white-space: nowrap`. With 9
columns, the sum of every header label's forced-unbroken width (`table-
layout: fixed` only fixes each column's *proportional* width, it doesn't
stop unbreakable header text from forcing the table's overall rendered
width past 100% of `.table-wrap`) pushed the table wider than its
container, and `.table-wrap`'s own `overflow-x: auto` then showed a
persistent scrollbar. Fixed by letting header text wrap (`white-space:
normal`) instead of a narrower fix tied to one specific column or
viewport width -- removes the forcing case entirely. Verified live: no
scrollbar at the original window width or a resized-narrower one.

**Verified end-to-end in a real browser** (same throwaway-Postgres-plus-
local-server setup used earlier the same day for the Sale rate % work):
registered a superuser test account, added a WoW account, added/removed/
renamed it, added a second realm and confirmed the dashboard's "Your
account" column showed the right label only on matching rows, confirmed
column sorting puts matches first, deleted the account and confirmed its
realms cascaded, and confirmed a free-tier (non-subscribed) account never
fires `/api/wow-accounts` at all and sees a plain upsell message instead
of the management UI on `/profile`.

`pytest -q` and `env -u DATABASE_URL pytest -q`: 371 passing.

## Profile page redesign + connected-realm fan-out (2026-08-02, same day)

A rapid sequence of human follow-ups after the WoW Accounts feature above
shipped, each addressed in order:

1. "This whole adding wow accounts, needs a big UI/make sense update. Like
   its bad to list them, also the list should be searchable with a clear
   searchbar? Numbers will always be 1,2,3,4,5,6,7, or 8. So no need to
   'example main' etc." Clarified via an AskUserQuestion round: accounts
   should be numbered by default (auto-suggested "Account N", still
   renameable) rather than freely named from the start, and laid out as
   side-by-side cards rather than a stacked list.
2. "Also when displaying realms, pls make sure we always can see the full
   realm name." / "Also wait on displaying a users account, until the data
   is actually loaded" / "Infact remake the entire profile page.." -- all
   landed in quick succession while the account-cap backend change was
   still in progress, escalating scope from "fix the WoW Accounts card" to
   a full page rebuild.
3. "Also remove 'max per item' filter, not nessary anymore" and "Redo the
   flagged icon to something red danger" -- two smaller, unrelated
   dashboard.html requests folded into the same pass.
4. "Also its important that this works with connectedrealms, so if choose
   realm that has connected it auto adds them aswell" -- sent mid-
   implementation, after the realm search was already built against the
   original (one-entry-per-connected-realm) `/api/realms/eu` shape.

**Account cap lowered 10 -> 8** (`wow_accounts.MAX_ACCOUNTS_PER_USER`) --
matches Blizzard's real per-Battle.net-account WoW account limit, the
human's own stated reasoning ("numbers will always be 1...8"). Test suite
updated to match (`test_create_account_enforces_max_8_cap`,
`test_create_account_concurrent_requests_only_eight_win`, seeding 7 instead
of 9 accounts before racing for the cap).

**`static/profile.html` rebuilt from scratch**: `.shell` widened from a
narrow centered column to `max-width: 1100px`; the profile card became a
compact horizontal summary (email + badge + inline nickname) instead of
stacked labeled rows; the WoW Accounts section is now `#wow-accounts-list`,
a CSS grid (`repeat(auto-fill, minmax(260px, 1fr))`) of cards with a dashed
"+ Add account" tile as the trailing grid item (becomes a disabled
"Maximum 8 WoW accounts reached" tile at the cap). `addAccount()` no longer
takes a text input at all -- it POSTs `label: "Account " + (currentAccounts
.length + 1)` directly, with rename still available via each card's own
inline input (a `.cap-note` under it -- "Press Save to save changes." --
makes explicit that renaming doesn't auto-save on blur, added after a
direct human request for exactly this).

**Loading skeleton**: three pulsing `.skeleton-card` placeholders
(`@keyframes skeleton-pulse`) show from the moment the account is confirmed
subscribed until both `/api/wow-accounts` and `/api/realms/eu` resolve
(`Promise.all`) -- verified live in a real browser (caught the actual
skeleton mid-render via a screenshot taken immediately after navigation).

**Realm search replacing the `<select>` dropdown**: each card gets its own
text input + an absolutely-positioned results dropdown, filtered live
against a flat `realmSearchEntries` array (substring match, capped at 8,
sorted by name). `Enter` picks the top match; result-item clicks use
`mousedown` rather than `click` specifically so the pick fires before the
input's own `blur` handler (bound with a 150ms grace `setTimeout`) could
race the dropdown closed. Realm chips and search-result items have no
truncation anywhere (`white-space: normal; word-break: break-word`, no
`overflow:hidden`) per the direct "always see the full realm name" request.

**Connected-realm fan-out**, the most structurally significant of the
follow-ups: `/api/realms/eu` used to return one entry per connected-realm
id (via the existing `_realms_payload()`, which only exposes the first/
"primary" member realm's name via `_realm_info()`). A connected realm can
bundle several named realms sharing one AH -- a user searching for their
actual character's realm name might not find it if that name happens not
to be the "primary" one Blizzard's API returns for that connected realm.
New `_connected_realm_members_cache`/`_connected_realm_members(cr_id)`
(deliberately separate from `_realm_info_cache` -- every other caller of
`_realm_info()` genuinely wants just the one display name) backs a rewrite
of `api_realms_eu()` that fans out to one entry per member realm name, with
multiple entries sharing the same `id` when a connected realm has more
than one. `profile.html`'s `realmSearchEntries` (flat list, for search) and
`realmNameById` (deduped to one name per id, for chip display) split the
same response for the two different needs. **Confirmed live against real
data while manually verifying the fix**: searching "Zul'jin" and picking it
added a realm whose chip displayed as "Sanguino" -- the two are the same
connected realm, with Sanguino as Blizzard's primary name for it -- and the
dashboard's "Your account" column then correctly matched a real Zul'jin-ES
buy-side row against that same registration. New regression test:
`test_api_realms_eu_fans_out_connected_realm_members`.

**Also same day, `static/dashboard.html`**:
- **"Max per item" filter removed** (human request, "not necessary
  anymore") -- the `#max_per_item` input, its `buildGroups()` per-group cap
  logic, and its `LIVE_FILTER_IDS` entry are gone; "Min sell price (g)"
  (previously paired with it in a `.filter-row`) is now a standalone
  full-width label. Purely a client-side removal -- the server-side
  `max_per_item` param on `snipe_check.find_snipes()`/the CLI/`/api/snipes`
  was never sent by this page in the first place, and is untouched.
- **Sus-item flag restyled** (human request, "redo the flagged icon to
  something red danger") -- `.sus-flag` changed from a deliberately-muted
  brown (`#7c6a52`, chosen 2026-07-31 specifically to read as an identity
  note rather than a caution) to `var(--alert)` red, and the glyph itself
  from an hourglass (⌛) to a warning triangle (⚠) -- direct human
  preference overriding the earlier design reasoning.

**Verified live in a real browser** (same throwaway-Postgres-plus-local-
server setup used earlier the same day): caught the loading skeleton
mid-render, confirmed numbered-account auto-suggestion and the card grid
layout, confirmed the realm search dropdown and pick-to-add flow, confirmed
the connected-realm fan-out with real data (Zul'jin/Sanguino, see above),
confirmed the red warning-triangle sus-flag renders on real flagged rows,
and confirmed dark mode renders correctly throughout the redesigned page.

`pytest -q` and `env -u DATABASE_URL pytest -q`: 372 passing.


## Pre-push double-run retired; DB hermeticity enforced in conftest (2026-08-04)

The human asked why the workflow runs the tests twice. The answer turned out
to be worse than "redundantly": one of the two runs had been dead for some
time, and nobody could have noticed, because a no-op check passes exactly
like a real one.

**The ritual.** Since the 2026-08-01 CI incident (see "CI went red on push"
above -- `/api/snipes` gained a direct `Depends(get_async_session)`,
`test_dashboard.py` had no override for it, 17 tests died in CI on dependency
resolution while passing locally), every push was supposed to run both
`pytest -q` and `env -u DATABASE_URL pytest -q`, the second one "matching
CI's environment."

**It matched nothing.** `blizz.py`'s `_load_env()` runs at import time and
does `os.environ.setdefault(k, v)` for every line of `.env`, and `.env` has a
`DATABASE_URL=` line. `db.py` imports `blizz` directly. So the shell's `env
-u` unset survived only until the first import of anything under test, at
which point `setdefault` put the value straight back. Verified directly:

    $ env -u DATABASE_URL python -c "import os; \
        print('before:', 'DATABASE_URL' in os.environ); import blizz; \
        print('after: ', 'DATABASE_URL' in os.environ)"
    before: False
    after:  True

Roughly 40 seconds per change, for years' worth of pushes, buying nothing.
Worth noting *how* it rotted: the second run was genuinely effective when it
was introduced. It stopped being effective the moment a `DATABASE_URL` line
was added to a local `.env` -- a change with no visible connection to the
test suite, in a file git never sees. There was no failing signal to catch,
because the check's output is identical whether it is working or not.

**No hidden backlog.** The real CI-equivalent is an empty string, not an
unset var (`setdefault` skips a present-but-empty key, and
`db._database_url()` does `if not url: raise`). `DATABASE_URL="" python -m
pytest -q` -> 441 passed. So nothing had actually drifted behind the dead
check; the suite was clean, just unverified.

**Fix, two layers.** Root `conftest.py` now hard-assigns
`os.environ["DATABASE_URL"] = ""` -- it has to be `""` rather than a `del`
(an absent key is exactly what `setdefault` refills), a plain assignment
rather than `setdefault` (the whole point is to beat `.env`), and in the
*root* conftest rather than `tests/` (it must run before `import db` ->
`import blizz`).

That alone stops a real connection but fails badly. The negative test caught
it: deleting `autouse=True` from `test_dashboard.py`'s
`bypass_get_async_session` -- reproducing the 2026-08-01 incident exactly --
did not fail, it **hung pytest indefinitely**. `_database_url()` raises
`SystemExit`, which is a `BaseException`, and a `BaseException` raised inside
a route deadlocks Starlette's `TestClient` portal rather than propagating.
CI, which reaches the same code path via an absent `DATABASE_URL`, would have
burned its full job timeout instead of going cleanly red.

So `tests/conftest.py` (new) adds a `no_real_database` autouse fixture
patching `db._database_url` to raise a plain `RuntimeError` naming the
offending test. `_database_url()` is the single chokepoint all three seams
funnel through -- `engine()`, `sessionmaker()` (via `engine()`), and
`isolated_session()` -- so one patch covers everything. Re-running the same
broken-fixture test with the guard in place: **failed in 2.15s**, with the
node id and a pointer to the canonical override pattern. Restored, full suite
green at 441.

**New workflow.** Locally, run only the tests covering the change
(`tests/test_<module>.py`; full suite for `db.py`/`auth.py`/`conftest.py` or
anything cross-cutting; nothing at all for `static/*.html`, which has no
Python coverage -- browser-verify instead). CI runs all 441 on every push and
gates the Railway deploy, so it is the real full-suite gate; `/ship` still
waits for it to go green. A local pass now genuinely predicts CI, which is
what makes the narrower local run safe. Updated: `/ship`, `CLAUDE.md`'s
`tests/` row and definition-of-done, `progress.md`, the `project-review`
skill, and two now-dead `env -u DATABASE_URL` entries in
`.claude/settings.local.json`.

**Sign-off convention changes from here on** to a single `pytest -q`. Every
"`pytest -q` and `env -u DATABASE_URL pytest -q`: N passing" line above is
left untouched -- each was an accurate statement of what was run at the time.

**Same session, separate concern: `CLAUDE.md` trimmed 160,941 -> 101,574
chars** (-37%). That file is loaded into context at the start of every
session, and three rows -- `snipe_check.py` (20,648), `dashboard.py`
(20,039), `static/dashboard.html` (27,793) -- were 43% of it, making it by
some margin the largest recurring cost in the project. `CLAUDE.md`'s own
preamble already says it is the current-state reference and that this file
holds the narrative; it had simply drifted. Checked first whether the detail
was duplicated here -- it was **not** (`Scarshield`, `QuotaExceededError`,
`evictOldestOtherCache`, `RENDER_PAGE_SIZE`, `table-layout`,
`isBackgroundRefresh`, `LAST_SELL_KEY` all returned zero hits), so those rows
were the sole record. Moved verbatim into the archive section below rather
than summarized, then rewritten as current-state summaries. Nothing deleted.

## Archived: verbatim `CLAUDE.md` file-table detail (migrated 2026-08-04)

Three CLAUDE.md rows had grown to 20-28k characters each (43% of a
160,941-char file that is loaded into context every session). Per
CLAUDE.md's own stated policy -- it is the current-state reference, this
file is the narrative -- their dated incident detail was moved here verbatim
and the rows rewritten as concise current-state summaries. Nothing was
deleted; everything below is the exact prior text. It is not in
chronological order with the rest of this file, because it is a migration of
accumulated per-file notes rather than a single session's log.

### `snipe_check.py`

Joins `data/listings/*.parquet` against the sell realm's own **current
cheapest live listing** (not a sold-price percentile — see "What this
project is" above) on

**`(item_id, pet_species_id, pet_quality_id)`** (changed 2026-07-26,
dropping `market_key(bonus_key)` from the match key entirely — see "What
this project is" above's matching-model note and "Inference logic" below).
`find_snipes()` params: `items`/`min_discount`/`min_gold`/`max_gold`/`min_se
ll_now`/`max_appearance_sources`/`max_per_item`/`class_quotas`/`min_value_fl
oor_g`/`top`/`sort`.

**`SORT_COLUMNS` gained a final `auction_id` tiebreaker** (2026-07-31, human
report "items switch even with no filters touched" + live repro: re-running
`find_snipes()` twice against completely unchanged local data returned 66
different rows out of 200) — `discount_pct DESC, buy_g ASC` (the tiebreak
added 2026-07-28 for the same underlying reason, see its own dated comment
in the source) still leaves plenty of real rows tied on *both* keys (round-
number decoy prices are common in the near-100%-discount pool), and DuckDB
doesn't guarantee stable row order for ties in a parallel query plan — so
which tied row won a final `LIMIT` boundary, or either `ROW_NUMBER()` cutoff
(`capped`'s `item_rank` for `max_per_item`, `class_ranked`'s `class_rank`
for `class_quotas`, both updated to match) could differ between two
otherwise-identical calls, with no data actually changing. `auction_id` is
stable for a listing's lifetime and unique (see "Blizzard API facts" below),
so appending it to all three `ORDER BY`s makes the full result deterministic
— confirmed live (10 repeated calls, identical every time) and covered by `t
ests/test_snipe_check.py::test_find_snipes_fully_tied_rows_are_deterministic
_across_calls`. `check_data_ready(sell) -> str \| None` is the shared "at
least one snapshot exists + listings swept" precondition (changed 2026-07-25
from an events-file check — see `collect_all.py`'s row below), used by both
the CLI and `dashboard.py`'s `/api/snipes`. `_filter_by_appearance(rows,
max_appearance_sources)` applies the transmog-rarity filter (via
`appearance.py`) and excludes `NON_TRANSMOG_INVENTORY_TYPES` (profession
tool/accessory slots).

**`is_sus_item(item_id, inventory_type, base_level)`** (added 2026-07-31 as
`is_legacy_jewelry`, renamed/broadened to `is_legacy_gear` same day, renamed
again the same day to `is_sus_item` per human naming preference — human
request throughout): a pure predicate flagging old, likely-obsolete gear two
ways — (1) `LEGACY_JEWELRY_INVENTORY_TYPES = {"NECK", "FINGER",
"TRINKET"}`/`LEGACY_JEWELRY_ILVL_MAX = 150` (tunable), old jewelry from
long-superseded expansions — confirmed live via `blizz.api_get()` against
real items (Charm of Potent and Powerful Passions/27982 ilvl 26 NECK,
Polished Pendant of Edible Energy/27976 ilvl 22 NECK — sold by an NPC vendor
for 25g, Ornate Band/83793 ilvl 37 FINGER, Shadowfire Necklace/83794 ilvl 37
NECK; current-expansion jewelry sits at ilvl 600+ post-squish) — unlike
weapons/armor, these slots have no transmog-appearance value at all, so
there's no collector-value angle that could make an ancient one worth
flagging; (2)

**`CLASS_STARTER_ARMOR_ITEM_IDS`**, a curated 52-item id set, not a
threshold rule (ilvl alone can't discriminate here — hundreds of unrelated
epic/rare transmog/PvP-replica items also sit at ilvl 1) — confirmed live:
patch 9.1.5 (2021-11-02) introduced a unified `"{Class}'s {Slot}"` ilvl-1
Common starter armor set per class (e.g. Paladin's Girdle/187726, Plate,
WAIST), found via the contiguous id range 187690-187778 — 9 classes
confirmed (Hunter/Shaman/Paladin/Warrior/Warlock/Mage/Priest/Rogue/Druid, 7
with all 6 slots, Rogue/Druid missing WRIST), Death Knight/Demon
Hunter/Monk/Evoker checked and NOT found (bespoke starting gear from their
own class-launch expansion instead).

**`SLITHERSHELL_ARMOR_ITEM_IDS`** (added 2026-08-01, human request naming
the set directly, e.g. Slithershell Armwraps): a second curated id set,
unioned with `CLASS_STARTER_ARMOR_ITEM_IDS` into `CURATED_SUS_ITEM_IDS` (the
single set `is_sus_item()` actually checks) — a naga-themed leveling quest-
reward armor set, not class-starter gear, confirmed live via
`blizz.api_get()`'s item search (single page, 10 total results for
"Slithershell"): 8 Leather pieces
(CHEST/FEET/HAND/HEAD/LEGS/SHOULDER/WAIST/WRIST) + 1 Cloth CLOAK, all
UNCOMMON quality, ilvl 58, required level 50. The 10th result, Slithershell
Warglaive (170119, Weapon/Warglaives), is deliberately excluded — the human
asked for "armors" specifically.

**`BLACK_TOOTH_GRUNT_ARMOR_ITEM_IDS`** (added 2026-08-01, human request
naming the set directly, e.g. Black Tooth Grunt's Armplates): the Plate
counterpart to Slithershell, same pattern, also unioned into
`CURATED_SUS_ITEM_IDS` — confirmed live by scanning all 8 pages of
`blizz.api_get()`'s item search for the exact substring "Black Tooth Grunt"
(Blizzard's search API ORs individual words, so "Black"/"Tooth" alone each
match hundreds of unrelated items — a plain single-page search isn't enough
here the way it was for the rarer "Slithershell" token): 8 Plate pieces
(CHEST/FEET/HAND/HEAD/LEGS/SHOULDER/WAIST/WRIST), all UNCOMMON quality, ilvl
60, required level 50, no cloak this time. Plundered Black Tooth Face-
Splitter (169290, Weapon/Axe, RARE) uses a different naming pattern and
quality tier — deliberately excluded, same reasoning as Slithershell
Warglaive.

**Known blind spot**: level-bracket "twink" builds specifically value old,
low-ilvl neck/ring/trinket items (no level requirement or transmog
competition to worry about) — this can't distinguish dead vendor jewelry
from a genuinely twink-valuable item via Blizzard's API alone, so it's never
used to filter server-side — see `dashboard.py`'s row for how it's surfaced.
Every returned row carries the raw `bonus_key` (display only) plus
`pet_species_id`/`pet_quality_id` (dashboard groups by the latter two plus
`item_id`, not `market_key` anymore) and `appearance_sources`. Prints
`snipe_check.CAVEAT` every CLI run (the equip-before-transfer warning). No
longer imports/calls `fetch_snapshot.market_key()` or anything base-
level/noise-bonus-id related — that machinery moved conceptually to "manual
tooling only" (see `diff_snapshots.py`'s row).

**`region_median_g`/`region_median_copper`** (added 2026-07-27, human
request): every row also carries the statistical *median* of the same per-
other-realm cheapest listings, computed in the `region_stats` CTE — a median
is more honest as "the typical EU price" to show a human directly, since one
other realm's own outlier listing skews a mean far more. Purely
informational, gates nothing. `print_snipes()` shows it in a new `eu_med_g`
column. (

**`sell_price_suspect`**, a 500x-region-mean scam-price flag that used to
live alongside this — added 2026-07-27, traced live to Draenor item
36519/Moonlit Katana — was removed 2026-07-31, human decision;
`region_stats` used to also compute the mean this needed. See `history.md`.)

**`class_quotas`** (added 2026-07-27, human request, resolves the "decoy
listings crowd out entire categories" issue tracked in `progress.md`'s
"Known gaps/risks" and "Next up" since 2026-07-26): an optional `{bucket:
max_rows}` dict (bucket keys match `dashboard.html`'s `ITEM_CLASS_FILTERS` —
`weapon`/`armor`/`container`/`profession`/`housing`/`battlepet`/`quest`/`mou
nt`/`recipe` (the last added 2026-07-28, `item_class` 9, confirmed live via
`GET /data/wow/item-class/index` — distinct from Profession's 19), see
`CLASS_BUCKET_RULES`) that caps each item-class bucket independently instead
of one flat top-N by discount% — a real 88.1%-discount Housing snipe was
previously crowded out entirely by 99%+-discount decoy listings elsewhere,
confirmed live. `None` (the default, and every CLI call) preserves the exact
prior flat-ranking behavior.

**Rewritten same day** after an initial version (widen the SQL `LIMIT` to a
fixed ceiling, bucket/truncate in Python afterward) was proven insufficient
live: 450,568 rows qualified region-wide on Draenor at `min_discount=0`, and
the first genuine Housing candidate sat at rank 39,524 — past any
reasonably-bounded widening, so the "guarantee" wasn't actually one. The
real fix: `capped` (the item-deduped match set) is materialized into a `TEMP
TABLE` with **no row-count truncation at all**; `item_class`/`item_subclass`
are resolved for every *distinct* item id in it via `NameCache`
(`_register_class_quota_maps()`, bounded by
`CLASS_QUOTA_RESOLVE_LIMIT=20000` — raised from an earlier, too-low 2000
once the real distinct-item count, 15,258 on Draenor, was known; same worst-
case-latency reasoning as `collect_all.py`'s prewarm cap, self-healing as
the background prewarm catches up) and registered as two DuckDB relations
(`class_quota_item_map`/`class_quota_bucket_map`) via `con.register()`; a
real `ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY discount_pct DESC)`
then ranks and filters per bucket as a genuine SQL window function over the
*complete* candidate set — a guarantee, not a "wide enough in practice"
hope. A bucket missing from the dict (or given `0`) is excluded entirely.
`_class_bucket()`'s "mount" rule needs both `item_class==15 AND
item_subclass==5` since Mount is a subclass of the generic Miscellaneous
class, not its own item_class — a class-15 item with a different subclass
matches no bucket at all, same as any item_class this dict doesn't cover.
Verified live via `railway ssh` against real Draenor data both before
(finding the bug) and after (confirming the fix) shipping.

**`CLASS_QUOTA_PER_ITEM_CAP=3`** (added 2026-07-31, human report "why do I
only have 1 housing item" + live repro via `railway ssh`): `class_rank`
ranks by *row*, not by distinct item, and the separate/optional overall
`max_per_item` defaults to unset — so one real item with an unusually large
number of qualifying listings could consume most of a small bucket's quota
before any other item got a slot, the same "one thing crowds out everything
else" failure `class_quotas` itself exists to prevent at the whole-category
level, recurring one level down. Confirmed live: item 264709 (Stranglekelp
Sack, 663 qualifying region-wide listings) took 29 of the free tier's 40-row
housing quota (72%), 131 of subscribed's 285 (46%), 242 of superuser's 1,565
(15%) — leaving only 5/20/64 distinct housing items visible respectively,
before any client-side filter even ran. A new `class_item_capped` CTE (a
`ROW_NUMBER() OVER (PARTITION BY bucket, item_id, pet_species_id,
pet_quality_id ORDER BY discount_pct DESC, buy_g ASC, auction_id)`, filtered
to `<= CLASS_QUOTA_PER_ITEM_CAP`) runs before `class_rank`, always active
regardless of whether `max_per_item` is set. `3` is human-specified ("~3-5
rows per item," picked from a live example showing it leaves 37 of the free
tier's 40 housing slots for *other* items). Covered by `tests/test_snipe_che
ck.py::test_find_snipes_class_quotas_caps_one_item_hogging_its_own_bucket`.

**`_register_class_quota_maps()` now checks `NameCache.has_class_info()`
before calling `.item_class()`/`.item_subclass()`** (2026-07-31, real bug
fix found during a repo-wide audit): those two accessors transparently fall
back to a *blocking*, one-at-a-time network fetch for anything not already
cached — calling them unconditionally for every distinct candidate item
after `ensure_many()`'s own bounded/concurrent resolution meant any item
past `CLASS_QUOTA_RESOLVE_LIMIT` (or whose concurrent fetch merely failed
transiently) silently reintroduced the exact sequential-blocking-calls
failure mode that limit exists to prevent (see "Real production outage"
below). An uncached item now just gets no bucket for that call (self-heals
via `collect_all.py`'s background prewarm, same as the limit's own docstring
already claimed — this makes that claim actually true). Covered by `tests/te
st_snipe_check.py::test_register_class_quota_maps_never_blocks_past_the_reso
lve_limit`.

**`min_value_floor_g`/`MIN_VALUE_FLOOR_G=2000`** (added 2026-08-01, human
request, "cutting junk/decoy" from the SQL candidate pool so a tier's
top-N/class-quota budget isn't spent on rows nobody would actually snipe;
raised from an initial 500 to 2000 same day, human request, both human-
specified numbers): drops a `matches` row when **both** `sell_p_g` and
`region_median_g` are under the floor — an OR-to-keep, AND-to-drop threshold
(human explicitly corrected an initial AND-to-keep reading mid-
conversation), not a floor on either number alone, since an item can be
genuinely worth sniping on the strength of just one of the two. `None` (the
default, every CLI call, and every existing test via `test_dashboard.py`'s
`disable_value_floor` autouse fixture — same isolation precedent as
`default_item_class_stub`) preserves prior behavior; `dashboard.py`'s
`/api/snipes` always passes `MIN_VALUE_FLOOR_G`, not user-configurable (the
existing `min_sell_now` query param remains the user-adjustable floor, this
is an unconditional baseline beneath it). Covered by `tests/test_snipe_check
.py::test_find_snipes_min_value_floor_keeps_row_if_either_price_clears_it`
and `tests/test_dashboard.py::test_api_snipes_applies_the_junk_value_floor`.

**`min_sale_rate`/`_filter_by_sale_rate()`** (added 2026-08-01, human
request — "a filter, where the user can set a minimum sellrate," confirmed
the region-wide `saleRate`/`soldPerDay` fields specifically, see `tsm.py`'s
row): an optional post-SQL filter, same shape/placement as
`_filter_by_appearance()` — runs on the SQL candidate pool after it comes
back, before the final `rows[:int(top)]` slice, in both the `class_quotas is
None` and `class_quotas` branches. Every row gets
`region_sale_rate`/`region_sold_per_day` attached unconditionally (via
`tsm.SaleRateCache().get(item_id)`; both `None` for anything TSM has no data
for — never tracked, a caged pet (`item_id` 82800 is shared across every pet
species, and TSM has no per-species breakdown), or a cache that hasn't
refreshed yet). When `min_sale_rate` is set, a row with no TSM data is
excluded — `None` doesn't count as passing the threshold, it's treated as
"doesn't meet it," not "unknown, let it through." TSM only publishes
`saleRate`/`soldPerDay` on its **region-wide** files, not per-realm ones
(confirmed live parsing both file shapes) — so this is deliberately EU-
region-wide liquidity, not specific to the sell realm a row is priced
against, the same relationship `region_median_g` already has to `sell_p_g`.
Triggers the same SQL-pool-widening path as `max_appearance_sources`
(`_widen_pool`, `sql_limit = max(int(top) * 20, 1000)`), since a post-SQL
filter can only shrink the set the `LIMIT` already cut down to. CLI: `--min-
sale-rate`; `print_snipes()` gained a `sale%` column. Covered by `tests/test
_snipe_check.py::test_find_snipes_reports_sale_rate_without_filtering`/`::te
st_find_snipes_min_sale_rate_filters_illiquid_items`/`::test_find_snipes_min
_sale_rate_excludes_items_with_no_tsm_data`.

**`price_suspect`/`PRICE_SUSPECT_MULTIPLE=10`** (added 2026-08-03, human
request, "If sell price is 10x eu-median its a scam add this to hide
flagged"): a `matches`-CTE boolean, `sell_p_g >= PRICE_SUSPECT_MULTIPLE *
region_median_g` — a recalibrated revival of the `sell_price_suspect` flag
removed 2026-07-31 (see this row's own note above, traced live to Draenor
item 36519/Moonlit Katana), two deliberate changes: compares against the
already-computed **median** (`region_stats.region_median_cheapest`) instead
of the mean `sell_price_suspect` used (a mean is exactly the kind of
statistic one outlier realm can drag around — plausibly what produced that
false positive), and a much tighter **10x** instead of 500x, since a median
is already outlier-resistant so a tight threshold on it doesn't carry the
same blast radius a tight threshold on a mean would. Same house convention
as every other heuristic here: flag, never silently filter server-side — no
new checkbox, `dashboard.html`'s existing "Hide flagged (sniper filter)"
checkbox ORs `price_suspect` in alongside `sus_item_suspect`. Costs nothing
extra (pure SQL over columns already in `matches`), so `dashboard.py`
exposes it unconditionally, same as `region_median_g`, not gated behind
`names=true` like `sus_item_suspect` is. Covered by `tests/test_snipe_check.
py::test_find_snipes_price_suspect_flags_sell_price_over_10x_median` and `te
sts/test_dashboard.py::test_api_snipes_carries_price_suspect_flag_unconditio
nally`.

**`region_sale_avg_copper`** (added 2026-08-03, human request, "region sale
avg from tsm, if it exist"): `_filter_by_sale_rate()` now also attaches
TSM's `avgSalePrice` (see `tsm.py`'s row — already in copper, confirmed
live), `None` if TSM has no data for the item, same as
`region_sale_rate`/`region_sold_per_day` from the same call. Read via
`entry.get("avg_sale_price")`, not `entry["avg_sale_price"]` — self-heals
against a cache file written before this field existed, same convention
`item_names.NameCache`'s backfill logic uses, rather than a `KeyError` on an
otherwise-valid older cache entry. Purely informational, gates nothing
(unlike `min_sale_rate`, no filter param was requested for this one).
Covered by `tests/test_snipe_check.py::test_find_snipes_reports_region_sale_
avg_without_filtering`/`::test_find_snipes_region_sale_avg_none_when_tsm_has
_no_data`/`::test_find_snipes_region_sale_avg_none_for_cache_entry_missing_t
he_field`.

**`sniper_filter_suspect`/`SNIPER_FILTER_N=5`/`SNIPER_FILTER_CLOSE_MULTIPLE=
1.7`/`SNIPER_FILTER_MIN_REALMS=3`/`SNIPER_FILTER_HIGH_VALUE_EXEMPT_G=200000`
** ("Sniper filter" — named by the human, added 2026-08-04, human request,
prompted by a real screenshot: an item flagged as a snipe at 400g purely
because the sell realm/EU median were much higher, while the next 5 *other
unique realms'* own cheapest listings sat at 400/428/444/500/500g — a tight
cluster right next to the "snipe" price, not a lone outlier): a different
failure mode than `price_suspect` above — that one catches a troll/inflated
*sell*-side reference price, this one catches a buy-side candidate that only
looks rare because the sell realm happens to be pricier than usual, when the
wider region shows the buy price is actually common. New CTEs between
`matches` and `capped` (`sniper_filter_keys`/`sniper_filter_ranked`/`sniper_
filter_cluster`/`matches_flagged`) compute, per `(buy_realm, item_id, pet
identity)` combo already surviving the `matches` `WHERE` (scoped there
rather than the whole region, cheaper than `region_stats`' own
unconditional-over-`buy` computation, correctness unaffected since the flag
is only ever read off a `matches` row), the median of the `SNIPER_FILTER_N`
cheapest *other* realms' own cheapest listing for the same item
(`region_realm_floor`, already deduped to one row per realm — a single realm
spamming N copies of the same auction can't inflate the cluster, satisfying
the human's explicit "has to be unique realms" requirement) — flags when
that median sits within `SNIPER_FILTER_CLOSE_MULTIPLE` of the buy price.
Requires `SNIPER_FILTER_MIN_REALMS`+ other realms to even have a listing
before drawing any conclusion (`COALESCE(cluster_realm_count, 0) >=
MIN_REALMS` short-circuits the flag to plain `FALSE`, not `NULL`, when there
isn't enough data — same "unknown isn't a claim" convention as
`region_sale_rate`/`appearance_sources` being `None`). Deliberately **not**
the mirror case (a lone cheap listing far below every other realm) — that's
indistinguishable from a genuinely great snipe by price data alone
(explicitly raised and confirmed during design), so this flag only ever
fires in the "corroborated by nearby realms" direction.

**High-value exemption** (`SNIPER_FILTER_HIGH_VALUE_EXEMPT_G`, same day,
human request): never flags once both `sell_p_g` and `region_median_g` clear
200,000g, regardless of how close the cluster sits — on an expensive item a
proportionally-close cluster can still be a huge absolute gold gap, so the
ratio check alone is weaker evidence there. All four numbers are human-
specified, not agent-picked. Same house convention as
`price_suspect`/`sus_item_suspect`: flag, never silently filter server-side
— `dashboard.html`'s existing "Hide flagged (sniper filter)" checkbox ORs it
in alongside the other two. Covered by `tests/test_snipe_check.py::test_find
_snipes_sniper_filter_suspect_flags_a_crowded_price`/`::test_find_snipes_sni
per_filter_suspect_requires_minimum_realm_count`/`::test_find_snipes_sniper_
filter_suspect_exempts_high_value_items` and `tests/test_dashboard.py::test_
api_snipes_carries_sniper_filter_suspect_flag_unconditionally`.

### `dashboard.py`

FastAPI app, read-only web layer over `snipe_check.find_snipes()`. `GET
/api/snipes` mirrors the CLI's flags as query params, returns JSON rows +
raw-copper prices (`buy_copper`/`sell_copper`) +
`pet_species_id`/`pet_quality_id` (unconditional — replaced `market_key`
2026-07-26, see "What this project is" above's matching-model note) +
`region_median_g`/`region_median_copper` (added 2026-07-27, passed through
`_row_to_json()`'s explicit dict, purely informational — see
`snipe_check.py`'s row above) + `region_sale_rate`/`region_sold_per_day`
(added 2026-08-01, same passthrough pattern, unconditional and independent
of `names=true` — see `snipe_check.py`'s `min_sale_rate` row and `tsm.py`'s
row; both `None` when TSM has no data for the item) + `price_suspect` (added
2026-08-03, same unconditional-passthrough pattern — see `snipe_check.py`'s
`PRICE_SUSPECT_MULTIPLE` row; pure SQL, no `NameCache` lookup needed, unlike
`sus_item_suspect` below) + `region_sale_avg_copper` (added 2026-08-03, same
day, same unconditional-passthrough pattern — see `snipe_check.py`'s row and
`tsm.py`'s row; `None` when TSM has no data for the item) +
`sniper_filter_suspect` ("Sniper filter", added 2026-08-04, same
unconditional-passthrough pattern — see `snipe_check.py`'s `SNIPER_FILTER_N`
row; pure SQL, no `NameCache` lookup needed, same as `price_suspect`) +
`buy_realm_category` (added 2026-07-31, human request: which language
community the buy-side realm belongs to, e.g. `"German"` — straight
passthrough of `blizz.connected_realm_realms()`'s `category` field via
`_realm_info()`, unconditional like `buy_realm_name` since it's
cheap/already-cached, not gated behind `names=true`; display-only next to
the buy realm in `dashboard.html`'s table/tooltip, never used for matching
or filtering — the sell-realm picker deliberately does *not* show this,
human call) + (when `names=true`) `icon`/`quality_color`/`quality` (the tier
name, e.g. `"EPIC"` — added 2026-07-25 to back the dashboard's rarity
filter)/`item_class`/`item_subclass`/`is_profession_item`/`sus_item_suspect`
(added 2026-07-31 as `legacy_jewelry_suspect`, renamed/broadened to
`legacy_gear_suspect`, renamed again to `sus_item_suspect` same day per
human naming preference — `snipe_check.is_sus_item()` passthrough, same
zero-extra-cost `names=true`-gated pattern as `is_profession_item`; never
filters server-side, `dashboard.html`'s "Hide flagged (sniper filter)"
checkbox is the only thing that can hide it)/a smart `variant` summary
(`ilvl NNN` only when `_variant_label()`'s ilvl-plausibility check passes,
via `fetch_snapshot.ilvl_plausible()`/`parse_bonus_key()`, else a bonus-
count fallback — `variant_raw` always carries the raw string, still per-row
display only, unaffected by the matching change). Runs the query via `await
asyncio.to_thread(_run_query)` **and** separately runs the `names=true` row-
building step via its own `await asyncio.to_thread(_build_rows)` (see "Real
production outage" below — the per-row `NameCache` lookups can make blocking
Blizzard calls; `find_snipes()` itself no longer does mid-query since
`_populate_market_keys()` was removed 2026-07-26, except via
`_filter_by_appearance()`'s `NameCache().inventory_type()` calls when
`max_appearance_sources` is set). `_build_rows()` prewarms the batch's
distinct item ids via `NameCache.ensure_many()`/`.ensure_icons_many()`
(concurrent) before the per-row translation, so a cold realm resolves in
parallel instead of one blocking call at a time.

**Auth/tiers**: `current_active_user` gated every `/api/*` route (not
`current_subscribed_user` — free tier, see below) **except `/api/snipes`**
(changed 2026-08-01 — see `auth.py`'s `resolve_user_from_request()` row for
the full reasoning: that route's 30-175s of unrelated slow work made holding
a `Depends(current_active_user)`-chain connection open for its whole
duration a real production incident).

**`/api/me`, `/api/status`, and `/api/realms` all dropped their auth
requirement entirely 2026-08-03** (alongside letting anonymous visitors use
`/snipes` — see below): none of the three ever returned genuinely per-user
data (`/api/status`/`/api/realms` are pure file-mtime/listing lookups;
`/api/me` now branches to an anonymous shape instead of 401ing), so the old
gate was only ever a "must be logged in" checkpoint, not a real data-
sensitivity or cost boundary. `SNIPE_TIER_CAPS`/`_snipe_cap(user)`: 500
(logged in, no subscription — raised from 250 2026-08-03, see below) / 2000
(active subscription) / 10000 (superuser, raised from 5000 2026-07-26).

**Anonymous (no account at all) tier, `ANON_SNIPE_CAP = 250`** (added
2026-08-03, human decision, bundled with the free-tier bump above):
deliberately today's *old* free-tier number, reused as the "create a free
account" incentive below the new 500 — `dashboard.html`'s anon nudge banner
shows both numbers side by side, read from `/api/me`'s `anon_cap`/`free_cap`
fields rather than hardcoded copy.

**`_class_quotas(user)`** (added 2026-07-27, human-specified numbers,
mirrors `_snipe_cap()`'s same tier detection): returns the tier's
`FREE_CLASS_QUOTAS`/`SUBSCRIBED_CLASS_QUOTAS`/`SUPERUSER_CLASS_QUOTAS` dict,
passed to `find_snipes()`'s `class_quotas` param on every `/api/snipes`
call. Free tier: `weapon` 100 / `armor` 200 / `housing` 80 / `mount` 10 /
`battlepet` 10 / `recipe` 100 (doubled 2026-08-03 alongside the cap bump —
sums to exactly its new 500 cap, same ratios as before so
`SUBSCRIBED_CLASS_QUOTAS`/`SUPERUSER_CLASS_QUOTAS`, derived from those
ratios, didn't need to change at all; deliberately zero
`quest`/`profession`/`container` — a human product decision, not an
oversight). `ANON_CLASS_QUOTAS` (added 2026-08-03) is the *old* pre-doubling
`FREE_CLASS_QUOTAS` values (`weapon` 50/`armor` 100/`housing` 40/`mount`
5/`battlepet` 5/`recipe` 50), now its own constant summing to
`ANON_SNIPE_CAP` (250) — used directly by `api_snipes()`'s anonymous branch,
never via `_class_quotas()` (which still only ever takes a real `db.User`;
an anonymous request never calls it).

**`recipe` added 2026-07-28** (human decision, after realizing recipes —
item_class 9, confirmed live, distinct from Profession's 19 — had no bucket
at all): free tier's `weapon` quota was cut from 100 to 50 (pre-doubling
numbers) to fund an equal 50 `recipe`, rather than raising the 250 cap
itself. Subscribed (2000) and superuser (10000) both keep fixed floors
`quest` 100 / `profession` 100 / `container` 20 (not scaled — free tier has
zero of these to scale a ratio from) and scale the remaining budget using
free tier's own weapon/armor/housing/mount/battlepet/recipe ratios
(20%/40%/16%/2%/2%/20%, weapon and recipe now equal instead of weapon alone
at 40%): subscribed → 355/712/285/36/36/356, superuser →
1955/3912/1565/196/196/1956 (weapon/armor/housing/mount/battlepet/recipe
order). Every tier's (and the anonymous tier's) dict sums to exactly that
tier's own cap
(`tests/test_dashboard.py::test_class_quotas_by_tier_sum_to_the_tier_cap`
and `::test_anon_class_quotas_sum_to_anon_cap` assert this).

**`_run_query()` always passes
`min_value_floor_g=snipe_check.MIN_VALUE_FLOOR_G`** (added 2026-08-01, human
request — see `snipe_check.py`'s row for the OR-to-keep/AND-to-drop
semantics) — not a query param, not user-configurable; every `/api/snipes`
call gets the same junk/decoy floor so a tier's top-N/class-quota budget
isn't spent on rows nobody would snipe. `tests/test_dashboard.py`'s
`disable_value_floor` autouse fixture neutralizes it for every other test
(same precedent as `default_item_class_stub`);
`test_api_snipes_applies_the_junk_value_floor` re-enables it to prove the
route wiring itself. `_enforce_realm_lock(user, sell, session)` (its
`session` now comes from a short-lived `async with db.sessionmaker()() as
session:` block `api_snipes()` opens just for this call, not a route-level
`Depends(get_async_session)` held for the rest of the request — see
`auth.py`'s row above): a free-tier (non-subscribed, non-superuser) account
is locked to the *first* sell realm it ever queries
(`db.User.locked_sell_realm`, written once); querying a different realm
afterward is a 403. Enforced *before* `check_data_ready`/`sort` validation,
deliberately (see `test_auth.py::test_free_tier_locks_to_first_sell_realm`)
— a free account commits its one realm choice by asking about that realm at
all, whether or not the query turns out ready.

**Atomic `UPDATE ... WHERE locked_sell_realm IS NULL`, not read-then-write**
(2026-07-31, real bug fix found during a repo-wide audit): the old ORM
pattern (`if user.locked_sell_realm is None: user.locked_sell_realm = sell;
commit`) had a genuine TOCTOU race — `current_active_user` resolves a fresh
`User` from a fresh session per request, so two concurrent first-ever
requests for two *different* realms could both observe `None` before either
committed, both pass, both proceed to run a real query, defeating the lock
entirely. The UPDATE's `WHERE` is evaluated atomically at the DB level; a
loser (`rowcount == 0`) looks up the real value via a plain `SELECT` (not
`session.refresh(user)`, which requires `user` already attached to *this*
session — not guaranteed for every caller) and updates `user` in memory to
match. Uses `execution_options(synchronize_session=False)` — SQLAlchemy's
default "evaluate" sync strategy re-checks the `WHERE` clause against in-
memory attribute values already in the session's identity map rather than
the real DB rowcount, which could silently "win" a stale session's stale
read even when the actual UPDATE matched zero rows; found live while writing
the regression test for the race fix itself. Covered by
`tests/test_dashboard.py`'s `test_enforce_realm_lock_*` tests, including one
that directly reproduces the race with two independent sessions against the
same row.

**The atomic mechanism itself was extracted into
`_atomic_lock_first_realm(session, model, pk_column, pk_value, lock_column,
sell)`** (2026-08-03, alongside adding anonymous access):
`_enforce_realm_lock` is now a thin wrapper around it, and a new sibling
`_enforce_anon_realm_lock(anon_token, sell, session)` calls the same shared
core against `db.AnonSession` instead of `User` — no subscription bypass
(there's no such thing as a subscribed anonymous session; subscribing means
registering, which moves onto the `User` path entirely), and its 403 message
deliberately never mentions cookies (human decision — don't hint that
clearing cookies resets the lock). Sharing one core means a bug in the
atomicity primitive breaks both tiers' tests simultaneously instead of only
one, rather than risking the two policies drifting apart via a hand-copied
second `UPDATE`. Covered by `tests/test_dashboard.py`'s `test_enforce_anon_r
ealm_lock_*`/`test_atomic_lock_first_realm_concurrent_requests_only_one_wins
` and `tests/test_auth.py`'s
`test_anonymous_visitor_locks_to_first_sell_realm` (a real cookie-flow test
mirroring `test_free_tier_locks_to_first_sell_realm` line for line). `GET
/api/realms` (backed by `_list_snapshotted_realms()` — any realm with at
least one snapshot, since 2026-07-25; no longer requires an events file, see
`collect_all.py`'s row; no auth requirement since 2026-08-03, see
"Auth/tiers" above).

**`GET /api/realms/eu`** (added 2026-08-02): every EU connected realm, not
just snapshotted ones — backs `wow_accounts.py`'s realm-registration picker
on `profile.html`. Gated by `current_subscribed_user` (can trigger up to ~92
individual Blizzard calls on a cold `_connected_realm_members_cache`, so
it's restricted the same as the paid feature it supports). Deliberately a
plain `def`, not `async def` — Starlette runs synchronous routes in a worker
thread automatically, so it's already off the event loop without needing
`asyncio.to_thread()` (contrast `api_snipes()`, which is `async def` and
therefore does need it, per "Real production outage" above).

**Fanned out to one entry per member realm name, not one per connected-realm
id, the same day** (human request: "this works with connectedrealms, so if
choose realm that has connected it auto adds them as well") — a connected
realm can bundle several named realms sharing one AH (confirmed live:
Zul'jin and Sanguino are the same connected realm, with Sanguino as
Blizzard's "primary" name), and a user should be able to find their
character's realm by any of its names, not just the one `_realm_info()`
happens to show elsewhere. New
`_connected_realm_members_cache`/`_connected_realm_members(cr_id)` (separate
from `_realm_info_cache` — every other `_realm_info()` caller genuinely
wants just the one display name) backs this; multiple returned entries can
share the same `id`, and adding any one of them registers the whole
connected realm (matching is by `id`, same as everywhere else in this app) —
`profile.html`'s `realmNameById` dedupes back down to one (first/primary)
name per id purely for chip display. Covered by `tests/test_dashboard.py::te
st_api_realms_eu_fans_out_connected_realm_members`. `GET /api/status`
(returns `has_data`, renamed from `events_exist` the same day — true once a
snapshot has ever been retrieved for the realm; no auth requirement since
2026-08-03)/`GET /api/me` (also exposes `locked_sell_realm` and, since
2026-07-29, `nickname` — see the next paragraph).

**`GET /api/me` no longer requires login at all (2026-08-03)**: it resolves
the real user manually via `auth.resolve_user_from_request()` (the same
helper `api_snipes()` uses) instead of `Depends(current_active_user)`, and
falls back to `auth.resolve_or_create_anon_session()` when there's no real
user, returning an anonymous-shaped response
(`email`/`subscription_status`/etc. all `None`, plus `is_anonymous: true`
and the `anon_cap`/`free_cap` numbers) instead of 401ing.
`static/dashboard.html`'s `init()` no longer redirects to `/login` on a
failed `/api/me` call as a result — see that file's row below.

**`PATCH /api/me/nickname`** (added 2026-07-29, human request — Snipe Board
posts used to show the account's real email publicly): body `{"nickname":
str}`, strips whitespace, rejects empty or over `NICKNAME_MAX_LEN` (50) with
400, writes `db.User.nickname`. Gated by `current_active_user` like every
other `/api/*` route; `forum.create_post()` independently requires
`user.nickname` be set before it'll accept a post (see `forum.py`'s row
below) — this endpoint is what `static/snipeboard.html`'s post dialog and
`static/profile.html` both call to set/change it. `GET /pricing` is a
deliberately unauthenticated route (pricing isn't the paid product).

**The public retrieval-time log page (`/log`, `static/log.html`, `GET
/api/log`/`GET /api/log/realms`) was removed 2026-08-01** (human decision,
"not necessary anymore") — `_list_snapshotted_realms()` is still real, still
used by `/api/realms`, just no longer shared with a second public consumer.

**Routing changed 2026-07-26**: `GET /` now serves `static/landing.html` (a
public marketing page, human request); the sniper tool itself moved to the
new `GET /snipes`, which serves `static/dashboard.html` (unauthenticated
route, same as before — auth is enforced client-side by the page's own
`init()`). Every other page's nav "Dashboard" link and `login.html`'s post-
login redirect were updated from `/` to `/snipes` to match; `pricing.html`'s
"Dashboard" nav link was removed outright (human request) rather than
repointed. `python dashboard.py --sell 1403` runs it locally on
`127.0.0.1:8000` — leave `ENABLE_BACKGROUND_COLLECTION` unset locally.

**`no_cache_html` middleware** (added 2026-07-31, human report + live repro:
a user kept seeing a bug fixed server-side because their browser silently
reused a cached `dashboard.html` from before the fix deployed, even on a
plain reload — `FileResponse`/`StaticFiles` set `Last-Modified`/`ETag` but
no `Cache-Control`, leaving browsers free to skip revalidation entirely via
heuristic caching): sets `Cache-Control: no-cache` on every `text/html`
response (both the page routes above and the raw `/static/*.html` mount),
forcing a conditional revalidation request on every load — a cheap 304 when
nothing changed server-side, never a silently-stale page. Deliberately `no-
cache`, not `no-store` — doesn't turn into a full re-download on every
visit, just guarantees the browser can't skip asking. Doesn't touch JSON API
responses (different content-type).

**Root-logger `logging.basicConfig()` now passes `stream=sys.stdout`
explicitly** (2026-08-01, real bug fix: this is the handler actually active
for `collect_all.py`/`fetch_snapshot.py`/`scan_region.py`'s `log.info()`
calls when those run as library functions from inside this process's
background loop, since their own `setup_logging()` — which independently got
the same `sys.stdout` fix, see those files' rows — is only invoked when they
run standalone via their own CLI `__main__` block; a first fix to only those
files' `setup_logging()` was confirmed live to *not* actually stop the
misclassification, since production runs the library-call path, not the
standalone one). Same root cause as those files:
`basicConfig()`/`StreamHandler()` with no stream argument default to
`sys.stderr`, which Railway's log platform tags `"severity":"error"`
regardless of the real Python log level.

**`track_activity` middleware + `GET /api/admin/active-users`** (added
2026-08-01, human request — "who's on the site right now"):
`_recent_activity`, a module-level in-process `dict[ip, last_seen_unix_ts]`,
updated by `track_activity()` for every `/api/*` request (static/page loads
don't count as "activity"). `_client_ip(request)` reads `X-Forwarded-For`'s
first entry, not `request.client.host` — confirmed live that the latter
shows Railway's internal edge-proxy hop (an `fd12:...` address matching
`railway logs --http`'s own `upstreamAddress` field), not the real visitor,
while `X-Forwarded-For` matches that same command's `srcIp`. `GET
/api/admin/active-users` (superuser-only, 403 otherwise —
`current_active_user` alone isn't enough, mirrors
`has_active_subscription`'s stricter-check-on-top-of-login convention
elsewhere in this file) returns IPs seen within `ACTIVE_WINDOW_SECONDS` (15
min). Deliberately IP-based, not resolved to a real account identity — cheap
(no per-request DB lookup, unlike `resolve_user_from_request()`) and
sufficient for the actual ask. Purely an in-memory, resets-on-redeploy
observability convenience for a low-traffic period (same not-critical-
infrastructure precedent as `_realm_info_cache` above), explicitly not a
real analytics feature — Railway's own `railway logs --http --filter
"@srcIp:..."` already gives the same underlying data ad hoc, this just makes
"how many distinct visitors right now" a one-click authenticated check
instead. Opportunistic pruning (`_ACTIVITY_PRUNE_THRESHOLD=1000`) keeps the
dict from growing unbounded over a long-running process without a separate
background task.

**`ensure_anon_cookie` middleware** (added 2026-08-03, alongside anonymous
`/snipes` access): sets the `ah_anon` cookie on the *genuinely final*
response, not a route-local injected `Response` parameter — real bug found
while building this: FastAPI's exception handling builds an entirely
separate `Response` when a route raises `HTTPException`, which never sees
mutations made to a route's own `Response` parameter, and `/api/snipes`
routinely raises `HTTPException` for anonymous requests (400 for an
uncollected realm, 403 for a different-realm lock) — the *common* case for a
first-time anonymous visitor, not an edge case, so a fix scoped only to the
200-success path would have left the cookie unset almost every time it
actually mattered. `auth.resolve_or_create_anon_session()` stashes the
resolved token on `request.state.anon_token`; this middleware reads it after
`call_next()` and sets the cookie on that response (unaffected by exception
handling, since middleware wraps the whole request/response cycle), only
when it's actually new/changed so an already-valid session doesn't get a
redundant `Set-Cookie` every call.

### `static/dashboard.html`

Single static file, vanilla JS, no build step. Light "assay ledger" visual
identity with a dark-mode toggle (`localStorage`-backed, shared pre-paint
`<head>` script across all six pages) — see `history.md`'s "UI design pass"
for how this was chosen. Quality-colored item-icon rings, gold/silver/copper
coin-icon formatting, a hover tooltip, click-icon-for-Undermine-Exchange-
link.

**Client-side filtering architecture** (2026-07-24): fetches one loose batch
per sell realm (`fetchBatch()`, `BATCH_TOP=10000` — the ceiling across every
tier, must stay >= the highest tier cap, the server clamps down to the real
cap via `_snipe_cap()`) and re-filters/re-sorts entirely in the browser
(`applyFilters()`/`renderTable()`/`compareRows()`) — only a sell-realm
switch, an item-id change, or the 60s auto-refresh timer re-fetches (the
manual Refresh button was removed 2026-07-25 as redundant —
`checkForUpdates()` already covers "is there new data" on its own).
`checkForUpdates()` (auto-refresh + initial load **and**, since 2026-07-26,
a realm/item-id switch too via `onCandidatePoolChange()`) checks the cheap
`/api/status` timestamp first and only re-fetches `/api/snipes` when it's
actually advanced — a realm switch used to always re-run the full query even
when the just-selected realm's cached batch was already fresh, since it
called `fetchBatch()` directly instead of going through this check (verified
live in a real browser against a stubbed backend: switching back to an
already-cached, unchanged realm now does zero new `/api/snipes` calls, only
the cheap status check). The last-fetched batch is cached in `localStorage`
(keyed per sell realm + item filter, cleared on logout,
`CACHE_VERSION`-namespaced so a row-shape change can't get painted against
code expecting different fields — bumped to 2 on 2026-07-27's
`sell_price_suspect`/`region_median_*` addition, bumped to 3 on 2026-07-31's
`legacy_jewelry_suspect` addition, bumped to 4 the same day when
`sell_price_suspect` was removed and `legacy_jewelry_suspect` was
renamed/broadened to `legacy_gear_suspect`, bumped to 5 the same day again
when that was renamed to `sus_item_suspect` per human naming preference,
bumped to 6 the same day again when `buy_realm_category` was added, bumped
to 7 on 2026-08-01's `region_sale_rate`/`region_sold_per_day` addition) and
painted instantly on load.

**`saveCache()` hardened twice more the same day, both traced live against a
real production account** (Draenor, superuser tier): (1) it now purges every
*other*-version `snipe_cache_v*` key before writing, not just on logout — a
`CACHE_VERSION` bump orphans the old entry forever (nothing else ever reads
it), but it doesn't disappear from `localStorage` on its own, and a full
`BATCH_TOP`-row batch can sit close enough to the browser's per-origin quota
that the orphaned entry alone blocked the new version's write with
`QuotaExceededError` — silently, since the write was already wrapped in a
fail-soft `catch`, so caching was permanently broken with no visible error,
every load painting from nothing and reading as a cold "Loading latest
data…" forever. (2) Even with that purged, the *current* payload alone
measured 11.12MB for 10,000 rows on Draenor (its huge near-100%-discount
decoy-listing pool, see "Known gaps/risks" in `progress.md` — likely over
quota on its own for any account hitting a large chunk of that pool), so
`saveCache()` now retries with progressively fewer rows (halving up to 6
times) instead of giving up outright — a partial cache still serves the
"instant paint" purpose, and the next real fetch always replaces it with the
true current batch regardless of how many rows made it in.

**Given LRU eviction of *other* realms' caches, 2026-07-30** (human report +
live repro: Scarshield Legion never got a cache entry across two separate
real visits, in a browser that already had three other realms cached at ~5MB
combined -- confirmed live via the browser's own `localStorage` inventory
and a live `QuotaExceededError` on even a small test write). Root cause the
halving retry above never covered: shrinking *this* realm's own row count
only helps if this realm's own payload is what's filling the quota -- it
does nothing when the quota is already consumed by *other* realms' full-size
entries sitting untouched, which is exactly what happens to anyone who
explores more than a couple of realms in one browser session, since nothing
ever evicted an old realm's cache to make room for a new one.
`evictOldestOtherCache(sell)` now removes the least-recently-fetched *other*
realm's entry (by `fetchedAt`, true LRU) and `saveCache()` retries at the
*same* row count before ever shrinking its own payload, falling back to the
pre-existing halving loop only once there's nothing left to evict. Verified
live by injecting the fix into a real browser already in the broken state:
it evicted all three other realms in oldest-first order and successfully
wrote the new realm's cache.

**`fetchBatch()` no longer re-fetches `/api/status` a second time just to
learn `last_modified` for `saveCache()`** (fixed 2026-07-30, human report +
live repro: Scarshield Legion's data loaded and displayed correctly on two
separate visits, yet had no `localStorage` entry at all afterward —
confirmed live via the browser's own `localStorage`, no cache key existed
even though the row data was clearly on screen). Root cause: `saveCache()`
only ran after a *second*, independent `/api/status` call made inside
`fetchBatch()` itself, purely to learn the timestamp to tag the cache with —
but `checkForUpdates()` (`fetchBatch()`'s only caller) already has that
exact value from its own status check moments earlier, before ever deciding
a real fetch was needed. If that redundant second call failed for any
transient reason, the main `/api/snipes` fetch still succeeded and rendered
normally, but `saveCache()` was silently never called, so that realm would
redo the full expensive query on every future visit, forever, with no
visible symptom. Fixed by having `checkForUpdates()` pass its already-known
`status.last_modified` into `fetchBatch(knownLastModified)`, which uses it
directly instead of calling `loadStatus()` again — removing the redundant
round-trip (and its independent failure mode) entirely for the common path;
the one remaining fallback to a fresh `loadStatus()` call is the pre-
existing cold-start path where `checkForUpdates()`'s own status check
already failed and no known value was available to begin with.

**`fetchBatch()`'s loading treatment is quiet on a background refresh**
(fixed 2026-07-27, human report: the dim/blocked `.table-wrap.loading` state
— meant for a cold load with nothing to show yet — was firing on every
silent 60s-timer refresh too, roughly once an hour when Blizzard's AH data
actually republishes, often with no visibly different result; felt like an
unwarranted interruption). `isBackgroundRefresh = cachedRows.length > 0`
gates it: rows already on screen means `setLoading(true)`/the blocking dim
is skipped entirely (and a failed background refresh no longer blanks the
table either — it just leaves the last-known-good rows and lets the next
tick retry); only a genuine cold load (nothing cached yet) still shows the
blocking "Loading latest data…" state. Verified live via a direct
`fetchBatch()`/`checkForUpdates()` console exercise against both cases.

**`isBackgroundRefresh` fixed to also require `cachedRowsSell === sell`**
(2026-07-30, human report: switching sell realm showed the *previous*
realm's rows with no loading indicator while the new realm's query ran).
Root cause: `cachedRows.length > 0` alone can't tell "quiet same-realm
refresh" apart from "stale rows left over from whatever realm was selected
before this one" — `onSellRealmChange()` only overwrote `cachedRows` when
`renderFromCache()` found an existing cache for the newly-picked realm, so
switching to a realm with no cache yet left the prior realm's rows (and
`cachedRowsSell`) in place; `isBackgroundRefresh` then read `true` and
skipped `setLoading(true)` entirely. Fixed with a new `cachedRowsSell`
variable (set everywhere `cachedRows` is assigned) plus an explicit clear in
`onSellRealmChange()` when `renderFromCache()` returns false. Verified
against the real functions (copied verbatim into an isolated harness with
mocked `fetch`/`localStorage`, not just read) — confirmed the old formula
reproduces the bug and the new one doesn't. **"Min sale rate %"** filter
(added 2026-08-01, human request — see `snipe_check.py`'s `min_sale_rate`
row and `tsm.py`'s row): a 0-100 percentage input, same UI convention as
`min_discount_pct` (not a raw 0-1 fraction), converted to a fraction and
compared against `row.region_sale_rate` in `applyFilters()`. Parsed via the
`.trim() ? Number(...) / 100 : null` pattern (not `Number(x) || null`)
deliberately — `"0"` is a real, meaningful filter value here
(region_sale_rate must be >= 0, i.e. tracked by TSM at all, excluding
untracked items) rather than the same as an empty field, the same "0 is
falsy but valid" class of bug `applyFilters()`'s "Max per item" parsing was
fixed for 2026-07-31. A row with `region_sale_rate == null` (TSM has no data
for the item) fails the filter once one is set, matching
`snipe_check._filter_by_sale_rate()`'s server-side (CLI/API) semantics
exactly. Registered in `LIVE_FILTER_IDS` so it re-filters live as typed,
same as every other threshold. `showTooltip()` shows a "Sale rate" row
(percentage + `N.N/day`) right after "EU median" when TSM has data for the
item, omitted entirely when it doesn't.

**A dedicated "Sale rate %" table column was added the same day** (human
follow-up: "There needs to be a sellrate column... in 0,00003 every deci is
important") — initially shipped as tooltip/filter-only, matching
`min_sell_now`'s "a filter, no dedicated column" precedent, but a whole-
number-percent rounding (`toFixed(0)`) would have rounded every real value
for a niche item (routinely well under 1%, e.g. `0.00003%`) down to `0.0` —
indistinguishable from "no TSM data at all." `formatSaleRate()` (shared by
both the column and the tooltip, so they can never disagree) fixes the
display at **5 decimal places**, human-specified precision, not just "more
precision than before" picked by the assistant; `null` shows as `-`,
matching `variant`'s "nothing to show" convention rather than a misleading
`0.00000`. Sortable (`sale_rate` key in `SORT_VALUE`/`NUMERIC_SORT_KEYS`),
null values sort last regardless of direction like every other nullable
numeric column (`compareRows()`'s existing null-handling). Column widths
were redistributed across all eight columns (mostly out of Item and the
three money columns) to make room — see the CSS comment at the column-width
block. Filter rail: discount%/gold range/sell-now/min-sale-rate/unique-
transmog/"hide flagged"/"buy below sale avg"/9-way item-class (added
`recipe` 2026-07-28, item_class 9)/6-way rarity checkboxes (each group OR'd
together, groups AND'd), all client-side. **"Buy below sale avg"** checkbox
(added 2026-08-04, human request): only shows rows where `buy_copper <
region_sale_avg_copper` (TSM's EU-region-wide average sale price, the "Sale
avg" column) -- a row with no TSM average-sale-price data is excluded once
this is checked, same "unknown doesn't count as passing" convention
`min_sale_rate_pct` already uses for `region_sale_rate`. Purely client-side
against fields already in every cached row
(`buy_copper`/`region_sale_avg_copper`) -- no new API field, no
`CACHE_VERSION` bump needed. **"Min sale avg (g)"** input (added 2026-08-04,
same day, human request): a floor on `region_sale_avg_copper` itself, same
`.trim() ? Number(...) * 10000 : null` parsing pattern as
`min_gold`/`max_gold` (not `min_sale_rate_pct`'s /100, since this one's a
raw gold amount) -- a row with no TSM average-sale-price data is excluded
once set, same convention as "Buy below sale avg." Registered in
`LIVE_FILTER_IDS` so it re-filters live as typed. **"Max per item" removed
2026-08-02** (human request, "not necessary anymore") — the `#max_per_item`
input, its `buildGroups()` cap logic, and its `LIVE_FILTER_IDS` entry are
gone; "Min sell price (g)" (previously paired with it in a `.filter-row`) is
now a standalone full-width `<label>`. Client-side only — the server-side
`max_per_item` CLI/API param on `snipe_check.find_snipes()`/`/api/snipes`
was never sent by this page in the first place (see the `fetchBatch()` param
comment above) and is untouched. — the rarity filter (added 2026-07-25)
reads the new `quality` field (`QUALITY_FILTERS`) and shows a color swatch
matching each tier's icon-ring color. **"Hide flagged (sniper filter)"**
checkbox (added 2026-07-31 as "legacy jewelry," renamed/broadened to "legacy
gear" same day when `sell_price_suspect` and its own "Hide flagged (suspect
price)" checkbox were removed outright, renamed again the same day to "sus
items" per human naming preference — unchecked by default, nothing hidden
unless asked) reads `sus_item_suspect` (see `snipe_check.py`'s row above) —
flagged rows show a `⚠` (`.sus-flag`, red/`var(--alert)` — changed
2026-08-02 from a deliberately-muted brown hourglass per direct human
request, "redo the flagged icon to something red danger") in the item-cell
next to the name, visible regardless of the checkbox state so the signal is
seen even when not filtering on it.

**The same checkbox now also ORs in `price_suspect`** (added 2026-08-03,
human request — see `snipe_check.py`'s `PRICE_SUSPECT_MULTIPLE` row): no new
checkbox, since this is the same "flag, let the user opt in to hiding it"
convention as `sus_item_suspect`, just a different signal (the sell realm's
own reference price, not the item's identity) — its own `.price-flag` marker
(same red `var(--alert)` palette, same `⚠` glyph) originally sat in the
sell-price cell, next to the number it's actually about.

**Moved into the item-cell alongside `.sus-flag` the same day** (human
report, comparing two real rows: sitting apart in a different column read as
inconsistent/misplaced next to the other flag) — both warnings now live in
one spot next to the item name, distinguished by their own tooltip text on
hover rather than by which column they're in. `CACHE_VERSION` bumped 7 → 8
for `price_suspect`'s own addition, then 8 → 9 the same day for
`region_sale_avg_copper` below (same reasoning as every prior bump — an old
cache would be missing the field/show a flag in the wrong place until it
happened to expire), then 9 → 10 on 2026-08-04 for `sniper_filter_suspect`
("Sniper filter," see `snipe_check.py`'s `SNIPER_FILTER_N` row) — a third
signal riding the same checkbox and item-cell flag pattern as
`price_suspect`/`sus_item_suspect`: own `.sniper-flag` marker (same red
`var(--alert)` palette, own tooltip text — "several other realms are priced
close to this listing"), own hover-tooltip "Note" row, human-named directly
("Sniper filter") rather than assistant-picked.

**A new "Sale avg" column** (added 2026-08-03, human request — "we should
display region sale avg from tsm, if it exist on the items") shows TSM's EU-
region-wide `region_sale_avg_copper` (see `snipe_check.py`'s row and
`tsm.py`'s row) via `formatMoney()`, placed right after "Sale rate %" (both
are TSM stats) — sortable (`sale_avg` key in
`SORT_VALUE`/`NUMERIC_SORT_KEYS`, null sorts last same as every other
nullable numeric column) and shown in the hover tooltip as a "Sale avg" row
when TSM has data for the item. `formatMoneyOrDash()` wraps `formatMoney()`
for this column specifically — unlike every other money column (buy/sell/EU
median), this one can genuinely be `null`, and `formatMoney()` itself has no
null guard (`Math.round(null)` is `0`, which would silently render "0c").
Column widths redistributed across all ten columns (9% out of Item, 1% off
every other column except Buy realm/Variant) following the same pattern as
prior column additions. An **"EU median"** column (added 2026-07-27, human
request) shows `region_median_copper` next to Sell price, sortable
(`eu_median` sort key) same as every other numeric column, plus a matching
row in the hover tooltip — purely informational, doesn't filter anything.
The **buy-realm cell shows a language tag** (added 2026-07-31, human
request) — `realmLangTag()` abbreviates `buy_realm_category`
(English/German/French/Italian/Russian/Spanish) to a 2-letter `.realm-lang`
tag next to the realm name, plus the full word in the cell's title tooltip
and the hover tooltip's "Buy realm" row; display-only, doesn't filter or
sort. Deliberately **not** shown in the sell-realm picker
(`populateRealmPicker()`) — human call, that list is about picking *your
own* realm, not about a buy-side listing's origin, so `_realms_payload()`
doesn't carry `category` at all.

**`table-layout: fixed` + per-column % widths** (added 2026-07-31, human
report + live repro on the human's own production account): the table used
the default *auto* layout, which sizes every column from the widest content
across all rows currently in the table — so toggling a filter (which changes
the row set, e.g. varying realm-name+language-tag lengths) visibly shifted
every column boundary on every re-render, confirmed live via
`getBoundingClientRect()` before/after ("Unique transmog only" alone moved
the Item column's left edge by 28.6px). Fixed layout sizes columns from the
CSS % widths alone, independent of row content; the two free-text columns
(`td:nth-child(1)`/buy realm, `td:nth-child(3)`/variant) and the item name
(`.item-name`, needs `min-width: 0` since a flex child's default min-width
is its content size and would otherwise defeat the column's truncation) get
`overflow: hidden; text-overflow: ellipsis` since fixed layout no longer
lets a cell grow to fit long content — the existing hover tooltip already
surfaces the untruncated value, so nothing is actually lost.

**`RENDER_PAGE_SIZE`/`renderLimit` pagination** (added 2026-07-31, same
investigation): profiling `renderTable()` live on the human's own account
found `table-layout: fixed` alone was a real but partial fix (~900ms →
~680ms) — the dominant cost, ~90% of total time, was `$("rows").innerHTML =
html.join("")` itself rebuilding thousands of DOM rows from scratch on every
filter change (measured: ~940ms of ~1060ms total for 1,647 rendered groups),
not the layout/filtering/sorting logic (all three combined stayed under
15ms). `renderTable(resetLimit=true)` now only builds DOM for the first
`renderLimit` (default `RENDER_PAGE_SIZE=200`) groups post-sort — since
groups are already sorted best-first, this never hides a better match behind
a worse one, it just defers building rows past what's shown — with a **"Show
N more"** button (`#show-more`) revealing another page at the same
`renderLimit`-preserving call (`renderTable(false)`). `resetLimit` defaults
to `true` (a fresh filter/sort/realm change snaps back to page 1) except the
group-expand-toggle handler and the "Show more" button itself, both of which
pass `false` to keep the current page instead of discarding it. Verified
live: paginated re-render of a 200-row page measured ~45-55ms warm, versus
~900-1000ms for the full ~1,650-group set before this change.

**Three more real bugs fixed 2026-07-31, found during a repo-wide audit**:
(1) `escapeHtml()` (a real regex-based escaper, mirroring
`snipeboard.html`'s fix — see that row below) is now applied to every row-
derived string
(`row.name`/`buy_realm_name`/`buy_realm_category`/`variant`/`variant_raw`)
before interpolation into `buildRowHtml()`/`showTooltip()`'s `innerHTML`
template strings, including inside double-quoted attributes like
`title="..."` — item/realm names come from Blizzard's API, generally curated
but not contractually guaranteed free of quotes/angle brackets, and this
project already shipped the exact same unescaped-attribute gap once. (2)
`applyFilters()`'s "Max per item" parsing now clamps to `null` (no cap) for
anything that isn't a real positive integer, not just a blank field —
`Number("0")` is falsy, so typing `0` used to silently mean "no cap" (same
as leaving it blank, confusing); `Number("-5")` is truthy and
`Array.slice(0, -5)` means "drop the last 5," the *opposite* of a per-item
cap, so a negative input silently inverted the filter instead of erroring.
(3) `fetchBatch()` now discards a response whose realm no longer matches
`$("sell").value` (and kicks off `checkForUpdates()` for whatever's actually
selected) instead of applying it unconditionally — `fetchInFlight` prevents
two fetches running at once, so switching realms mid-fetch (a cold query can
take 30-175s) used to silently no-op the *new* realm's own fetch attempt,
leaving the *stale* response as the only thing that ever painted,
overwriting the UI with the previous realm's data under the newly-selected
realm's dropdown value. Rows group by `(item_id, pet_species_id,
pet_quality_id)` (`groupKey()`, changed 2026-07-26 from `market_key` — see
"What this project is" above's matching-model note; pet identity is still
needed since every caged pet shares one item_id), best-discount first, with
a `▾ N` expand toggle. Free-tier accounts get a `requirePick` realm dropdown
(no default pre-selected — an earlier version silently locked new accounts
to the server's site-wide default realm on first load, fixed 2026-07-25).

**`LAST_SELL_KEY` localStorage-remembers the last-picked realm**
(2026-07-30, human report: picking Howling Fjord, visiting Snipe Board, then
coming back reset the dropdown to Draenor) — `init()` previously only ever
used `/api/config`'s site-wide `default_sell` as the prefill for a non-free-
tier account, with nothing remembering a genuine per-browser choice across
page loads. Now a remembered pick beats the config default, but a real
server-enforced free-tier lock (`locked_sell_realm`) still beats both —
order verified with a standalone priority-logic test, not just read. Written
on every `onSellRealmChange()`; purged by `clearAllCaches()` on logout
alongside the snipe-cache keys, same reasoning (`localStorage` is origin-
scoped, not per-account). Topbar brand mark (seal + wordmark) is wrapped in
a link to `/` (2026-07-26, `.brand-link`, `display: contents` so it doesn't
disturb `.brand`'s flex layout) — same convention now shared by every other
static page (see the row below). The `seal-label` "Validated Data" text next
to it was removed the same day (human request); the seal icon itself is
unchanged. Nav is `Dashboard`/`Watchlist`/`Snipe Board`/`Profile`/`Log out`
(`Watchlist` added 2026-07-31, see `/watchlist`'s row below — a "coming
soon" placeholder, not a real feature yet; the public retrieval-time `Log`
page was removed 2026-08-01, human decision, "not necessary anymore" — net
link count unchanged) — the `Pricing` link was removed (2026-07-26, human
request) since this navbar is only ever seen by already-logged-in users
inside the tool; `static/landing.html`'s own navbar (the marketing
frontpage) keeps its `Pricing` link regardless of login state — a deliberate
distinction, not an oversight.

**`Dashboard` self-link added 2026-07-30** (human report: nav buttons
visibly shifted position depending on which page you were on) as part of a
site-wide nav-unification pass — every in-app page
(`dashboard.html`/`snipeboard.html`/`profile.html`) now shows the identical
link set in the identical order for a given auth state, with the current
page rendered via a new `.nav a.active` style (bold, `--bullion`-colored,
matching the table's `th.active` convention) instead of being omitted — an
omitted self-link was *why* the nav's width/contents differed page to page.
Same pass added a subtle shared topbar polish (`.nav a, .nav button` padding
0.4rem 0.7rem → 0.45rem 0.8rem, radius 3px → 4px, a `background:
var(--paper)` hover tint, a `transition`) applied identically to every page
including `landing.html`/`pricing.html` (whose own distinct nav *link sets*
were deliberately left alone, see those files' rows). **9th "Your account"
column added 2026-08-02** (human request — see `wow_accounts.py`'s row): a
new module-level `userRealmAccounts` Map (realm id → array of account
labels), populated once in `init()` via `loadUserRealmAccounts()` (a `GET
/api/wow-accounts` fetch, skipped entirely for free-tier accounts since
they'd only ever get a 402) before the first render. Deliberately **not**
baked into the cached per-realm row JSON or `CACHE_VERSION` — this is per-
user data computed client-side by cross-referencing `row.buy_realm` against
the Map; putting it in the shared `localStorage` snipe-batch cache would
leak one user's account labels into another user's cache-inspection surface
if that cache were ever shared/logged. Sortable (`your_account` key in
`SORT_VALUE`, deliberately **not** added to `NUMERIC_SORT_KEYS` — a string
column like `buy_realm`/`item`/`variant`, defaults ascending) —
`compareRows()`'s existing generic null-handling already sorts unmatched
rows last, no new code needed there. Column widths redistributed across all
nine columns (mostly out of Item, plus 1% off each money column) following
the same pattern as the Sale rate % column's own addition the day before.

**Fixed a real horizontal-scrollbar regression the same day** (human report:
"the diagram rows and columns should not have a scroll bar... everything
should be visible all the time"): `th` had an unconditional `white-space:
nowrap`, and with 9 columns the sum of every header label's forced-unbroken
width (particularly the two-word "Your account") pushed the table's actual
rendered width past `.table-wrap`'s container width — `table-layout: fixed`
only fixes each column's *proportional* width, it doesn't stop unbreakable
header text from forcing the overall table wider than 100%, which is exactly
what `.table-wrap`'s `overflow-x: auto` then shows a scrollbar for. Changed
`th` to `white-space: normal` so long labels wrap onto two lines instead —
removes the forcing case entirely, regardless of column count or viewport
width, rather than a narrower fix tied to one specific width. Verified live
in a real browser (not just reading the diff): the scrollbar is gone at both
the original window width and a resized-narrower one.

**Anonymous access (2026-08-03)**: `init()` no longer redirects to `/login`
when `/api/me` fails — it never fails for a normal visitor anymore (see
`dashboard.py`'s row), and falls back to a synthetic anonymous shape
(`is_anonymous: true`) if the fetch itself errors. `isFreeTier`'s existing
formula (`!meData.is_superuser && meData.subscription_status !== "active"`)
is naturally `true` for the anonymous response shape too, so the entire pre-
existing free-tier UI (locked-realm hint, disabled dropdown once locked,
blank-until-chosen `requirePick` behavior) already worked unchanged for an
anonymous visitor with zero extra logic. Nav is now conditional (`#nav-
login`/`#nav-profile`/`#nav-logout`, default markup is the logged-out state)
— matches `snipeboard.html`/`watchlist.html`'s existing swap convention,
since this page could never be reached logged-out before. A new **anon nudge
banner** (`#anon-banner`, its own `.anon-banner` CSS class rather than
reusing `.realm-lock-hint` — the latter's negative `margin-top` is tuned to
sit directly under the `<select>`, which would overlap the ticker above
where this banner sits instead) shows only for `meData.is_anonymous`,
reading `anon_cap`/`free_cap` from the `/api/me` response rather than
hardcoding the 250/500 numbers a second time: "Browsing anonymously"
followed by a bullet list ("Capped at N items" / "Create a free account to
raise your cap to N"), human-specified layout. Says nothing about cookies
anywhere (human decision — don't hint that clearing the `ah_anon` cookie
resets the realm lock).

## `admin.py` — activity tracking split out of `dashboard.py` (2026-08-04)

**The question was whether to make this a Railway service.** The human asked
whether the whole `/api/admin/active-users` endpoint could move to a second
service in the same Railway project, superuser-login only, "to sort aiming
for microservices". The answer was no, and not on style grounds: the
endpoint's only data source was `_recent_activity`, an in-process dict
written by `dashboard.py`'s own `track_activity` middleware. A second
container has its own RAM and serves none of the `/api/*` traffic, so the
moved endpoint would have returned `{"count": 0, "ips": []}` forever. Making
it work across a process boundary would have meant Redis or a Postgres table
written *on every request* — a real latency and pool cost, paid to relocate
what its own code comment called "an observability convenience". Secondary
costs, all real but none decisive on their own: `auth.py`'s
`CookieTransport(cookie_name="ah_auth")` cookie wouldn't cross to a
different Railway domain without a shared parent domain, a second DB pool
and a second copy of fastapi-users would exist only to answer "is this user
a superuser", and the whole web layer is under 1,800 lines — there is no
team boundary or scaling pressure that a service split solves. Recorded here
because "why isn't this a microservice" is the kind of question that comes
back.

**What the human actually wanted** turned out to be bigger than the existing
endpoint: an admin page showing current active users *and* a persistent
history of every IP that has accessed the site, with location. That changes
the storage answer (history means persistence) without changing the service
answer (the middleware still has to run where the traffic is). So: a module
split, `admin.py` + `static/admin.html`, in the same app.

**Two-layer design, forced by a prior incident.** `db.engine()`'s comment
records a real outage — QueuePool exhausted, login failing, from a single
account browsing lightly — and `dashboard.html` auto-refreshes, so a DB
write per `/api/*` request was never viable. The request path stays
memory-only (`track_activity` writes two dicts and returns);
`_visitor_flush_loop` batches into `db.VisitorIP` every 60s. One write a
minute, not one per request. Both endpoints then read the *table*, so the
numbers survive a redeploy where the old in-memory view reset to zero; the
accepted cost is that a brand-new IP can lag up to one flush interval before
it appears. Merging the live dict into the read would close that gap but
give two sources of truth that disagree about `hit_count` — not worth it for
an observability page.

**Schema shape was a human decision**: one row per distinct IP
(`first_seen`, `last_seen`, `hit_count`) rather than a row per visit or a
full request log. Bounded by distinct visitors, so it stays small
indefinitely, and it answers both questions from one table — "active now" is
`last_seen` within `ACTIVE_WINDOW_SECONDS`, history is the same table
ordered by it.

**Geolocation was offered and deferred** (human picked "skip geo for now"
from ipinfo.io / MaxMind GeoLite2 local DB / ip-api.com / skip). Worth
keeping the licensing note: ip-api.com's free tier is the easiest to ship
but is explicitly non-commercial and HTTP-only, which doesn't fit a product
with a paid tier. No `country`/`city`/`org` columns were added speculatively
— empty columns would just be dead UI.

**Two bugs found while building, both real rather than test artifacts:**

- `DateTime(timezone=True)` is honoured by Postgres but *not* SQLite, which
  has no native timestamp type and returns a naive `datetime`. So the same
  column arrives aware in production and naive under test, and any
  arithmetic mixing them raises `TypeError`. Fixed with `_as_utc()` on every
  read — everything written is UTC, so attaching UTC to a naive value
  recovers the real instant rather than guessing.
- `_client_ip()` returns an attacker-controlled header value
  (`X-Forwarded-For`) which is now a `String(45)` primary key, where an
  over-length value raises instead of being silently accepted the way the
  old dict did. Truncated to `MAX_IP_LEN`. For the same reason
  `admin.html` builds every row with `textContent`, never `innerHTML` — a
  stored-XSS hole on the one page a superuser is guaranteed to open would be
  a bad trade for a few characters of markup.

**Verified in a real browser** (per the definition of done), which needed a
workaround worth recording: the `ah_auth` cookie is `HttpOnly`, so a
superuser session can't be forged from `document.cookie` to load `/admin`
as an admin. The logged-out denial state was screenshotted directly; both
endpoints were exercised end-to-end with `curl` against a local instance
carrying a minted JWT (which also proved the flush loop works — the local
`127.0.0.1` row appeared in the table with a real hit count, all the way
through middleware → buffer → flush → table → API); the page's rendering
was then driven by feeding `load()` those exact payloads. Light and dark
both checked, no console errors.

**Flagged, not decided: GDPR.** Stored IPs are personal data, the operator
is EU-based serving EU users, and there is a paid tier. Nothing currently
deletes rows and `static/` has no privacy policy page. A retention sweep in
the existing flush loop is cheap to add — left to the human, since it's a
policy call, not an implementation one.

## Admin signups list — `User.created_at` (2026-08-04, same day)

Follow-up request to the admin page: "add new users actual signups also with
mail". The IP tracking counts anonymous traffic; this is the real account
list, so `GET /api/admin/signups` is a separate endpoint rather than an
extension of `/visitors` — the two answer different questions and neither
subsumes the other.

**There was no signup timestamp to sort by.** FastAPI-Users' base table has
`id`/`email`/`hashed_password`/`is_active`/`is_verified` and nothing
temporal, and the ids are `uuid4`, which (unlike uuid1/uuid7) carries no
embedded time — so account age was genuinely unrecoverable, not merely
unexposed. Hence a new `User.created_at`.

**The column is nullable with no backfill, deliberately.** A
`server_default=now()` on the migration would have stamped every
pre-existing account with the deploy timestamp, making the whole existing
user base read as "signed up the day this shipped" — inventing data rather
than admitting its absence. They stay NULL and the page renders "before
tracking". Same reasoning as `VisitorIP.first_seen`'s docstring.

Two consequences worth knowing, both found by tests rather than reasoned
about up front:

- **`ORDER BY created_at DESC` alone is wrong.** Postgres sorts NULLS FIRST
  on DESC, so every undated legacy account would sit above the recent
  signups the page exists to show. Needs `.nullslast()`.
- **A NULL `created_at` cannot be produced through the ORM.** SQLAlchemy
  applies a column default whenever the attribute is None at flush time, so
  even an explicit `User(created_at=None)` comes back stamped with `now()`.
  That is correct for real registrations — they must always get a date, and
  it means `auth.py`'s `UserManager` needed no change at all — but it means
  the only legitimately-NULL rows are the ones the migration added the
  column to. The test seeding a "legacy" account does a follow-up `UPDATE`
  to reproduce that state honestly, rather than weakening the model.

**The response is an explicit field allowlist, not the row.** A `User` also
carries `hashed_password`, `stripe_customer_id` and
`stripe_subscription_id`; none of those belong in a JSON response even a
superuser-only one, and a test asserts the exact key set so a column added
later can't leak by default. Email is included because it *is* the request —
and it's the only durable identifier an account has, since `nickname` stays
NULL until a first forum post.

`admin.html`'s signup rows are built with `textContent` like the IP rows,
and it matters more here: email and nickname are strings the user chose at
registration, so they are the most directly attacker-controlled values on
the page.

Verified in a browser against a seeded mix (brand-new unverified, active
subscriber, free tier, cancelled+inactive, superuser, and one legacy account
with no date) — ordering, badges, and the "before tracking" fallback all
render correctly in both themes, no console errors.
