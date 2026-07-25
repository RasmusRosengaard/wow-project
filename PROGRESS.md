# PROGRESS — WoW AH Snipe Validator

Living status doc: what's built, what's not, what's next. `CLAUDE.md` is
the authoritative current-state brief (architecture, conventions, roadmap,
API facts). `HISTORY.md` has the full session-by-session narrative — this
file is the scannable summary, kept short on purpose (restructured
2026-07-25 after both this file and `CLAUDE.md` grew past the size of the
entire codebase — see `HISTORY.md`'s "Full project cleanup pass" entry).

Last updated: 2026-07-25 (post-cleanup-pass, then retention removed the same day).

## Status at a glance

**Live and working right now**, at `https://wow-project-production.up.railway.app`:
- Register / log in / log out; subscribe/cancel/status on the profile page.
- Subscribe via Stripe (live mode, real payments) → dashboard access
  unlocks via webhook. **Free tier**: any logged-in account can use the
  dashboard with capped, real data (250 rows, locked to one sell realm) —
  no hard paywall before seeing anything.
- Server-side data collection every ~10 minutes for FULL/HIGH-pop EU
  realms, no human machine required. Only the latest snapshot per realm is
  kept (pricing never needed history) — disk usage is flat by construction,
  not adaptively managed.
- Auto-deploy on push to `main`, gated on tests passing.
- One consistent visual identity (light "assay ledger", dark-mode toggle)
  across all six pages.
- Sell price is the sell realm's own **current cheapest live listing**
  (not an inferred sold-price percentile — see `CLAUDE.md`'s "What this
  project is" for why that changed 2026-07-25).
- Client-side filter rail (discount%, gold range, sell-now, max-per-item,
  unique-transmog, 8-way item-class, 6-way rarity), instant table
  sorting/grouping, an `localStorage` batch cache so a page reload paints
  instantly. Manual Refresh button removed (2026-07-25) — `checkForUpdates()`
  already covers "is there new data" via the 60s auto-refresh timer.

**Not built yet**, in priority order — see "Next up" below:
1. Restricted Stripe key (still the full `sk_live_...` secret). Human-only, not scheduled.
2. TSM/Auctionator buylist export idea (parked, no design work).
3. Phase 4 (Deal Score + Discord alerts) — blocked on Phase 3 data.
4. The free in-game addon itself (Phase 5's remaining half).

## Next up (roughly this order)

1. **Restricted Stripe key** — swap the full `sk_live_...` secret for a key
   restricted to Checkout/Customers/Subscriptions/Webhooks. **Human-only,
   asked explicitly**: do not rotate/swap this live credential without the
   human present, even when otherwise told to keep working autonomously.
2. **TSM/Auctionator buylist export** (see "Future work" below) — no design done.
3. **A residual pricing-model edge case**, documented not fixed: a
   genuinely live troll/decoy current listing can still become a market's
   reference price (inherent to the current-cheapest-listing design, not a
   bug in it) — see `HISTORY.md`'s "Pricing model replaced" entry for the
   item 13051 case that surfaced this.
4. Phase 2 (commodities feed) — explicitly out of scope, not being pursued.

## Future work (ideas, not scheduled)

- **Export selected snipes as a buylist for Auctionator or TSM** (human
  idea, 2026-07-23). Needs: research into the exact TSM/Auctionator import
  string formats, a UI selection mechanism, a client-side export button
  (likely no backend change — formatting already-fetched row data).
- **TSM public pricing data as a cross-check, not a replacement** (human
  idea, 2026-07-25, investigated, parked — see `HISTORY.md` for the full
  writeup on why it's not an independent ground truth and what a
  narrowly-scoped version would look like).

**Remember**: if a custom domain ever replaces the `railway.app`
subdomain, the Stripe webhook endpoint URL needs updating by hand in the
Stripe Dashboard — it won't happen automatically.

## Hosted SaaS pivot — status

All 5 stages (GitHub/CI, auth, Stripe, server-side collection, CD) are
**done** — see `CLAUDE.md`'s "Current state" file table for what each piece
does now, and `HISTORY.md`'s "Hosted SaaS pivot" entry for how it shipped.

## Longer-term roadmap (beyond the hosted pivot)

| Phase | What it is | Status |
|---|---|---|
| 0 — Validate the sale-inference signal | 48h manual verification protocol | **Gated, skipped** (2026-07-20) — signal still unvalidated against real seller behavior. |
| 1 — Cross-realm engine + hardening | Region scanner, snipe-check, orchestration | **Mostly done.** Remaining: sell/scan realm config file, `--since` incremental diff. |
| 2 — Commodities feed | Region-wide, quantity-delta inference | **Out of scope** (2026-07-24) — not being pursued. |
| 3 — Appearance layer | ItemModifiedAppearance scarcity mapping | **Groundwork started 2026-07-23.** Done: itemId→appearance-rarity mapping, wired into "unique transmog" filter. Not done: static-API fallback, real obtainability flags, region-wide AH scarcity of *currently listed* appearances. Transferability flag question is **closed, no flag will be built**. |
| 4 — Deal score + Discord alerts | Second paid feature | Not started — blocked on Phase 3 data. |
| 5 — Free companion addon | In-game tooltip overlay | Not started. The *web dashboard* half already shipped; only the addon itself remains. |

## Known gaps / risks

- **A genuinely live troll/decoy current listing can still become a
  market's reference price.** Inherent to the current pricing model, not a
  bug in it — see `HISTORY.md`'s "Pricing model replaced" entry (item
  13051). No sale-classification layer exists to catch "this current
  listing looks like a decoy"; not attempted.
- Sale-inference classification (`inferred_sale` especially) has never been
  checked against real seller behavior — Phase 0's gate was skipped. No
  longer on the pricing path, and no longer runs automatically at all
  (2026-07-25) — it's a manual/ad-hoc tool for `analyze.py`'s debugging
  commands now, requiring a human to accumulate snapshot history first.
- No sell/scan realm config file — `--exclude`/`--items` CLI flags are the
  manual stand-in.
- The AH `modifiers` type-28 field ("item level") isn't Blizzard-documented;
  `market_key()` conditionally pools implausible values (see `CLAUDE.md`'s
  "Inference logic"), but the underlying meaning is still community-sourced,
  not official. Same caveat applies to modifier types 9/42/44.
- **No test coverage for "does an async route block the event loop"** — see
  `CLAUDE.md`'s "Real production outage" note. Worth a regression test if
  this class of bug recurs.
- **If a sell realm's entire observed history for an item is troll/camped
  listings, no existing guard can rescue the estimate for `analyze.py`'s
  manual debugging output** (found live 2026-07-24, item 7761). Not built,
  not likely to be revisited — `analyze.py`'s classification output is now
  a fully manual/ad-hoc tool (2026-07-25), and pricing itself never reads
  `sales` at all.
- `appearance.py`'s rarity signal (`source_count`) is known to diverge from
  Wowhead's own "same model as" data on at least one item (14042). No
  Wowhead API exists to reconcile against.
- The tightened background-poll window (`:12-:28` past the hour) is based
  on ~7 observed data points from one realm (Draenor) — a shared, global
  schedule, not learned per-realm.
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
| Web dashboard | `dashboard.py`, `static/dashboard.html` |
| Auth | `auth.py`, `db.py`, `static/login.html`/`register.html` |
| Billing | `billing.py`, `static/subscribe.html` |
| Item name/icon/quality cache | `item_names.py` |
| Appearance-rarity cache (Phase 3 groundwork) | `appearance.py` |
| Public retrieval-time log | `static/log.html`, `GET /api/log`/`GET /api/log/realms` in `dashboard.py` |
| Public pricing page | `static/pricing.html`, `GET /pricing` in `dashboard.py` |
| Hosting | `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore` |
| Tests | `tests/` (`pytest -q`; run `env -u DATABASE_URL pytest -q` too before pushing), no external services needed |

## Where to look for more

- `CLAUDE.md` — architecture, conventions, current-state file table,
  Blizzard API facts, roadmap (authoritative for "what's true now").
- `HISTORY.md` — full session-by-session incident log ("why" and "how we
  got here" for anything summarized above).
- `README.md` — human-facing setup/usage.
- `git log` — commit-level history.
