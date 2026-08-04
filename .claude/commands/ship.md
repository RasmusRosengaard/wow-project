---
description: Test (incl. CI-matching env), commit, push, watch CI, and confirm the Railway deploy landed
---

This project's actual deploy flow (see CLAUDE.md's "Scheduled automation"
and README's "The deploy flow"): local code change → CI → Railway "Wait for
CI" → auto-deploy + migration. There is no manual deploy step — pushing to
`main` with green CI *is* the deploy. Run the full sequence:

1. `git status` — confirm what's actually changing. Never blind-commit;
   review the diff first.
2. Run **only the tests covering the change** — CI runs the full suite on
   every push and Railway won't deploy without it, so re-running all 441
   locally just duplicates that (changed 2026-08-04):

   | Changed | Run |
   |---|---|
   | `static/*.html` only | no pytest — browser-verify instead |
   | `<module>.py` | `python -m pytest -q tests/test_<module>.py` |
   | `snipe_check.py` | + `tests/test_dashboard.py` (the API surfaces its rows) |
   | `db.py`, `auth.py`, `conftest.py` | full suite — wide blast radius |
   | unsure / cross-cutting | full suite |

   A local pass now genuinely predicts CI: `conftest.py` forces
   `DATABASE_URL=""` and `tests/conftest.py` hard-fails any test that reaches
   a real engine, so the old "green locally, red in CI" asymmetry is gone.
   (The previous step 3 here, `env -u DATABASE_URL python -m pytest -q`, was
   retired — it had become a silent no-op, see HISTORY.md 2026-08-04.)
3. Stage the specific files that changed (never `git add -A` blindly — check
   `git status` after staging for anything that looks like a secret or an
   unintended file), commit with a message explaining *why*, not just what.
4. `git push origin main`.
5. `gh run list --branch main --limit 1` then `gh run watch <id>
   --exit-status` — wait for CI green. This is the real full-suite gate now,
   so actually wait for it; don't consider the push "done" until it confirms
   green.
6. Once CI is green, Railway's "Wait for CI" gate will pick it up
   automatically — use `/railway-status` (or the equivalent docker/railway
   CLI call) to confirm the deploy actually reached `Online`, not stuck
   `Building`/`Deploying`/crashed.
7. If the change is worth live-verifying against production data (a bug
   fix, a data-pipeline change), use `/railway-debug` to check it against
   real data post-deploy — don't just trust the diff.

Stop and report back (don't self-resolve) if: tests fail, CI goes red, or
the Railway deploy doesn't reach Online within a couple of minutes of CI
going green.
