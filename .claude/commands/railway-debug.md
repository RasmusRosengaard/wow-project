---
description: Run a read-only command against live production data via railway ssh
argument-hint: <shell command to run inside /app on the live container, e.g. "python analyze.py --cr-id 1403 trace 164353">
---

Run this command on the live `wow-project` container and report the output:

```
MSYS_NO_PATHCONV=1 docker exec railway-cli-helper node /usr/local/lib/node_modules/@railway/cli/bin/railway.js ssh -- "cd /app && $ARGUMENTS"
```

Context: `railway ssh` gives a real shell on the deployed container with
access to the production `data/` Volume — unlike `railway run` (which
executes *locally* with Railway env vars injected, useful for hitting remote
Postgres but never reaches the Volume). This is how every real production
bug this project has hit was actually confirmed (see CLAUDE.md's "Remote
debugging note" and the various "traced live via `railway ssh`" writeups)
rather than guessed at from local synthetic data.

If `$ARGUMENTS` is empty, ask the user what they want to run instead of
guessing — this executes directly against production.

**Guardrails**:
- Treat this as read-only unless the user has explicitly asked for a write
  (e.g. `--refresh` on `appearance.py`, or anything touching `data/state/`).
  Prefer `analyze.py`/`snipe_check.py`/one-off read-only `python -c` snippets.
- Never pass secrets or modify `.env`/environment variables through this.
- If the container isn't running or the SSH key isn't registered yet, see
  CLAUDE.md's "Remote debugging note" for the one-time setup
  (`ssh-keygen`, `railway ssh keys add -k <path>.pub`,
  `StrictHostKeyChecking accept-new` in `~/.ssh/config`) rather than
  improvising a workaround.
