# PROGRESS — WoW AH Snipe Validator

Living status doc: what's built, what's not, what's next. `CLAUDE.md` is
the authoritative current-state brief (architecture, conventions, roadmap,
API facts). `history.md` has the full session-by-session narrative — this
file is the scannable summary, kept short on purpose (restructured
2026-07-25 after both this file and `CLAUDE.md` grew past the size of the
entire codebase — see `history.md`'s "Full project cleanup pass" entry).

Last updated: 2026-08-03 — new heuristic, **`price_suspect`**
(`snipe_check.py`'s `PRICE_SUSPECT_MULTIPLE=10`): flags a row when the sell
realm's own reference price is >= 10x the EU region median — a recalibrated
revival of the `sell_price_suspect` flag removed 2026-07-31 (median instead
of mean, 10x instead of 500x — see `CLAUDE.md`'s `snipe_check.py` row for
the full reasoning). No new checkbox: rides the dashboard's existing "Hide
flagged (sus items)" checkbox alongside `sus_item_suspect`. Its `.price-flag`
marker originally sat next to the sell-price number, moved into the item-cell
alongside the sus-item flag the same day (human report comparing two real
rows: sitting in a different column read as inconsistent) — both warnings
now live in one spot next to the item name. Also same day: a new **Sale
avg** column shows TSM's EU-region-wide average sale price
(`region_sale_avg_copper`, human request — "region sale avg from tsm, if it
exist"), sortable, dash when TSM has no data for the item. All exposed
unconditionally by `/api/snipes` (pure SQL/cache lookups, no `NameCache`
cost).

2026-08-02 — new feature, **Watchlist** (`watchlist.py`,
`tsm_import.py`, `static/watchlist.html`): a subscribed user tracks specific
items region-wide (no sell realm), sets a plain gold trigger price per item,
and gets a Discord webhook notification when any EU realm's current
cheapest listing clears it. Items can be added one at a time or bulk-
imported from a pasted TSM group export string. The interesting build risk
was decoding that export string: it's LibSerialize+LibDeflate binary data,
not plain text, and the project's own conventions (real test vectors,
"don't guess, get a real sample first") ruled out hand-porting the format
from documentation — a real, human-provided sample string was decoded by
running TSM's own unmodified `LibDeflate.lua`/`LibSerialize.lua` via a
vendored Lua runtime (`lupa`) rather than a reimplementation, after a
research pass confirmed the exact format against TSM's real addon source.
Trigger checking rides `collect_all.py`'s existing ~10-minute cycle (no new
cadence). Full trace in `history.md`'s "Watchlist" entry.

2026-08-02 (earlier) — the WoW Accounts profile page was rebuilt from
scratch (same day it first shipped, after rapid human follow-up):
numbered auto-suggested accounts (cap lowered 10→8, matching Blizzard's
real per-account limit), a side-by-side card grid instead of a stacked
list, a searchable realm typeahead replacing the old dropdown, a loading
skeleton before any data renders, and realm names shown in full everywhere
(no truncation). `/api/realms/eu` now fans out one entry per connected-
realm member name (not one per connected-realm id) so a user can find
their realm by any of its names — confirmed live that Zul'jin and Sanguino
share one connected realm. Also removed the dashboard's "Max per item"
filter (superseded) and restyled the sus-item flag from a muted brown
hourglass to a red warning triangle. Full trace in `history.md`'s "Profile
page redesign + connected-realm fan-out" entry.

2026-08-02 — earlier the same day: new feature, multi-WoW-account
registration. A subscribed user can register their real WoW accounts and
which EU realms each has a character on (`profile.html`, `wow_accounts.py`);
the dashboard gets a new "Your account" column showing which account to
log into for a given snipe. Also fixed a real horizontal-scrollbar
regression the new column introduced (header text now wraps instead of
forcing the table wider than its container). Full trace in `history.md`'s
"Multi-WoW-account registration" entry.

2026-08-01 — the biggest incident this project has had:
a Postgres connection-pool exhaustion (broke login) plus a Blizzard
rate-limit storm (missing icons, failed realm collection), both real,
pre-existing architectural weaknesses triggered back-to-back by the
assistant's own live-debugging activity against production. Root-caused
and fixed properly (not just patched): `/api/snipes` no longer holds a DB
connection during its 30-175s of DuckDB/Blizzard work; every Blizzard API
call in the app now shares a rate limiter; live requests no longer wait
unboundedly on it either (a 15s deadline, self-healing same as existing
patterns). Also: a real `NameCache` lost-update race fixed ("items randomly
jumping"), two new curated sus-item sets, the junk/decoy value floor raised
500→2000, a log-severity misclassification fixed (twice — the first fix
didn't touch the code path actually running in production), a new
superuser-only "who's on the site right now" endpoint, and a new TSM
region-sale-rate filter (`tsm.py`, human request — see "Status at a
glance"). Full trace in `history.md`'s "Production incident" entry and the
several entries around it, all dated 2026-08-01.

## Status at a glance

**Live and working right now**, at `https://wow-project-production.up.railway.app`:
- Register / log in / log out; subscribe/cancel/status on the profile page.
- Subscribe via Stripe (live mode, real payments) → dashboard access
  unlocks via webhook. **Free tier**: any logged-in account can use the
  dashboard with capped, real data (500 rows, raised from 250 2026-08-03,
  locked to one sell realm) — no hard paywall before seeing anything.
  **Anonymous access** (added 2026-08-03): a visitor with no account at all
  can now use `/snipes` too, capped at 250 rows (today's old free-tier
  number, now the incentive to register) and locked to the first sell realm
  it queries the same way the free tier is — identity is a first-party
  `ah_anon` cookie (`db.AnonSession`), not IP, so unrelated visitors sharing
  a network never get merged into one lock.
- Server-side data collection every ~10 minutes for FULL/HIGH-pop EU
  realms, no human machine required. Only the latest snapshot per realm is
  kept (pricing never needed history) — disk usage is flat by construction,
  not adaptively managed.
- Auto-deploy on push to `main`, gated on tests passing.
- One consistent visual identity (light "assay ledger", dark-mode toggle)
  across all eight pages, including the new public landing page.
- `/` is now a public marketing landing page (`static/landing.html`,
  2026-07-26); the sniper tool itself moved to `/snipes`. Every page's
  brand mark links to `/`; nav "Dashboard" links point to `/snipes`.
- Sell price is the sell realm's own **current cheapest live listing**
  (not an inferred sold-price percentile — see `CLAUDE.md`'s "What this
  project is" for why that changed 2026-07-25).
- Matching is now purely `(item_id, pet_species_id, pet_quality_id)` —
  bonus/ilvl differences no longer gate a match at all (changed 2026-07-26,
  see `CLAUDE.md`'s "What this project is" matching-model note); the
  buy-side listing's actual variant is still shown per row for display.
- Client-side filter rail (discount%, gold range, sell-now,
  min sale rate %, unique-transmog, 9-way item-class, 6-way rarity), instant
  table sorting/grouping, an `localStorage` batch cache so a page reload
  paints instantly. Manual Refresh button removed (2026-07-25) —
  `checkForUpdates()` already covers "is there new data" via the 60s
  auto-refresh timer. ("Max per item" removed 2026-08-02, human request —
  superseded.)
- **TSM region-wide sale-rate filter** (`tsm.py`, added 2026-08-01, human
  request — "a filter, where the user can set a minimum sellrate"):
  `collect_all.py`'s background loop refreshes a cache of TSM's public
  EU-region `saleRate`/`soldPerDay` fields (region-wide only — TSM's
  per-realm files don't carry them); every snipe row is annotated with them
  regardless of whether the filter is used, and the dashboard's "Min sale
  rate %" input excludes rows below the threshold (and rows TSM has no data
  for, once a threshold is set). A dedicated, sortable "Sale rate %" table
  column (5 decimal places — human-specified precision, real rates for
  niche items are routinely under 1%) was added the same day after a
  human follow-up. See `CLAUDE.md`'s `tsm.py`/`snipe_check.py`/
  `static/dashboard.html` rows.
- `/api/snipes` no longer holds a Postgres connection during its slow
  DuckDB/Blizzard work (2026-08-01, real production incident — see
  `history.md`); every Blizzard API call in the app shares a rate limiter,
  and live requests self-bound their wait on it (15s) instead of blocking
  indefinitely. A superuser-only `GET /api/admin/active-users` shows who's
  currently on the site.
- **Admin activity page** (2026-08-04, human request): `/admin`, superuser
  only, showing who's on the site now, the full history of every client
  IP that has ever hit `/api/*` (first/last seen, lifetime hit count), and
  a **signups list** — every registered account with its email, nickname,
  signup date and subscription/verification status. The two answer
  different questions (anonymous traffic vs real accounts) and neither
  subsumes the other. Signup dates come from `User.created_at`, added the
  same day; accounts predating it read "before tracking" rather than being
  backfilled with a fabricated date.
  Lives in `admin.py` + `db.VisitorIP`, not a separate Railway service —
  the middleware has to run in the process it observes. Requests stay
  memory-only; a background loop flushes to Postgres once a minute.
  **No geolocation yet** — deliberately deferred (human decision: ship the
  tracking first), so the columns don't exist; adding nullable
  `country`/`city`/`org` is a small follow-up migration once a provider is
  picked. **No retention policy either** — stored IPs are personal data
  under GDPR and nothing currently deletes them; flagged to the human,
  not yet decided.
- **Multi-WoW-account registration** (added 2026-08-02, human request,
  subscribers only; UI rebuilt from scratch the same day, see "Last
  updated" above): register WoW accounts (numbered by default, still
  renameable) and which EU realms each has a character on, searchable by
  any of a connected realm's member names (`/profile`, `wow_accounts.py`,
  capped at 8 accounts/50 realms each, human-specified — 8 matches
  Blizzard's real per-account limit). The dashboard's new "Your account"
  column shows which account to log into for a snipe, computed entirely
  client-side (never sent to the server as part of the
  shared per-realm row cache). See `CLAUDE.md`'s `wow_accounts.py`/
  `db.py`/`static/profile.html`/`static/dashboard.html` rows.
- **Watchlist** (added 2026-08-02, subscribers only): track items region-
  wide with a plain gold trigger price, add by item id or bulk-import a
  TSM group export, Discord webhook delivery with a cooldown so a still-
  cheap listing doesn't re-notify every cycle. See `CLAUDE.md`'s
  `watchlist.py`/`tsm_import.py`/`static/watchlist.html` rows.

**Not built yet**, in priority order — see "Next up" below:
1. **Marketing** — explicitly named the next step (2026-07-26), superseding
   the product/code backlog below in priority. Starting from a blank page;
   see "Next up" #1.
2. ~~A fix for decoy-listing discount% crowding out entire legitimate
   categories from the batch cap~~ — **resolved 2026-07-27**, see "Next up" #2.
3. Restricted Stripe key (still the full `sk_live_...` secret). Human-only, not scheduled.
4. Email verification for new registrations, to stop spam/throwaway free-tier accounts.
5. TSM/Auctionator buylist export idea (parked, no design work).
6. Phase 4 (Deal Score + Discord alerts) — blocked on Phase 3 data.
7. The free in-game addon itself (Phase 5's remaining half).

## Next up (roughly this order)

1. **Marketing** (human, 2026-07-26): explicitly named as the next step
   now that this session's product/bug-fix work has settled — the whole
   session's worth of changes (bonus/ilvl matching removal, the realm-
   switch fixes, the new `/` landing page, all the copy/UX polish) is now
   the thing to actually put in front of people. Direction floated earlier
   the same day, not yet acted on: content creators, Reddit, and Discord
   communities. Genuinely starting from a blank page — no research, channel
   prioritization, audience sizing, or messaging done yet. The new
   `static/landing.html` (see `CLAUDE.md`'s file table) is presumably the
   thing being marketed *to*, so its copy/pitch should be considered
   settled/shareable before spending effort driving traffic to it — flag
   to the human if anything about it still feels unfinished before this
   starts in earnest.
2. **RESOLVED 2026-07-27** — **Decoy-listing discount% is crowding entire legitimate categories out of
   the dashboard's batch cap** (confirmed live 2026-07-26, via `railway ssh`
   against production data on Draenor — see `history.md` for the full trace
   once it's written up, or re-derive with the same technique: `find_snipes()`
   called directly against `analyze.connect(1403)`). Concrete finding: real,
   legitimate housing-item snipes exist (best confirmed discount 88.1%), but
   they never reach the dashboard because `fetchBatch()` only ever fetches
   the **top `BATCH_TOP=5000` rows by discount%** (`static/dashboard.html`),
   and on Draenor that batch is now saturated end-to-end with 99.1%-100%
   discount rows — even the 5000th-ranked row sits at 99.1%. An 88.1%
   discount, while genuinely good, doesn't come close to cracking that
   cutoff. This is almost certainly the same root cause as the already-
   documented troll/decoy pricing-model edge case (see "Known gaps /
   risks" below — a single absurdly-cheap camped/joke listing becomes the
   sell realm's "current cheapest listing" reference price with no sanity
   check, producing inflated discount% numbers) — but this is the first
   time it's been confirmed to actually **crowd out other, unrelated items/categories**
   from ever being seen at all, not just mis-price the item it directly
   corrupts. Likely affects more than just Housing — anything without an
   extreme (99%+) discount on a sell realm with enough decoy listings would
   have the same problem. Not yet fixed; open directions to evaluate, not
   decided:
   - Sanity-check/exclude obviously-decoy reference prices before they're
     read as "the sell realm's current cheapest listing" (reintroduces some
     of the classification complexity that was deliberately removed
     2026-07-25 — needs to be weighed against that decision, not just
     reversed reflexively). **2026-07-27 update**: a human decision was made
     on this specific question — flag, don't exclude (`sell_price_suspect`,
     since removed 2026-07-31, see `history.md`). That decision stood at the
     time, and on its own it didn't resolve this batch-crowding item — the
     `class_quotas` mechanism below (also shipped 2026-07-27) is what
     actually did.
   - Raise `BATCH_TOP` and/or restructure the batch fetch to guarantee some
     minimum representation per item-class/category, so one flooded
     category can't zero out another. **Done 2026-07-27**: this is the
     direction that shipped. `snipe_check.find_snipes()`'s new `class_quotas`
     param (see `CLAUDE.md`'s `snipe_check.py` row) caps each item-class
     bucket independently instead of one flat top-N by discount% —
     `dashboard.py`'s `_class_quotas(user)` supplies human-specified,
     per-tier numbers (free at the time: weapon 50/armor 100/housing 40/
     mount 5/battlepet 5/recipe 50, summing to exactly its then-250 cap,
     deliberately zero quest/profession/container; subscribed/superuser add
     fixed floors for those three plus the free tier's own ratios scaled up
     — see `CLAUDE.md` for the exact current numbers, doubled 2026-08-03
     alongside the free tier's cap rising 250→500, same ratios throughout).
     **`recipe` bucket added 2026-07-28** (item_class
     9, confirmed live, distinct from Profession's 19 — recipes had no
     bucket at all before this): free tier's `weapon` quota was halved from
     100 to fund an equal 50 `recipe`, and every other tier's weapon/recipe
     split was rescaled the same way. `BATCH_TOP` itself was left at 10000, not raised
     further, at explicit human request ("we want most items as possible").
     **A first version of this shipped and was corrected the same day**: it
     widened the SQL `LIMIT` to a fixed ceiling (20,000) and bucketed in
     Python afterward, which turned out not to actually guarantee anything
     — live-verified on Draenor that 450,568 rows qualify region-wide, and
     the first genuine Housing candidate sat at rank 39,524, past that
     ceiling. The fix (still 2026-07-27): rank per bucket as a real SQL
     `ROW_NUMBER() OVER (PARTITION BY bucket ...)` window function over the
     *entire*, untruncated candidate set — see `CLAUDE.md`'s
     `snipe_check.py` row for the mechanism.
   - First confirm the suspected root cause directly: sample a handful of
     the 99-100% discount rows and check whether they're genuinely single-
     copy, wildly-off-market troll listings (as suspected) rather than
     something else entirely — don't design a fix before that's verified.
     Confirmed 2026-07-27 via `sell_price_suspect`/`region_median_g` and
     direct production sampling (Draenor item 36519 and others) — yes,
     decoy/troll pricing, exactly as suspected.
3. **Restricted Stripe key** — swap the full `sk_live_...` secret for a key
   restricted to Checkout/Customers/Subscriptions/Webhooks. **Human-only,
   asked explicitly**: do not rotate/swap this live credential without the
   human present, even when otherwise told to keep working autonomously.
4. **Email verification for new registrations** (scoped 2026-07-26, not
   started) — goal: require a verified email before an account reaches any
   `current_active_user`-gated route (`/api/me`, `/api/snipes`,
   `/api/realms`, `/api/status`), so a spam/throwaway registration can't
   consume free-tier resources (server-side snipe queries, a realm-lock
   slot). Current state: `db.py`'s `User` model already has an
   `is_verified` column (inherited from FastAPI-Users' base table from day
   one) but it's never read or written anywhere — no new migration needed
   for the column itself. **No email-sending capability exists in this
   project at all** (no SMTP config, no provider SDK, nothing in
   `.env.example`) — that's the real gap. `auth.py`'s `UserManager` has no
   `on_after_register`/`on_after_request_verify` hooks, and `dashboard.py`
   never mounts `fastapi_users.get_verify_router(...)`. Open decisions
   before this can be built:
   - **Email provider** — picking one (Resend/SendGrid/Postmark/SMTP)
     means creating a third-party account, a human-only step (same pattern
     as the Battle.net API client below). Resend was the lightweight
     recommendation (single REST POST via the `httpx` dependency already
     present, no new library) but not decided.
   - **Existing-account backfill** — production already has real accounts
     (the founder/superuser account, any current subscribers) with
     `is_verified=False` by default. Flipping the gate on without a
     one-time backfill (mark every pre-existing account verified) would
     lock all of them out on deploy.
   - UX not yet designed: `register.html`'s post-registration messaging
     (currently silently redirects to `/login`, no "check your email"
     state), a `/verify?token=...` landing page, a "resend verification
     email" affordance.
   - `tests/test_auth.py` already exercises register/login/route-gating
     end-to-end against a real throwaway SQLite DB — extending it (an
     unverified-account-blocked case, updating the existing tests that
     assume register+login alone reaches `/api/snipes`/`/api/status`) is
     part of this work, not an afterthought.
5. **TSM/Auctionator buylist export** (see "Future work" below) — no design done.
6. **CLI parity for `sus_item_suspect`** (2026-07-31) — currently
   dashboard-only. Deferred, not because it's hard: the constants/predicate
   already live in `snipe_check.py`, so a `--hide-sus-items` CLI flag
   would follow `_filter_by_appearance()`'s exact existing pattern. Just not
   built yet since the experimental filter shipped dashboard-first.
7. Phase 2 (commodities feed) — explicitly out of scope, not being pursued.

## Future work (ideas, not scheduled)

- **Export selected snipes as a buylist for Auctionator or TSM** (human
  idea, 2026-07-23). Needs: research into the exact TSM/Auctionator import
  string formats, a UI selection mechanism, a client-side export button
  (likely no backend change — formatting already-fetched row data).
- ~~**TSM public pricing data as a cross-check, not a replacement**~~ (human
  idea, 2026-07-25, investigated, parked as a *pricing* cross-check — see
  `history.md` for the full writeup on why it's not an independent ground
  truth) — **the narrowly-scoped version shipped 2026-08-01** as a liquidity
  filter instead (`tsm.py`'s `saleRate`/`soldPerDay`, see "Status at a
  glance"), not a pricing cross-check. The buylist-export idea directly
  above remains separately unshipped.
**Remember**: if a custom domain ever replaces the `railway.app`
subdomain, the Stripe webhook endpoint URL needs updating by hand in the
Stripe Dashboard — it won't happen automatically.

## Hosted SaaS pivot — status

All 5 stages (GitHub/CI, auth, Stripe, server-side collection, CD) are
**done** — see `CLAUDE.md`'s "Current state" file table for what each piece
does now, and `history.md`'s "Hosted SaaS pivot" entry for how it shipped.

## Longer-term roadmap (beyond the hosted pivot)

| Phase | What it is | Status |
|---|---|---|
| 0 — Validate the sale-inference signal | 48h manual verification protocol | **Gated, skipped** (2026-07-20) — signal still unvalidated against real seller behavior. |
| 1 — Cross-realm engine + hardening | Region scanner, snipe-check, orchestration | **Mostly done.** Remaining: sell/scan realm config file, `--since` incremental diff. |
| 2 — Commodities feed | Region-wide, quantity-delta inference | **Out of scope** (2026-07-24) — not being pursued. |
| 3 — Appearance layer | ItemModifiedAppearance scarcity mapping | **Groundwork started 2026-07-23.** Done: itemId→appearance-rarity mapping, wired into "unique transmog" filter. Not done: static-API fallback, real obtainability flags, region-wide AH scarcity of *currently listed* appearances. Transferability flag question is **closed, no flag will be built**. |
| 4 — Deal score + Discord alerts | Second paid feature | Deal score itself not started — blocked on Phase 3 data. **Discord delivery infra now exists** (`watchlist.py`'s per-user webhook + notification, built 2026-08-02 for Watchlist, a separate feature) and could plausibly be reused rather than rebuilt when this phase starts. |
| 5 — Free companion addon | In-game tooltip overlay | Not started. The *web dashboard* half already shipped; only the addon itself remains. |

## Known gaps / risks

- **A genuinely live troll/decoy current listing can still become a
  market's reference price.** Inherent to the current pricing model, not a
  bug in it — see `history.md`'s "Pricing model replaced" entry (item
  13051). No sale-classification layer exists to catch "this current
  listing looks like a decoy"; not attempted. **Confirmed 2026-07-26 to
  have a second-order effect**: on Draenor, decoy-inflated discount%
  numbers saturated the dashboard's entire batch cap (at the then-current
  `BATCH_TOP=5000`, the 5000th row was still at 99.1% discount), crowding
  out real, lower-but-still-good snipes in other categories entirely
  (Housing's best real discount, 88.1%, never surfaced at all) — see "Next
  up" #1 for the full trace and open directions. `BATCH_TOP`/the superuser
  tier cap were both raised to 10000 the same day (unrelated request), not
  independently re-measured against this specific saturation issue.
  **Partial mitigation shipped 2026-07-26** (traced live via a user report
  on Draenor item 36519, Moonlit Katana, ~93x the real Undermine Exchange
  price): `snipe_check.find_snipes()` now flags (`sell_price_suspect`) any
  row whose sell-realm reference price is over `SELL_PRICE_SCAM_MULTIPLE`
  (500x) the *average* price for that item across the rest of the scanned
  region, surfaced as a `⚠` in the dashboard and CLI plus a "hide flagged"
  checkbox (unchecked by default). **This does not fix the crowding-out
  effect above** — a human decision, not an oversight: a flagged row is
  deliberately still included and still sorted by its (possibly inflated)
  discount%, so it can still occupy a `fetchBatch()` slot ahead of a real,
  lower-discount snipe unless the user manually checks "hide flagged".
  Reintroducing an actual exclusion was considered and explicitly rejected
  in favor of surfacing the signal (see `snipe_check.py`'s
  `SELL_PRICE_SCAM_MULTIPLE` comment) — the crowding-out problem needed
  solving on its own, and **was, the same day**: `find_snipes()`'s new
  `class_quotas` param caps each item-class bucket independently instead of
  one flat top-N by discount%, so a saturated category (whether or not any
  of its rows individually clear the 500x `sell_price_suspect` bar) can no
  longer zero out another category's real, lower-discount snipes. See
  `CLAUDE.md`'s `snipe_check.py`/`dashboard.py` rows for the exact
  per-tier numbers (human-specified, not scaled/decided by the assistant).
  **A second, distinct symptom of the same troll-listing risk fixed
  2026-07-28** (human report on Draenor, item "Bent Staff" priced at
  ~9,999,999g): `discount_pct` is rounded to 1 decimal at the SQL level, so
  a single troll-priced sell reference can make dozens of genuinely-
  different buy prices all round to the exact same displayed discount% —
  with no secondary sort key anywhere in the pipeline (SQL `ORDER BY`,
  `dashboard.html`'s `compareRows()`, or its per-group sort), their
  relative display order was an arbitrary DB scan order, not sorted by
  anything visible. Fixed by adding cheapest-buy-price-first as a
  tiebreaker in all three places (`snipe_check.SORT_COLUMNS`,
  `dashboard.html`'s `compareRows()`/`tiebreak()`, and `buildGroups()`'s
  within-group sort) — human-specified tiebreak semantics, not decided by
  the assistant. Doesn't address the underlying troll-reference-price risk
  itself, only the ordering artifact it exposed.
  **`sell_price_suspect` itself was removed 2026-07-31** (human decision) —
  the tiebreak/class_quotas mitigations above are unaffected and remain in
  place; see `history.md` for why the flag was dropped and what replaced it
  (`snipe_check.is_sus_item()`/`sus_item_suspect`, a broader,
  differently-scoped signal, not a like-for-like swap).
  **`MIN_VALUE_FLOOR_G` added 2026-08-01** (human request, currently 2000,
  raised same day from an initial 500) is a related but distinct
  mitigation — drops a row from the candidate pool entirely when *both* the
  sell price and EU median are under the floor, freeing budget rather than
  flagging/reordering. Doesn't address a troll listing that clears the
  floor on its own (a camped listing priced in the thousands would still
  pass); see `CLAUDE.md`'s `snipe_check.py` row for the exact OR-to-keep
  semantics.
- Sale-inference classification (`inferred_sale` especially) has never been
  checked against real seller behavior — Phase 0's gate was skipped. No
  longer on the pricing path, and no longer runs automatically at all
  (2026-07-25) — it's a manual/ad-hoc tool for `analyze.py`'s debugging
  commands now, requiring a human to accumulate snapshot history first.
- No sell/scan realm config file — `--exclude`/`--items` CLI flags are the
  manual stand-in.
- The AH `modifiers` type-28 field ("item level") isn't Blizzard-documented;
  `market_key()` (see `CLAUDE.md`'s "Inference logic") conditionally pools
  implausible values for `diff_snapshots.py`'s relist detection and
  `analyze.py`'s manual debugging tool — the only remaining live callers
  since 2026-07-26 (live pricing/matching in `snipe_check.py` dropped
  bonus/ilvl-aware matching entirely, see "Status at a glance"). The
  underlying meaning is still community-sourced, not official. Same caveat
  applies to modifier types 9/42/44.
- **Bonus/ilvl differences no longer gate a snipe match at all** (human
  product decision, 2026-07-26, see `history.md`) — every variant of an
  item_id is treated as one market, priced at the sell realm's overall
  cheapest listing. This is a deliberate tradeoff, not a bug: a genuinely
  different-value variant (e.g. a much higher ilvl roll) can now be priced
  against a cheaper lower-tier listing's reference price, which the
  previous market_key()-based design specifically existed to prevent. The
  buy-side listing's actual bonus_key/ilvl is still shown per row so a user
  can judge for themselves before buying.
- **No test coverage for "does an async route block the event loop"** — see
  `CLAUDE.md`'s "Real production outage" note. This exact gap let the same
  bug recur at a second call site 2026-07-26 (a realm switch hanging/timing
  out — fixed, see `history.md`); still no regression test for the class of
  bug itself, only unit coverage for the specific fix
  (`tests/test_item_names.py`'s `ensure_icons_many` tests).
- **If a sell realm's entire observed history for an item is troll/camped
  listings, no existing guard can rescue the estimate for `analyze.py`'s
  manual debugging output** (found live 2026-07-24, item 7761). Not built,
  not likely to be revisited — `analyze.py`'s classification output is now
  a fully manual/ad-hoc tool (2026-07-25), and pricing itself never reads
  `sales` at all.
- `appearance.py`'s rarity signal (`source_count`) is known to diverge from
  Wowhead's own "same model as" data on at least one item (14042). No
  Wowhead API exists to reconcile against.
- `snipe_check.is_sus_item()`'s `sus_item_suspect` flag (2026-07-31,
  experimental, dashboard-only): the jewelry-ilvl half can't distinguish old
  dead vendor jewelry from a genuinely valuable "twink" item (level-bracket
  PvP/leveling builds specifically prize old low-ilvl neck/ring/trinket
  items) — same reason it's never filtered server-side, only an opt-in
  checkbox. `LEGACY_JEWELRY_ILVL_MAX` (150) is a tunable starting cutoff,
  not a rigorously derived one. The `CLASS_STARTER_ARMOR_ITEM_IDS` half
  (52 confirmed ids) has no equivalent blind spot — it's a curated,
  live-verified id set, not a threshold. See `history.md` for the
  live-verified examples of both, and for `sell_price_suspect`'s removal
  the same day.
- The tightened background-poll window (`:18-:26` past the hour, narrowed
  2026-08-02 from an earlier `:12-:28` estimate on further human-confirmed
  observation) is still a shared, global schedule, not learned per-realm.
- Stripe is on the full `sk_live_...` secret key, not a restricted key
  scoped to what `billing.py` actually needs (see "Next up").

## What's built (file-level)

| Component | File(s) |
|---|---|
| Sale-inference engine (core IP, manual/ad-hoc only since 2026-07-25) | `diff_snapshots.py` |
| Realm collector | `fetch_snapshot.py` (called by `collect_all.py`, not run standalone anymore) |
| Region scanner | `scan_region.py` |
| Snipe-check logic | `snipe_check.py` |
| Server-side collection | `collect_all.py` |
| Public landing page | `static/landing.html` (`GET /`, added 2026-07-26) |
| Web dashboard (the sniper tool) | `dashboard.py`, `static/dashboard.html` (`GET /snipes`, moved from `/` 2026-07-26) |
| Auth | `auth.py`, `db.py`, `static/login.html`/`register.html` |
| Billing | `billing.py`, `static/subscribe.html` |
| Item name/icon/quality cache | `item_names.py` |
| Appearance-rarity cache (Phase 3 groundwork) | `appearance.py` |
| TSM region-wide sale-rate cache | `tsm.py` (added 2026-08-01) |
| Multi-WoW-account registration | `wow_accounts.py`, `static/profile.html` (added 2026-08-02) |
| Watchlist (region-wide item tracking + Discord alerts) | `watchlist.py`, `tsm_import.py`, `static/watchlist.html`, `vendor/tsm_lua/` (added 2026-08-02) |
| Anonymous (no-account) dashboard access | `db.AnonSession`, `auth.resolve_or_create_anon_session()`, `dashboard.ensure_anon_cookie`/`_enforce_anon_realm_lock` (added 2026-08-03) |
| Public pricing page | `static/pricing.html`, `GET /pricing` in `dashboard.py` |
| Hosting | `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore` |
| Tests | `tests/` (`pytest -q`; locally just the affected `tests/test_<module>.py` — CI runs the full suite and gates the deploy), no external services needed |

## Where to look for more

- `CLAUDE.md` — architecture, conventions, current-state file table,
  Blizzard API facts, roadmap (authoritative for "what's true now").
- `history.md` — full session-by-session incident log ("why" and "how we
  got here" for anything summarized above).
- `README.md` — human-facing setup/usage.
- `git log` — commit-level history.
