---
description: Check the live Railway deploy status, CI gate, and data-volume usage for wow-project
---

Report the current state of the hosted app in 3 short lines: deploy status,
latest CI run, and volume usage vs cap. Steps:

1. Make sure the Railway CLI helper container is running: `docker ps --filter
   name=railway-cli-helper --format '{{.Names}}: {{.Status}}'`. If it's not
   running, start it (see CLAUDE.md's "CLI tooling note" for how it was set
   up: `node:20-slim` image, `npm install -g @railway/cli`, an SSH keypair
   registered via `railway ssh keys add`) or tell the user Docker Desktop
   needs to be started first (`Start-Process "C:\Program Files\Docker\Docker\
   Docker Desktop.exe"`, wait ~10-20s for the daemon).
2. Run:
   ```
   MSYS_NO_PATHCONV=1 docker exec railway-cli-helper node /usr/local/lib/node_modules/@railway/cli/bin/railway.js status
   ```
   The `MSYS_NO_PATHCONV=1` prefix and calling `bin/railway.js` directly via
   `node` (not the `railway` bin) are both required in this environment —
   see CLAUDE.md's "Hosted deployment (Railway)" section for why.
3. Run `gh run list --branch main --limit 1` to see the latest CI run's
   conclusion.
4. Summarize: is `wow-project` Online (not stuck Building/Deploying/Crashed)?
   Did the latest CI run pass? What's the volume usage
   (`wow-project-volume · X GB / 4.9 GB`) — flag it if it's trending close to
   the cap (see CLAUDE.md's "Disk usage / retention" section: current
   `RETENTION_DAYS = 14` was projected to exceed the cap at full history as
   of 2026-07-25, and an adaptive-retention fix was proposed but not built).

Do not take any destructive or write action (no restart, no rollback, no env
var changes) — this command is read-only status reporting. If something
looks wrong, report it and ask before acting.
