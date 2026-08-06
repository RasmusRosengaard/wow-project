# INTEGRATIONS — every external service this project depends on

One place for "what is this account for, what breaks without it, and where do I
change it". Written 2026-08-06 after an hour was lost to a wrong assumption about
which company administers the DNS.

**No secrets in this file, ever.** Values live in `.env` locally and in Railway's
Variables in production. Identifiers that are public by design (OAuth client id,
Railway project ids) are included because they save real time.

## At a glance

| Service | What it does | Breaks what if it fails | Costs |
|---|---|---|---|
| **Blizzard Battle.net API** | the auction data, the entire product | everything — no data at all | free |
| **Railway** | hosting, Postgres, volume, **DNS + registrar** | site down; email/DNS unmanageable | usage + $14/yr domain |
| **GitHub** | repo + CI, gates every deploy | can't ship (Railway waits for CI) | free (private repo) |
| **Stripe** | the €4.99/mo subscription | no new subscribers; existing ones keep access | ~2.9% + fee |
| **Resend** | verification + password-reset email | signups can't confirm → can't subscribe | free tier |
| **Google Cloud** | "Continue with Google" login | Google login only; email/password unaffected | free |
| **Discord** | Watchlist alert delivery (per-user webhooks) | alerts silently undelivered | free |
| **TSM public data** | region sale rates / liquidity filter | sale-rate columns and the standing rule | free, no account |
| **wago.tools** | appearance-rarity data (Phase 3 groundwork) | `appearance.py` features | free, no account |

---

## Blizzard Battle.net API

The one dependency with no substitute. `blizz.py` gets an OAuth token from
`oauth.battle.net`, then reads auction data from `eu.api.blizzard.com`.

- **Console:** https://develop.battle.net → API Access → Create Client
- **Credentials:** `BLIZZ_CLIENT_ID`, `BLIZZ_CLIENT_SECRET`, `BLIZZ_REGION` (`eu`)
- **Rate limit:** 36,000 req/h. Actual use is nowhere near it — ~6 req/h per
  deep-collected realm, ~100 dumps/h to sweep every EU connected realm. See
  `CLAUDE.md`'s rate-limit note before assuming a limit problem.
- **Human-only:** creating the client (`CLAUDE.md` "Human-only tasks").
- **ToS:** re-read the Developer API Terms before shipping anything
  monetization-adjacent. Non-negotiable, see `CLAUDE.md`'s guardrails.

## Railway — hosting, database, *and* DNS

Does more than people assume, which is where the confusion came from.

- **Dashboard:** https://railway.com/dashboard — workspace "Rasmus Rosengaard
  Nielsen's Projects", plan **Hobby**
- **Project:** `valiant-peace` — `9ac4cc7f-f1bc-432e-950b-63e4132e0df9`
- **Services:** `wow-project` (`92f776b9-3521-47a2-9ec8-744546193376`) +
  `Postgres` (`3f079799-237e-4d39-b45f-5541ce72ca76`), environment `production`
  (`b2412b23-6fad-4595-9f55-b14d95a0be46`)
- **Volumes:** `wow-project-volume` (parquet snapshots, DuckDB, collector
  cursors), `postgres-volume`
- **Deploy:** auto-deploys from GitHub `main` with **Wait for CI** on. There is
  no manual deploy step — a green-CI push *is* the deploy. `docker-entrypoint.sh`
  runs `alembic upgrade head` before serving, so migrations ride along.
- **CLI:** not on PATH. It lives in a Docker helper container; see
  `.claude/commands/railway-status.md` for the exact invocation
  (`MSYS_NO_PATHCONV=1 docker exec railway-cli-helper node …railway.js`).

### Domain + DNS ⚠️ read this before touching DNS

`realm-arbitrage.com` was **bought through Railway**, so **Railway manages the
DNS zone**. Records are edited at
`railway.com/workspace/domains/realm-arbitrage.com` — types A, AAAA, ANAME,
CNAME, **MX** (with priority), NS, SRV, TXT.

**WHOIS reports registrar "Name.com, Inc." — that is Railway's backend registrar
only. There is no name.com account for this domain.** Signing into name.com
shows zero domains and sends you hunting for an account that doesn't exist. The
one-command check for who actually administers it:

```
curl -I https://realm-arbitrage.com    # → Server: railway-hikari
```

Also true: a community report that Railway "doesn't support MX records" is **out
of date**. It does.

- Apex is an `ANAME` → `myywywn9.up.railway.app`, plus a `TXT _railway-verify`.
  Leave both alone.
- **Auto-renew is OFF** → lapses **2027-07-26**. $14/yr.
- Transfer-locked until **2026-09-24** (ICANN's 60-day post-registration hold).

## GitHub

- **Repo:** `RasmusRosengaard/wow-project` (private)
- **CI:** `.github/workflows` — single `test` job, Ubuntu, Python 3.12,
  `pip install -r requirements.txt` then `pytest -q`. Runs on push and PR to
  `main`.
- **Branch protection:** requires the `test` status check. Pushing before CI
  passes reports "Bypassed rule violations" — CI still runs and still gates
  Railway, so the deploy waits regardless.
- **CLI:** `gh` is on PATH and authenticated as `RasmusRosengaard`.
- CI is the real full-suite gate. Locally run only the tests covering your
  change — see `CLAUDE.md`'s testing table.

## Stripe

**Live mode from day one** — deployed straight to live with no test-mode pass
(human decision, 2026-07-23). Treat early live transactions as the verification.

- **Product:** one €4.99/month subscription
- **Credentials:** `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`, `STRIPE_WEBHOOK_SECRET`
- **Not used by any code**, despite being in `.env.example`:
  `STRIPE_PUBLISHABLE_KEY`, `STRIPE_PRODUCT_ID`. Nothing reads them.
- **Webhook** listens for exactly three events, and is the **only** writer of a
  user's subscription fields: `checkout.session.completed`,
  `customer.subscription.updated`, `customer.subscription.deleted`
- **Portal:** Stripe's hosted Customer Portal handles cancel/update/invoices.
  Requires `stripe_customer_id` — which a comped account
  (`grant_free_month.py`) never has, the cause of a real UI bug fixed 2026-08-06.
- ⚠️ **Never rotate or swap the live secret key without the human present**, even
  under instructions to keep working autonomously. Still on the full
  `sk_live_...` key; swapping it for a restricted one is an open, human-only task.
- ⚠️ **Tests must never reach live Stripe.** `billing.py` reads the key at import
  time, so a dev `.env` puts the *live* key in scope. `tests/test_billing.py`
  patches in a fake `sk_test_` key; `tests/test_auth.py` clears the key entirely.
  A test once created a real Checkout Session this way.

## Resend — transactional email

- **Console:** https://resend.com — account `rasmusrosen2001@gmail.com`
- **Domain:** `realm-arbitrage.com`, region **Ireland (eu-west-1)**, id
  `d2153cac-8f29-4166-966b-8328b1b84ea9`
- **Credentials:** `RESEND_API_KEY`, `MAIL_FROM`
- **Free tier:** 3,000 emails/month, **100/day**, 1 domain, 30-day log retention
- **Sender:** `mailer.py`, one authenticated POST via the existing `httpx`. No
  new dependency.
- **Records** (all in Railway's DNS editor): DKIM `TXT resend._domainkey`,
  SPF `MX send` → `feedback-smtp.eu-west-1.amazonses.com` (priority 10),
  SPF `TXT send`, DMARC `TXT _dmarc`.
- **Status reads "Partially Verified" and that is correct.** DKIM + both SPF
  records are Verified; the outstanding one is an apex `MX` for Resend's
  *inbound receiving*, which this app doesn't use. **Don't add it** — an apex MX
  decides where all mail for the domain goes. Turning "Enable Receiving" off in
  Resend makes the status read fully Verified.
- **Unconfigured is a supported state:** with no `RESEND_API_KEY`, `mailer.send()`
  logs the message (link included) and returns. That's how local dev completes a
  signup and how CI stays hermetic. It also never raises, so an outage can't turn
  a created account into a 500.
- ⚠️ **No rate limiting** on registration or `/auth/request-verify-token`, so the
  100/day quota is burnable by anyone. Known gap, deliberately unfixed —
  thresholds are human-specified in this project.

## Google Cloud — "Continue with Google"

- **Console:** https://console.cloud.google.com — project **`realm-arbitrage`**,
  owner `rasmusrosen2001@gmail.com`
- **Credentials:** `GOOGLE_OAUTH_CLIENT_ID`
  (`777528193354-8jslrlquk0fb3l38pbp3gd8fcajk464u.apps.googleusercontent.com` —
  public by design, it travels in the authorization URL),
  `GOOGLE_OAUTH_CLIENT_SECRET` (confidential)
- **Consent screen:** External, **published / In production**. While it's in
  *Testing* only accounts added as test users can sign in at all — that looks
  exactly like "Google login is broken".
- **Scopes:** `userinfo.profile` + `userinfo.email`. Both non-sensitive, so
  publishing needs no Google review.
- ⚠️ **The People API must stay enabled.** `httpx-oauth`'s Google client resolves
  the address via `people.googleapis.com/v1/people/me`, **not** the OIDC userinfo
  endpoint. Without it, login fails at its final hop with `SERVICE_DISABLED`.
  For the same reason, **do not narrow the scopes** — `userinfo.profile` is
  required by that call.
- **Redirect URIs** must match byte for byte:
  `https://realm-arbitrage.com/auth/google/callback` and
  `http://127.0.0.1:8000/auth/google/callback`
- **Unconfigured is a supported state:** with the two vars unset, the
  `/auth/google/*` routes aren't mounted and the button is hidden.
  `GET /api/auth-config` reports `{google, email}` so the frontend knows.

## Discord

Not an account we own — **each user pastes their own webhook URL**
(`User.discord_webhook_url`), no OAuth. `watchlist.py` POSTs alerts to it. An
account with no webhook still has its triggers tracked, just silently. Chosen as
the cheapest real delivery mechanism over email (new infra at the time) or
in-app-only (passive).

## Public data, no account needed

| Source | URL | Used by |
|---|---|---|
| TSM public data | `public-data.tradeskillmaster.com/retail/eu/region/items.csv` | `tsm.py` — region sale rates, liquidity filter, standing sniper rule |
| wago.tools | `wago.tools/db2/ItemModifiedAppearance/csv` | `appearance.py` — appearance rarity |

No credentials, no rate-limit agreement, no dashboard. Both are unauthenticated
CSV fetches — which also means nobody owes us uptime.

## Link targets, *not* integrations

`wowhead.com` and `undermine.exchange` appear in the code but are **never
fetched** — they're URLs printed for humans (`analyze.py`, `snipe_check.py`) or
built for the dashboard's per-row link. No dependency, nothing to configure.

---

## Environment variables — one table

| Variable | Service | Secret? | Notes |
|---|---|---|---|
| `BLIZZ_CLIENT_ID` / `BLIZZ_CLIENT_SECRET` | Blizzard | secret | required for any data |
| `BLIZZ_REGION` | Blizzard | no | `eu` |
| `DATABASE_URL` | Railway Postgres | secret | `postgresql+asyncpg://…`; tests force `""` |
| `SECRET` | app | secret | signs session cookies + verify/reset tokens |
| `COOKIE_SECURE` | app | no | `false` for local http, unset in production |
| `ENABLE_BACKGROUND_COLLECTION` | app | no | `true` only on Railway |
| `PUBLIC_BASE_URL` | app | no | email links + Google redirect_uri |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Stripe | secret | **live** key |
| `STRIPE_PRICE_ID` | Stripe | no | |
| `RESEND_API_KEY` | Resend | secret | unset ⇒ log instead of send |
| `MAIL_FROM` | Resend | no | must be on the verified domain |
| `GOOGLE_OAUTH_CLIENT_ID` | Google | no | public by design |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google | secret | |

Set locally in `.env` (see `.env.example`), in production under Railway →
`wow-project` → Variables. Railway **stages** variable changes — they do nothing
until you press **Deploy**.

## Dates to watch

| When | What |
|---|---|
| **2026-09-24** | domain becomes transfer-eligible (ICANN 60-day hold ends) |
| **2027-07-26** | `realm-arbitrage.com` expires — **auto-renew is OFF** |

## Human-only, never automate

- Creating the Blizzard API client.
- Rotating/swapping the live Stripe secret key.
- Any monetization or ToS decision.
- Accepting third-party terms (e.g. Google's User Data Policy checkbox).
- Pasting any API key or secret into a field — that's a hard line for Claude,
  including into Railway's own Variables UI.
