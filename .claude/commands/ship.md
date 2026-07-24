---
description: Test (incl. CI-matching env), commit, push, watch CI, and confirm the Railway deploy landed
---

This project's actual deploy flow (see CLAUDE.md's "Scheduled automation"
and README's "The deploy flow"): local code change → CI → Railway "Wait for
CI" → auto-deploy + migration. There is no manual deploy step — pushing to
`main` with green CI *is* the deploy. Run the full sequence:

1. `git status` — confirm what's actually changing. Never blind-commit;
   review the diff first.
2. `python -m pytest -q` — full suite must pass.
3. `env -u DATABASE_URL python -m pytest -q` (Bash) — re-run matching CI's
   actual environment. **This step exists because of a real incident**: a
   local pass masked a missing test-dependency override (local `.env` had
   `DATABASE_URL` set, CI didn't), and 17 tests failed in CI despite a clean
   local run. Do not skip this if anything touched a FastAPI route's
   dependencies, `db.py`, or `auth.py`.
4. Stage the specific files that changed (never `git add -A` blindly — check
   `git status` after staging for anything that looks like a secret or an
   unintended file), commit with a message explaining *why*, not just what.
5. `git push origin main`.
6. `gh run list --branch main --limit 1` then `gh run watch <id>
   --exit-status` — wait for CI green. Don't consider the push "done" until
   this actually confirms green, not just "looked fine locally."
7. Once CI is green, Railway's "Wait for CI" gate will pick it up
   automatically — use `/railway-status` (or the equivalent docker/railway
   CLI call) to confirm the deploy actually reached `Online`, not stuck
   `Building`/`Deploying`/crashed.
8. If the change is worth live-verifying against production data (a bug
   fix, a data-pipeline change), use `/railway-debug` to check it against
   real data post-deploy — don't just trust the diff.

Stop and report back (don't self-resolve) if: tests fail, CI goes red, or
the Railway deploy doesn't reach Online within a couple of minutes of CI
going green.
