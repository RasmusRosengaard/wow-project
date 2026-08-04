# Roadmap and process deviations

The intended build order and every place reality deliberately departed from
it. Each deviation was a human decision, not drift.

## Roadmap — execute top to bottom, don't skip ahead


(See "Process deviations" above for where this hasn't happened literally,
and why each skip was a deliberate call.)

### Phase 0 — validate the signal
**Gated, skipped** (2026-07-20). Formalized the synthetic fixture into
`tests/test_diff.py` (done). The 48h collection + in-game verification
protocol + `VALIDATION.md` write-up were never run — see `README.md`'s
"Verification protocol" for what running it would involve.

### Phase 1 — cross-realm engine + hardening
**Mostly done.** Region scanner and snipe-check CLI both shipped and are
the end-to-end product. Remaining: sell/scan realm config file (manual
`--exclude`/`--items` flags stand in), `--since` incremental diffing if
event rebuilds get slow.

### Phase 2 — commodities feed
**Out of scope** (human decision, 2026-07-24) — not being pursued.

### Phase 3 — appearance layer
**Started ahead of Phase 1's remaining hardening** (2026-07-23). Done:
itemId → appearanceId mapping + source-item count (`appearance.py`), wired
into `snipe_check.py`/`dashboard.py` as a "unique transmog" filter. Not
done: static-API fallback, real obtainability flags (`source_count` is a
rarity proxy, not a farmability check — known to diverge from Wowhead's own
"same model as" data on at least one item), region-wide AH scarcity *of
currently listed* appearances. The originally planned "warband
transferability flag" is **closed, no flag will be built** — see "Market
structure" above.

### Phase 4 — deal score + Discord alerts (first paid feature)
Not started — blocked on Phase 3 data. Score = f(discount vs current
cheapest listing, appearance scarcity), attached to a route: buy realm →
sell realm. Payments require the human's explicit ToS sign-off first
(already read once, 2026-07-23 — re-check before turning on any *new*
billing surface if time has passed).

### Phase 5 — free companion addon + web dashboard
**The dashboard half is done** (pulled forward 2026-07-23 — hosted,
multi-tenant, auth, live Stripe subscriptions, free tier, all shipped).
Only the free in-game addon itself remains, not started.


## Process deviations from the roadmap (all human decisions, not silent drift)


The roadmap below says "execute top to bottom." That hasn't happened
literally — each skip-ahead was a deliberate, human-directed call, not
drift, and none of them reversed a guardrail. Summary (full reasoning for
each is in `history.md` if you need it):

- **Phase 0's 48h validation gate was skipped** (2026-07-20) to build ahead;
  risk accepted indefinitely unless the human decides to run the
  verification protocol later. The classification engine (`diff_snapshots.py`)
  remains unvalidated against real seller behavior.
- **Phase 5's dashboard was pulled forward** (2026-07-23) ahead of Phase 3;
  viable because the dashboard surfaces the same `snipe_check.CAVEAT` the
  CLI already prints, no new transferability guarantees needed.
- **The hosted multi-tenant pivot happened in one day** (2026-07-23):
  auth, live Stripe billing (skipping test-mode verification), scoped
  server-side collection, and CD all shipped same-day — a higher-risk-
  tolerance path than the roadmap implied, deliberate and human-directed.
- **Phase 3 groundwork (appearance rarity) started ahead of Phase 1's
  remaining hardening** (2026-07-23) — human asked for it directly.
- **Phase 2 (commodities feed) is explicitly out of scope** (2026-07-24),
  not just deprioritized — no current intent to build it.
- **The sold-price-percentile pricing model was replaced** (2026-07-25) —
  see "What this project is" above; this is a product-shape change, not a
  roadmap skip, but is the single biggest deviation from the original
  design in the project's history.
- **Bonus/ilvl-aware matching (`market_key()`) was dropped from live
  pricing** (2026-07-26) — see "What this project is" above's matching-
  model note. A human product decision (matching should be pure `item_id`,
  full pooling, lowest price wins, ilvl/bonus differences display-only)
  made after a live-traced bug in the noise-detection heuristic that
  matching depended on. Simplifies `snipe_check.py` substantially (four
  helper functions and a temp-table join removed); `market_key()` itself
  remains real code, still used by `diff_snapshots.py`'s relist detection
  and `analyze.py`'s manual debugging tool.

Not yet present: sell/scan realm config split (manual `--exclude`/`--items`
flags stand in), `--since` incremental diffing, `VALIDATION.md`.
