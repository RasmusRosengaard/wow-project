---
name: project-review
description: Repo-specific pre-push review checklist for wow-project — the traps that have actually bitten this project before, not a generic code-review pass. Use before shipping any change to fetch_snapshot.py, snipe_check.py, analyze.py, diff_snapshots.py, dashboard.py, or anything touching money/copper values or Blizzard API assumptions.
---

Review the current diff (`git diff` / `git diff --staged`) against this
checklist. This is a *supplement* to normal code review, not a replacement —
it exists because every item below has caused a real bug or a real incident
in this project already (see `CLAUDE.md` for the full writeups). Report
findings the same way any other review would; don't silently fix things the
user didn't ask you to touch.

## Non-negotiable guardrails (CLAUDE.md — check first, every time)

- No code that automates in-game actions (posting/buying/input simulation/
  memory reading/packet interception). This is a hard stop, not a judgment
  call — if a change drifts this way, flag it immediately rather than
  finishing it.
- No secrets in code or commits. If a diff touches `.env`-adjacent code,
  actually read the values being staged, not just the filenames.
- No payment/monetization feature ships without the human re-reading
  Blizzard's API Terms of Use first, and no rotating/touching the live
  Stripe secret key without the human present — even mid-autonomous-work.

## Matching-logic changes (market_key / bonus_key / relist_key)

If the diff touches `fetch_snapshot.market_key()`, `bonus_key()`, or
`diff_snapshots.relist_key()`:

- [ ] Is there a matching change to `analyze.MARKET_KEY_MACRO_SQL`? These are
      two independent implementations (Python + DuckDB SQL, no shared UDF —
      this duckdb version's Python UDF support needs numpy) that must stay in
      parity by hand.
- [ ] Does `tests/test_market_key.py`'s parity test cover the new behavior
      with a *real* vector, not just a synthetic one? This project's
      convention is to trace the actual bug live (`railway ssh` +
      `analyze.py trace`/custom DuckDB queries) before writing the fix, and
      encode the real observed bonus_key/modifier values as test vectors —
      guessing at plausible-looking values has produced false confidence
      before.
- [ ] Any newly-ignored modifier type: is it unconditional (like 9/42/44) or
      does it need a per-item conditional check (like type 28's ilvl
      plausibility)? Don't assume a new undocumented modifier is safe to pool
      globally without either strong corroborating evidence from real data
      (e.g. identical troll price across different values) or explicit human
      confirmation it doesn't affect the thing it might affect (transmog
      appearance, in the type-9 case).
- [ ] `bonus_key` itself must stay pure and unchanged in what it stores/
      displays — matching-key changes should only affect what a query
      *groups by*, never what's persisted or shown to the user.

## Money / prices

- [ ] Copper end-to-end: prices are stored and computed in copper; gold
      formatting only happens at display boundaries (CLI print statements,
      dashboard formatting functions). A raw `/ 10000` in the wrong place is
      an easy silent bug — check units at every arithmetic step touching a
      price field, not just the final output.
- [ ] If a new price-derived field is added to `find_snipes()`'s output or
      `/api/snipes`, does it need both a `_g` (display) and `_copper` (raw)
      variant, matching the existing `sell_now_g`/`sell_now_copper` pattern?

## Tests

- [ ] The tests covering the change pass (`tests/test_<module>.py`; full suite
      for `db.py`/`auth.py`/`conftest.py` or anything cross-cutting). CI runs
      all 441 on push and gates the deploy — see `/ship` step 2.
- [ ] If the diff adds or changes a FastAPI route dependency (anything in
      `dashboard.py`, `auth.py`, `db.py`): does the touching test file point
      that seam at throwaway SQLite? `tests/conftest.py`'s `no_real_database`
      guard fails loudly if not. Fix the test, never weaken the guard — it is
      what stops the "green locally, red in CI" asymmetry that cost 17 red CI
      tests on 2026-08-01 (see .claude/docs/history.md).
- [ ] New behavior traced from a real production bug: is there a regression
      test using the *actual* observed values (item id, bonus_key, base
      level, price), not invented-but-plausible ones?

## Docs

- [ ] Does this change need a `CLAUDE.md` update (architecture, schema,
      commands, a new non-obvious decision)? Does it need a `.claude/docs/progress.md`
      entry (feature shipped, phase status changed)? This project updates
      both throughout a session, not batched at the very end — don't leave
      it as a TODO.

## Frontend changes (dashboard.html and friends)

- [ ] Verified in an actual browser, not just by reading the diff — this
      project's established technique: copy the target HTML into the
      scratchpad dir, inject a `window.fetch` mock returning canned JSON (so
      the real, unmodified frontend code runs), serve via
      `python -m http.server`, drive it with the `claude-in-chrome` tools,
      inspect via `javascript_tool`. Never commit the throwaway copy.
- [ ] If it's a paid-tier/free-tier or auth-visible change: was the actual
      gating behavior (not just the visual layout) verified — e.g. a
      free-tier account doesn't silently get more access than intended, a
      locked field can't be bypassed client-side only?
