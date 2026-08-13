# CLAUDE.md — WoW AH Snipe Validator (EU retail)

Agent-facing brief. This file is loaded into context **every session**, so it
holds only what's always needed: what the product is, the rules that must never
be broken, and where to find everything else. `README.md` is the human-facing
version — keep both in sync when architecture or commands change.

Detail lives in `.claude/docs/` and is read **on demand** — don't open these
unless the task actually touches them.

| Read this | When |
|---|---|
| `.claude/docs/modules-pipeline.md` | Changing collection, scanning or pricing (`blizz`, `fetch_snapshot`, `scan_region`, `collect_all`, `snipe_check`, `speed_check`, `diff_snapshots`, `analyze`, `appearance`, `item_names`, `tsm`, `tsm_import`) |
| `.claude/docs/modules-web.md` | Changing the API, auth, billing or DB models (`dashboard.py`, `auth`, `db`, `billing`, `forum`, `wow_accounts`, `watchlist`, `admin`, `mailer`) |
| `.claude/docs/modules-frontend.md` | Changing any page under `static/` |
| `.claude/docs/modules-infra.md` | Tests, Docker, migrations, dependencies, repo tooling |
| `.claude/docs/architecture.md` | Data layout on disk, schemas, **Blizzard API facts** (trust these, don't guess) |
| `.claude/docs/matching.md` | Matching/inference logic, `market_key()`, and the async-blocking pitfall that has recurred |
| `.claude/docs/roadmap.md` | Build order and every deliberate deviation from it |
| `.claude/docs/progress.md` | Scannable done/not-done status per feature |
| `.claude/docs/history.md` | *Why* a constant, threshold or design choice exists — search it for the item id or file name in a nearby comment |
| `INTEGRATIONS.md` | **Read before touching any external service** — Railway/GitHub/Stripe/Resend/Google/Blizzard/Discord: which console, which env vars, what breaks without it, and the gotchas that have already cost time (Railway administers the DNS, *not* name.com; the People API must stay enabled; tests must never reach live Stripe) |

## What this project is

A **validation layer for WoW auction-house sniping**, not another sniper.
Existing tools (TSM Sniper, Auctionator) flag items cheap relative to *listing*
prices. This project deep-collects one or more sell realms' auction data and
separately scans every other EU realm's current listings, flagging a listing
elsewhere that's cheap relative to your sell realm's own current cheapest
listing for that item, net of the 5% AH cut.

Two product decisions that override anything you might infer from older code:

- **Pricing (2026-07-25):** sell price is simply the sell realm's own current
  cheapest live listing — directly observable, no classification. It used to be
  an inferred sold-price percentile (the original differentiator); three live
  pricing bugs from troll/camped listings killed that. This makes a snipe a
  listing-to-listing comparison, the same shape TSM Sniper does — a deliberate,
  accepted tradeoff, not an oversight. `diff_snapshots.py`'s classification
  engine still exists and still works, but is no longer on the pricing path.
- **Matching (2026-07-26):** purely **`(item_id, pet_species_id,
  pet_quality_id)`**. Every bonus/ilvl variant of an item is one market, priced
  at the sell realm's overall cheapest listing for that `item_id`. The buy-side
  listing's own `bonus_key`/ilvl is still shown per row, display only.

One consequence of that matching model, and the one experimental feature built
on it: pooling every variant under one `item_id` makes **tertiary stats
invisible to pricing**. `speed_check.py` + `/api/speed` + `/speed` (added
2026-08-12) are a deliberately **additive, off-the-pricing-path** census of
listings carrying the **+Speed** tertiary (`b:` bonus id **42** — verified,
not guessed; see `.claude/docs/modules-pipeline.md`). It reads the raw region
scan only, shares no filter or threshold with `snipe_check.py`, and
**flags/filters nothing by default** — gold validation was explicitly deferred
by the human. Don't add a cutoff to it without them.

Three things about that view that are easy to get wrong. **Item level does not
come from modifier 28** (which reports junk — 3321/5381 — for this family);
it's resolved from upgrade-track bonus ids in `speed_check.ILVL_BONUS_IDS`,
each tooltip-verified. **The in-game AH browse tooltip understates `m:9`
listings** — it shows 133 where the item you actually receive is 253 (human,
after buying one, 2026-08-13) — so never "correct" `ILVL_BONUS_IDS` or
`MAX_CHARACTER_LEVEL` from a hover; the delivered item is ground truth, and
that mistake has already been made and reverted once. The page's item-level
control offers exactly **266 and 253**, one at a time, +Speed only, and never
220 ("NEVERSHOW 220, ONLY266"); `TRACKED_ILVLS` is `[266]`. 253 is a real
level but **no +Speed listing currently carries it region-wide**, so that
option is empty by design and the page says so rather than looking broken.
And any
conclusion about *current* content must be checked against production data via
`railway ssh`, not local `data/` — this machine powers off nightly, so the
local sweep runs weeks stale and already produced one wrong conclusion
("ilvl 266 doesn't exist").

Business model (decided — don't revisit without the human): free in-game addon
(Blizzard requires addons to be free) + paid external data service. Competitors:
TSM (coarse regional sale rates, hostile UX), Saddlebag Exchange. Our edge:
cross-realm snipe routing, and eventually appearance-level intelligence. The web
dashboard has an anonymous tier, a free logged-in tier, a subscription, and an
internal superuser tier.

Cutting across those tiers since 2026-08-09: **a confirmed email address is
required to log in with a password.** `/auth/login` is mounted with
`requires_verification=True` and answers **400 `LOGIN_USER_NOT_VERIFIED`** until
the emailed link is clicked. This reversed the soft gate of 2026-08-06 on the
human's explicit instruction — if you find older text describing the gate as
soft, that text is stale, not a decision to restore.

Two carve-outs, both deliberate:

- **Google login is exempt** and needs no code to stay that way: `/auth/google/*`
  runs with `is_verified_by_default=True` and `auth.UserManager.oauth_callback`
  covers the associate-by-email path, so the account is verified before any login
  check sees it. That also makes Google the escape hatch for a password account
  whose confirmation mail never arrived.
- **The anonymous tier is untouched.** `/api/snipes`, `/api/status` and
  `/api/realms` need no account at all — the gate is on logging *in*, not on the
  data. Don't extend it to the read paths.

`auth.current_verified_user` (**403**, so the frontend can tell it apart from a
401) still guards Stripe checkout, the Snipe Board and Discord alerts. Keep it:
it re-reads the live DB row, which is what makes it hold for a session minted
while the account was verified.

**Current phase: 0 — the sale-inference signal's validation gate was explicitly
skipped** (human decision, 2026-07-20) in order to build ahead. The
classification engine has never been checked against real seller behavior.

## Market structure (Warbands — core to the product)

Since The War Within the warband bank makes **gold account-wide** and lets
**unsoulbound BoE items move between a player's characters on any realm**. The
gear/transmog AH is still per connected realm (separate listings, buyer pools,
prices) — so cheap listings rot on low-pop realms while hubs pay full price.
That asymmetry IS the product:

- The user picks **sell realms** (their high/full-pop hubs); we deep-collect
  those.
- A lightweight **region scanner** sweeps every other EU connected realm's
  current listings (no history needed on the buy side).
- A **snipe** is a listing on any realm priced well below the sell realm's own
  current cheapest listing for that item, net of the 5% AH cut.
- An AH listing is guaranteed unsoulbound — BoP items cannot be listed, full
  stop. The only remaining transfer risk is *usage*: don't equip/use an item
  before moving it through the warband bank. No per-item Warbound/BoP flag is
  needed or planned (closed, not deferred, 2026-07-24).

Rate-limit math holds: ~6 req/h per deep-collected realm; sweeping all ~100 EU
connected realms hourly is ~100 dumps/h against a 36,000 req/h limit.

## Non-negotiable guardrails

- **Decision support only.** Never write code that automates in-game actions: no
  auction posting/buying automation, no input simulation, no game-client memory
  reading, no packet interception. ToS compliance is a product requirement, not
  a preference. If a task drifts that way, stop and flag it.
- The future in-game addon must be **free**; monetization lives in the external
  service only.
- Before any payment/monetization feature ships, the human must re-read the
  current Blizzard Developer API Terms of Use. Do not ship payments
  autonomously.
- Secrets live in `.env`, never in code, never committed.
- **Never rotate/swap the live Stripe secret key without the human present**,
  even when otherwise told to keep working autonomously.
- No "WoW"/"Warcraft" in product branding; "for World of Warcraft" as a
  description is the accepted form.

## Conventions

- Python 3.10+, stdlib `argparse` CLIs, minimal deps. No pandas, no ORM for the
  AH data. DuckDB does the analytics. FastAPI + uvicorn is the one web
  framework; the frontend is static HTML + vanilla JS with **no build step**.
- **Prices are copper end to end** (10,000 = 1 gold). Only format as gold at
  display boundaries. This has caused real bugs — check units on every new money
  field, and expose both `_g` and `_copper` variants on API rows.
- Small modules, pure functions where possible (`classify_pair`, `bonus_key`,
  `parse_bonus_key`, `rows` are deliberately pure — keep them testable).
- Heuristics **flag, never silently filter** server-side; hiding is the
  dashboard checkbox's job. Pricing/matching thresholds are human-specified —
  propose the mechanism and calibration and wait, don't pick defaults yourself
  even when told "whatever it takes".
- Derived data (`data/events/`) is always recomputed from scratch; never make it
  incrementally stateful without also keeping the idempotent path.
- The collector loop must survive any exception (it guards a multi-day run).
- An `async def` FastAPI route does **not** by itself protect you from blocking
  the event loop. Any new synchronous, possibly-slow call inside one needs
  `asyncio.to_thread()`. This has already recurred once —
  see `.claude/docs/matching.md`.
- Update this file and `README.md` when commands, schemas or architecture
  change; `.claude/docs/progress.md` when a feature ships; put incident
  narrative in `.claude/docs/history.md`, never inline here.

## Commands

Standalone debugging/inspection tools — none of these is how the product
actually runs (`collect_all.py` inside the deployed app is).

```
python fetch_snapshot.py --find silvermoon          # realm slug -> cr-id
python fetch_snapshot.py --cr-id 1096 --loop        # collect, local debugging only
python diff_snapshots.py --cr-id 1096               # build events
python analyze.py --cr-id 1096 summary --top 30
python analyze.py --cr-id 1096 item 152510 --price 2500000   # copper
python scan_region.py --exclude 1403          # one sweep of all EU realms
python snipe_check.py --sell 1403             # flag discounted listings
python speed_check.py --top 50                # +Speed tertiary census (experimental,
                                              # no sell realm, no filters by default)
python speed_check.py --tarnished --tracked   # Midnight set, ilvl 266 only
python dashboard.py --sell 1403               # local dev on 127.0.0.1:8000
                                              # (leave ENABLE_BACKGROUND_COLLECTION unset)
```

## Testing and shipping

Run **only the tests covering your change** — CI runs the full suite on every
push and gates the Railway deploy, so re-running all of it locally only
duplicates that:

| Changed | Run |
|---|---|
| `static/*.html` only | no pytest — verify in a real browser instead |
| `<module>.py` | `python -m pytest -q tests/test_<module>.py` |
| `snipe_check.py` | + `tests/test_dashboard.py` |
| `speed_check.py` | `tests/test_speed_check.py` (covers the CLI, `/api/speed` and the Python/SQL parity check) |
| `db.py`, `auth.py`, `conftest.py` | full suite — wide blast radius |
| unsure / cross-cutting | full suite |

A local pass genuinely predicts CI: the root `conftest.py` forces
`DATABASE_URL=""` and `tests/conftest.py` hard-fails any test that reaches a
real database engine. Fix the test, never weaken that guard. Use `/ship` for the
full commit → push → watch-CI → confirm-deploy sequence.

## Human-only tasks (never attempt; ask and wait)

- Creating the Battle.net API client and filling `.env`.
- Creating the **Resend** account (done 2026-08-06). Its DNS records are *not*
  human-only after all: `realm-arbitrage.com` was **bought through Railway**, so
  Railway manages the zone and exposes a real record editor at
  `railway.com/workspace/domains/realm-arbitrage.com` (A/AAAA/ANAME/CNAME/MX/NS/
  SRV/TXT, priority field included). Name.com appears as the registrar in WHOIS
  because it's Railway's backend registrar only — there is no name.com account
  to log into, and looking for one wastes a lot of time.
- Creating the **Google Cloud OAuth client**, and enabling the **People API** on
  that project — `httpx-oauth`'s Google client reads the address from
  `people.googleapis.com/v1/people/me`, so login breaks at the final step
  without it. Leave the default scopes alone for the same reason.
- All in-game actions, including the verification protocol in `README.md`
  (posting, cancelling, expiring and buying test auctions) and reporting
  results.
- Any monetization/ToS decision.
- Rotating/swapping the live Stripe secret key, even a planned improvement like
  a restricted key.

## Definition of done

- The tests covering the change are green locally, and CI's full run is green
  before it counts as shipped.
- Any change to `market_key()`/`bonus_key()`/`relist_key()` has a matching
  SQL-macro update and a **real** (not invented) test vector — see
  `.claude/docs/matching.md`.
- Any frontend change verified in an actual browser, not just by reading the
  diff.
- `CLAUDE.md`/`README.md` updated for anything that changes current state; the
  relevant `.claude/docs/` file updated for the detail; narrative goes to
  `.claude/docs/history.md`.
