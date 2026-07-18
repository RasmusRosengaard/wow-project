# CLAUDE.md — WoW AH Snipe Validator (EU retail)

Agent-facing project brief. `README.md` is the human-facing version — keep both in
sync when architecture or commands change. Handed off from a Claude.ai planning
session on 2026-07-18.

## What this project is

A **validation layer for WoW auction-house sniping**, not another sniper.
Existing tools (TSM Sniper, Auctionator) flag items cheap relative to *listing*
prices — which are fiction. The unsolved half is validation: does this item
actually sell, at what real prices, how fast, and is its transmog appearance
genuinely rare? We infer **actual sales** from Blizzard's hourly AH snapshots and
(later) layer appearance scarcity on top, producing a Deal Score.

Business model (decided, don't revisit without the human): free in-game addon
(Blizzard requires addons to be free) + paid external data service — Discord
alerts, dashboards, custom filters. The TSM / Raider.IO pattern. Competitors: TSM
(coarse regional sale rates, hostile UX), Saddlebag Exchange (alerts). Our edge:
validated sell-realm liquidity + cross-realm snipe routing + appearance-level
intelligence.

## Market structure (Warbands — decided 2026-07-18, core to the product)

Since The War Within, the warband bank makes **gold account-wide** and lets
**unsoulbound BoE items move between a player's characters on any realm**. The
gear/transmog AH is still per connected realm (separate listings, buyer pools,
prices) — so cheap listings rot on low-pop realms while hubs pay full price.
That asymmetry IS the product:

- The user picks **sell realms** (their high/full-pop hub realms). We run full
  snapshot-diff sale inference there → real sold-price percentiles + sales/day.
- A lightweight **region scanner** sweeps every other EU connected realm's
  current listings (no sale inference needed on the buy side).
- **Validated snipe** = a listing on any realm priced well below the sell
  realm's sold-price percentile for that item/variant, on an item that is
  liquid there — net of the 5% AH cut and transfer friction.
- Caveat to model later: not every item survives the warband-bank route
  (warbound / BoP); the appearance layer needs a per-item transferability flag.

Rate-limit math holds: deep collection ~6 req/h per sell realm; scanning all
~100 EU connected realms hourly is ~100 dumps/h against a 36,000 req/h limit.
Buy-side scans don't need snapshot history — latest listings per realm suffice.

**Current phase: 0 — prove the sale-inference signal is real before building more.**

## Non-negotiable guardrails

- **Decision support only.** Never write code that automates in-game actions: no
  auction posting/buying automation, no input simulation, no game-client memory
  reading, no packet interception. ToS compliance is a product requirement, not a
  preference. If a task drifts that way, stop and flag it.
- The future in-game addon must be **free**; monetization lives in the external
  service only.
- Before any payment/monetization feature ships, the human must re-read the
  current Blizzard Developer API Terms of Use. Do not ship payments autonomously.
- Secrets live in `.env`, never in code, never committed.
- No "WoW"/"Warcraft" in product branding; "for World of Warcraft" as a
  description is the accepted form.

## Current state (updated 2026-07-18, Phase 0 tasks 1–3 done)

| File | Purpose |
|---|---|
| `blizz.py` | `.env` loader, OAuth client-credentials token, `api_get()`, realm-slug → connected-realm-id lookup |
| `fetch_snapshot.py` | Collector CLI: polls one connected realm, writes hourly parquet snapshots (If-Modified-Since aware); logs to console + rotating `data/logs/collector.log`, backs off on 429/5xx, skips malformed-JSON bodies |
| `tests/test_diff.py` | pytest suite for `classify_pair`: all five classifications, oversized-gap downgrade, relist-consumption edges (`pytest -q`; root `conftest.py` makes top-level modules importable) |
| `diff_snapshots.py` | **Core IP.** Diffs consecutive snapshots, classifies every vanished auction, writes events parquet |
| `analyze.py` | DuckDB CLI: liquidity summary + per-item sold-price distribution / percentile check |
| `requirements.txt` | `requests`, `pyarrow`, `duckdb` (Python 3.10+) |
| `.env.example` | `BLIZZ_CLIENT_ID`, `BLIZZ_CLIENT_SECRET`, `BLIZZ_REGION=eu` |

Verified: `pytest -q` green (11 tests covering all five classifications and the
edge cases in Phase 0 task 2). **Never run against the live API yet** — needs
the human's credentials and a 48h collection window.
Not yet present: retention, CI. Next up: task 4 (blocked on the human's 48h
run + in-game verification protocol).

## Architecture & data layout

```
Blizzard API ──> data/snapshots/{cr_id}/{epoch_ts}.parquet   (immutable, hourly)
                        │ diff consecutive pairs
                        v
                 data/events/{cr_id}.parquet   (derived; recomputed from scratch
                        │                       each run — always safe to delete)
                        v
                 analyze.py DuckDB views (snaps, ev, sales, span)
data/state/{cr_id}.json  — Last-Modified cursor for the collector
```

Snapshot schema is `SCHEMA` in `fetch_snapshot.py`; event schema is
`EVENT_SCHEMA` in `diff_snapshots.py`. Changing either must handle previously
written files (regenerate, or read with `union_by_name`) — globs assume uniform
schema.

## Blizzard API facts (trust these, don't guess)

- OAuth: `POST https://oauth.battle.net/token`, HTTP basic auth with client
  id/secret, `grant_type=client_credentials`. Token lasts ~24h; cached in-process.
- Base `https://eu.api.blizzard.com`; namespace param `dynamic-eu` (auctions,
  realms) or `static-eu` (items, appearances, media).
- Non-commodity AH: `GET /data/wow/connected-realm/{crId}/auctions`. Updates
  roughly hourly. Honor `If-Modified-Since` / `Last-Modified` (implemented; the
  Last-Modified timestamp is the canonical `snapshot_ts`).
- Commodities (region-wide, Phase 2): `GET /data/wow/auctions/commodities`.
  **Different physics:** commodity auctions can be partially bought — the same
  `auction_id` persists with reduced `quantity`. Inference there = quantity
  deltas on surviving ids + disappearances, NOT the gear logic below.
- Realm lookup: `GET /data/wow/search/connected-realm?realms.slug={slug}`.
- Rate limit 36,000 req/h, 100 req/s. Collector uses ~6/h. Headroom is not an
  invitation — stay polite.
- `time_left` buckets: SHORT <30m, MEDIUM 30m–2h, LONG 2–12h, VERY_LONG 12–48h.
  Players list at 12/24/48h durations.
- Prices are **copper** (10,000 = 1 gold) end to end; only format as gold at
  display boundaries.
- Battle pets: item_id 82800 cages + `pet_species_id` / `pet_quality_id` /
  `pet_level` fields.
- `auction_id` is stable for a listing's lifetime → it is the diff key. Seller
  identity is never exposed by the API.

## Inference logic (change only with tests proving equivalence or improvement)

For each auction present in snapshot N but missing in N+1, `classify_pair()`:

1. `buyout IS NULL` → `bid_only_gone` (can't be insta-bought; excluded).
2. `time_left == SHORT` → `likely_expired`.
3. Identical `(item_id, bonus_key, buyout, quantity)` appears among *brand-new*
   auction ids in N+1 → `likely_relisted` (consumed from a Counter, so two
   identical listings need two relists).
4. `gap_seconds >= MIN_REMAINING[time_left]` → `ambiguous` (could have expired;
   also absorbs collector-downtime gaps).
5. Else → `inferred_sale`.

`bonus_key` canonicalizes `bonus_lists` + `modifiers` so identical gear variants
compare equal. **Known blind spot:** a cancel *without* relist is
indistinguishable from a sale; the README verification protocol measures that
noise floor empirically. Ideas to shrink it later (seller-behavior priors,
multi-interval relist windows) belong after Phase 0, not during.

## Conventions

- Python 3.10+, stdlib `argparse` CLIs, minimal deps. No pandas, no ORM, no web
  framework until the dashboard phase. DuckDB does the analytics.
- Small modules, pure functions where possible (`classify_pair`, `bonus_key`,
  `rows` are deliberately pure — keep them testable).
- Derived data (`data/events/`) is always recomputed from scratch; never make it
  incrementally stateful without also keeping the idempotent path.
- Collector loop must survive any exception (it guards a multi-day run).
- Update this file and `README.md` whenever commands, schemas, or architecture
  change.

## Commands

```
python fetch_snapshot.py --find silvermoon          # realm slug -> cr-id
python fetch_snapshot.py --cr-id 1096 --loop        # collect (48h+)
python diff_snapshots.py --cr-id 1096               # build events
python analyze.py --cr-id 1096 summary --top 30
python analyze.py --cr-id 1096 item 152510 --price 2500000   # copper
```

## Human-only tasks (never attempt; ask and wait)

- Creating the Battle.net API client and filling `.env`.
- Keeping the collector running on their machine for the 48h window.
- All in-game actions, including the verification protocol in `README.md`
  (posting, cancelling, expiring, and buying test auctions) and reporting results.
- Any monetization/ToS decision.

## Roadmap — execute top to bottom, don't skip ahead

### Phase 0 — validate the signal (NOW)
1. `git init`; add `.gitignore` (`.env`, `data/`, `__pycache__/`, `.venv/`,
   `*.pyc`); initial commit.
2. Formalize the synthetic fixture into `tests/test_diff.py` (pytest). Two
   snapshots, gap 3600s, expected results exactly:
   - VERY_LONG with buyout vanishes, no relist → `inferred_sale`
   - LONG vanishes, identical listing reappears under new auction_id → `likely_relisted`
   - SHORT vanishes → `likely_expired`
   - MEDIUM vanishes (3600 ≥ 1800) → `ambiguous`
   - bid-only VERY_LONG vanishes → `bid_only_gone`
   - a surviving auction produces no event
   Add edge cases: oversized gap downgrades LONG/VERY_LONG to `ambiguous`; two
   identical vanishing listings + one relist → one `likely_relisted` + one
   `inferred_sale`.
3. Robustness pass on the collector: std-lib `logging` to console + rotating
   file, backoff on 429/5xx, catch malformed-JSON responses. Keep it small.
4. After the human's 48h run and in-game protocol: analyze their reported
   results, write `VALIDATION.md` — observed false-positive class, relist-catch
   success, per_day vs TSM regional for 3 liquid items.
   **Gate: only proceed to Phase 1 if liquid-item per_day lands within ~2x of
   TSM and the relist heuristic caught the test repost.**

### Phase 1 — cross-realm engine + hardening
The Warbands market structure (see section above) drives this phase:
- Config split: **sell realms** (deep hourly snapshots + sale inference, as
  today) vs **scan realms** (everything else in the region).
- Region scanner: sweep all other EU connected realms, keep only the *latest*
  listings per realm (`data/listings/{cr_id}.parquet`, overwritten each sweep —
  no history, no diffing on the buy side).
- First snipe-check CLI: join scan-realm listings against sell-realm sold-price
  percentiles + sales/day; flag listings below a discount threshold net of the
  5% AH cut. This is the end-to-end "validated snipe" proof.
- Hardening carried over: retention on sell-realm snapshots (compact >7 days
  into daily per-item aggregates before deleting), systemd unit / Windows Task
  Scheduler examples in README, `--since` flag for incremental diffing if event
  rebuilds get slow.

### Phase 2 — commodities feed
Region-wide collector + quantity-delta inference (see API facts). Separate
schema; do not force gear and commodities into one table.

### Phase 3 — appearance layer
itemId → appearanceId via wago.tools DB2 exports (`ItemModifiedAppearance`),
cached locally; static API as fallback. Per appearance: count of source items,
obtainability flag (manual curation acceptable at first), **warband
transferability flag** (warbound / BoP items can't ride the warband bank to a
sell realm), region-wide AH scarcity. This is the differentiator — design the
schema carefully.

### Phase 4 — deal score + Discord alerts (first paid feature)
Score = f(discount vs the *sell realm's* sold-price percentile net of 5% AH
cut, sell-realm sales_per_day, appearance scarcity), attached to a route: buy
realm → sell realm. Webhook alert engine with per-user config (their sell
realms + watchlist). Payments require the human's explicit ToS sign-off first.

### Phase 5 — free companion addon + web dashboard
Addon overlays Deal Score on tooltips/sniper results (free, per Blizzard
policy); dashboard is the premium surface.

## Definition of done for the current milestone

- pytest suite green in CI-less local runs (`pytest -q`).
- 48h of live snapshots collected without collector death.
- `VALIDATION.md` written with real numbers and a clear go/no-go call.
