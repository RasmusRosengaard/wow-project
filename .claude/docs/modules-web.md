# Web application modules

The FastAPI layer, auth, billing and the relational (users/subscriptions) data
model. Read-only over the pipeline above.

## `dashboard.py`

FastAPI app, read-only web layer over `snipe_check.find_snipes()`. `GET
/api/snipes` mirrors the CLI's flags as query params and returns JSON rows via
`_row_to_json()`: prices in both gold and raw copper
(`buy_copper`/`sell_copper`), `pet_species_id`/`pet_quality_id`,
`region_median_g`/`_copper`,
`region_sale_rate`/`region_sold_per_day`/`region_sale_avg_copper`,
`price_suspect`, `sniper_filter_suspect`,
`buy_realm_name`/`buy_realm_category` — all unconditional — plus, only when
`names=true`, `name`/`icon`/`quality`/`quality_color`/`item_class`/`item_subcl
ass`/`is_profession_item`/`sus_item_suspect`/`variant`, which need `NameCache`
lookups (the rest are pure SQL, hence free). **Event-loop discipline — this
project's most-repeated bug:** `api_snipes()` is `async def` and does 30-175s
of work, so both the query (`_run_query`) and the `names=true` row build
(`_build_rows`, prewarming via
`NameCache.ensure_many()`/`.ensure_icons_many()` under
`LIVE_RESOLVE_DEADLINE_SECONDS`) run inside `asyncio.to_thread()`. **Any new
blocking call site needs the same treatment** — this has already recurred once
at a site the first fix didn't cover. **Auth/tiers:** `/api/snipes`,
`/api/me`, `/api/status` and `/api/realms` are all unauthenticated;
`api_snipes()` resolves its user manually via
`auth.resolve_user_from_request()` rather than `Depends(current_active_user)`,
because a `Depends` chain holds a pooled DB connection for the request's
entire duration (a real pool-exhaustion outage). Caps: `ANON_SNIPE_CAP` 250 /
free 500 / subscribed 2000 / superuser 10000, with matching
`ANON_`/`FREE_`/`SUBSCRIBED_`/`SUPERUSER_CLASS_QUOTAS` dicts that each sum
**exactly** to their own cap (tests assert this). `_run_query()` always passes
`min_value_floor_g=snipe_check.MIN_VALUE_FLOOR_G`. `_enforce_realm_lock()` and
`_enforce_anon_realm_lock()` both delegate to `_atomic_lock_first_realm()` —
one `UPDATE ... WHERE locked_sell_realm IS NULL` with
`synchronize_session=False`, never read-then-write (a real TOCTOU race); non-
subscribed accounts and anonymous sessions alike lock to their first-queried
realm. Routes: `/` → `landing.html`, `/snipes` → `dashboard.html`, plus
`/snipe-board`, `/watchlist`, `/pricing`, `/profile`, `/admin`; `GET
/api/realms/eu` (subscriber-only, fans out to one entry per *member* realm
name) backs `wow_accounts.py`. `/robots.txt` and `/sitemap.xml` (2026-08-06)
serve the matching files out of `static/` with explicit media types — they
can't live under the `/static` mount because crawlers only read them from the
origin root; `no_cache_html` ignores both (it keys on `text/html`), so they
cache normally. Middleware: `no_cache_html` (`Cache-Control:
no-cache` on every HTML response — without it browsers serve a stale page
after a deploy), `ensure_anon_cookie` (sets `ah_anon` on the genuinely final
response; a route-local `Response` param is invisible when a route raises
`HTTPException`, which is the *common* anonymous path), and
`admin.track_activity` (implementation in `admin.py`; registered here because
middleware attaches to the app, not to a router). `lifespan` starts two
background tasks: the collection loop (gated on
`ENABLE_BACKGROUND_COLLECTION`) and `admin._visitor_flush_loop` (gated on
`DATABASE_URL` being set — `db.engine()` raises `SystemExit`, a
`BaseException` the loop's `except Exception` deliberately won't swallow,
when it isn't). `logging.basicConfig(stream=sys.stdout)` — the default
`stderr` makes Railway tag every log line as an error. History: see
history.md.

## `admin.py`

Superuser-only activity tracking + visitor history (split out of
`dashboard.py` 2026-08-04). `current_superuser` (403 for a logged-in
non-superuser, on top of `current_active_user`'s 401) gates every route:
`GET /api/admin/active-users` (`last_seen` within `ACTIVE_WINDOW_SECONDS`,
15 min), `GET /api/admin/visitors` (full history, newest first, capped at
`VISITOR_HISTORY_LIMIT`), `GET /api/admin/signups` (registered accounts
with email, newest first, capped at `SIGNUP_LIST_LIMIT`) and
`GET /api/admin/watchlist/{owner_id}` (one account's watchlist items,
capped at `WATCHLIST_DETAIL_LIMIT`); `/admin` → `admin.html` renders them.

### Account attribution on visitor rows (2026-08-06)

`db.VisitorIP.user_id` records the most recent **authenticated** account seen
from an IP, so `/active-users` and `/visitors` emit `user_email` /
`user_nickname` and the page can answer "which *users* are active", not just
which addresses. `/active-users` also returns `signed_in_count`.

The hard constraint is that this had to cost **no database work on the
request path** — this module's docstring documents a real outage from exactly
that. `_client_user_id()` therefore decodes the `ah_auth` JWT locally
(`fastapi_users.jwt.decode_jwt` with `auth.SECRET` and the strategy's own
audience) and reads `sub`; it never asks Postgres who the user is. Any
signature/expiry/audience failure returns None — this runs in front of every
request, so it must never be able to reject traffic. A validly-signed token
for a since-deleted account is caught at read time instead: the endpoints
join to `User`, and a dangling id renders as anonymous rather than as a
fabricated account.

Deliberately last-writer-wins on one row per IP, **not** an (ip, user)
history table — that table's whole design is to stay bounded by distinct
visitor count. Two consequences worth knowing before trusting the column:
a shared NAT/household IP shows only whichever account hit the API most
recently, and it is never cleared on logout, so it means "last account seen
here", not "who is logged in right now". A later *anonymous* request from the
same IP leaves it intact (tested).

### Watchlist visibility (2026-08-06)

`/signups` carries `watchlist_count` (one grouped query, not a count per
row), `default_sniper_list_enabled` and `has_discord_webhook`. The webhook is
exposed **only as a boolean** — the URL itself is a bearer credential that
lets anyone holding it post into that channel, and a test asserts the token
never appears in the response body.

The items themselves are their own endpoint, fetched only when a row is
actually expanded: one account may hold `watchlist.MAX_WATCHLIST_ITEMS_PER_USER`
(500) items, so embedding them would grow the signup payload with the
product's own success for data almost none of which is on screen. Item names
come from `NameCache`, whose cold lookups make live blocking Blizzard calls,
so the build runs inside `asyncio.to_thread()` bounded by
`LIVE_RESOLVE_DEADLINE_SECONDS` — the same precaution `watchlist.list_watchlist()`
takes, for the same reason.

The standing sniper-list rule is reported as **on/off only**, never as its
matches: those are region-wide and identical for every account sharing the
same thresholds, so rendering them per user would repeat one list N times.

`admin.html` keeps expanded rows in a module-level `Set` plus an item cache,
because the page re-renders every 30s and would otherwise snap an open list
shut while it was being read.

`/signups` returns an **explicit field allowlist**, never the whole `User`
row — that row also carries `hashed_password` and the Stripe customer/
subscription ids, and a test asserts the exact key set so a future column
can't leak by default. It reads `db.User.created_at`, added 2026-08-04
because FastAPI-Users' base table has no signup timestamp and `uuid4` ids
carry no embedded time. That column is **nullable with no backfill**:
accounts predating it have no recoverable signup date, so they stay NULL and
the page shows "before tracking" rather than inventing one. Ordering is
`created_at DESC NULLS LAST` — Postgres defaults to NULLS FIRST on DESC,
which would bury real recent signups under the legacy rows. Note that
SQLAlchemy applies the column default whenever the attribute is None at
flush, so a NULL `created_at` cannot be produced through the model; only the
migration's pre-existing rows have one (tests reproduce that with a
follow-up `UPDATE`).

**The two-layer split is the whole design and must not be collapsed.**
`track_activity` writes only to two in-process dicts (`_recent_activity` for
liveness, `_pending_hits` for counts) and does no I/O;
`_visitor_flush_loop` batches those into `db.VisitorIP` every
`FLUSH_INTERVAL_SECONDS` (60). A per-request `INSERT` would push straight on
the pool exhaustion `db.engine()`'s comment records as a real outage, and
the dashboard auto-refreshes. Both reads come from the table, not the dicts,
so the numbers survive a redeploy — the cost is that a brand-new IP can lag
up to one flush interval before appearing.

`flush_visitors()` swaps the buffer out before writing (hits arriving
mid-flush land in the fresh dict rather than being dropped) and does a
portable SELECT-then-UPDATE/INSERT rather than a dialect-specific upsert —
one writer, one process, no race to lose. `_as_utc()` exists because
`DateTime(timezone=True)` is honoured by Postgres but not SQLite, so the same
column arrives aware in production and naive under test.

`_client_ip()` reads `X-Forwarded-For`, not `request.client.host` (which only
shows Railway's proxy hop), and truncates to `MAX_IP_LEN` (45) — that value
is an attacker-controlled header and is now a `String(45)` primary key.
`admin.html` builds every row with `textContent`, never `innerHTML`, for the
same reason.

Why this is a module and not a Railway service (asked 2026-08-04): the thing
being observed is this process's own request traffic, so the middleware has
to run in the process that serves it. A second service could read the table
but the writer would still live here — the split would buy a duplicated auth
stack and a second deploy target for zero isolation.

## `auth.py`

FastAPI-Users wiring: email/password register+login, cookie-based sessions.
`current_active_user` gates login-only routes. `has_active_subscription(user)`
— single source of truth for "unrestricted" (active subscription OR
superuser), used by both `_enforce_realm_lock` and anywhere else that needs
the same check. `current_subscribed_user` (402 if not subscribed) went unused
by any route for a while after free tier superseded its original purpose,
until `wow_accounts.py` became its first real consumer (2026-08-02, see that
row below). `COOKIE_SECURE` env toggle (`false` in dev `.env`, unset/secure in
production). **`resolve_user_from_request(request) -> User \| None`** (added
2026-08-01, real production incident: Postgres pool exhaustion broke login
under barely any real traffic): manually resolves the current user from the
request's `ah_auth` cookie using a session opened and closed *within this one
function call*, instead of `current_active_user`'s `Depends()` chain, which
FastAPI keeps checked out for a route's *entire* request lifetime (confirmed
by reading `fastapi/routing.py`'s `solve_dependencies`/`AsyncExitStack`
handling — cleanup only runs after the full response is sent).
`dashboard.py`'s `/api/snipes` is the one route in the app that does 30-175s
of unrelated DuckDB/Blizzard work after authenticating; holding a pooled
connection open for all of that, on every call, at every tier, was the actual
root cause of the incident (a smaller, immediate mitigation — raising the pool
from 15 to 60 connections — shipped the same day but doesn't fix the
underlying pattern). Mirrors `fastapi_users.current_user(active=True)`'s real
behavior exactly (confirmed by reading the installed package's
`Authenticator._authenticate()`/`JWTStrategy.read_token()`): only the `active`
check applies — it is the `current_active_user` equivalent, not the
`current_verified_user` one. That distinction became load-bearing on 2026-08-06
when `current_verified_user` arrived: this helper's only callers (`api_me`,
`api_snipes`) are exactly the routes the verification gate leaves open, so
"active only" is complete *for them*, not an omission — anything needing
verification must use `Depends(current_verified_user)` instead, because this
function will never enforce it. (Since 2026-08-09 login itself requires
verification, so in practice every session reaching here is verified anyway —
but that is the auth router's doing, not this function's, and it must not be
relied on here.) `superuser=True` is still used nowhere.
Returns `None` on any failure (no cookie, invalid/expired token, unknown user,
inactive user) rather than raising — callers 401 themselves.
`db.sessionmaker()`'s `expire_on_commit=False` means the returned `User`'s
already-loaded scalar columns stay safely readable after the session closes.
`_enforce_realm_lock()` itself (`dashboard.py`) deliberately kept its exact
prior signature (still takes an explicit `session`) — its existing test
coverage, including a real two-independent-sessions TOCTOU race regression
test, depends on controlling that session precisely; only *where*
`api_snipes()` gets that session from changed (a short-lived `async with
db.sessionmaker()() as session:` opened just for that call, not a route-level
`Depends(get_async_session)` held for the rest of the request). Covered by
`tests/test_auth.py`'s `test_api_snipes_invalid_cookie_falls_back_to_anonymous
`/`test_api_snipes_inactive_user_falls_back_to_anonymous` (renamed 2026-08-03
— both cases now fall through to the anonymous path below rather than 401ing,
see that row) and `tests/test_dashboard.py::test_api_snipes_closes_the_realm_l
ock_session_before_slow_work` (patches `AsyncSession.close()` at the class
level — `__aexit__` is dispatched via the type, not instance attributes, so an
instance-level patch isn't seen by `async with` — to directly confirm the
session closes before `find_snipes()` runs). **Test-seam note**:
`tests/test_dashboard.py`'s `bypass_auth` fixture now also monkeypatches
`auth.resolve_user_from_request` directly (not just
`dependency_overrides[current_active_user]`) since it's a plain function call,
not a declared FastAPI dependency, so `dependency_overrides` alone no longer
reaches `/api/snipes`; both `tests/test_dashboard.py` and
`tests/test_auth.py`'s fixtures also now monkeypatch `db.sessionmaker` itself
(module-qualified — `auth.py`/`dashboard.py` both `import db` rather than
`from db import sessionmaker`, specifically so this stays patchable) so the
new direct-session-usage code paths land in the same throwaway SQLite test DB
as everything else, alongside the pre-existing
`dependency_overrides[get_async_session]` mechanism (kept, not replaced, since
other routes still use it).
**`ANON_COOKIE_NAME`/`resolve_or_create_anon_session(request, session)`**
(added 2026-08-03, letting a visitor with no account use `/snipes`):
resolves/mints a `db.AnonSession` identity from a separate `ah_anon` cookie (a
different name than `ah_auth` so `CookieTransport` never tries to JWT-decode
it), returning its opaque token. Called by `dashboard.py`'s
`api_me()`/`api_snipes()` only once `resolve_user_from_request()` has already
returned `None` — the "no real account" fallback, never a replacement for real
auth. Does **not** set the cookie itself (that was tried first via a route-
local `response: Response` parameter and found broken by a real bug: FastAPI's
exception handling builds an entirely separate `Response` when a route raises
`HTTPException`, which never sees mutations made to that parameter — and
`/api/snipes` routinely raises `HTTPException` for anonymous requests, 400 for
an uncollected realm or 403 for a different-realm lock, the *common* case for
a first-time visitor, not an edge case). Instead stashes the resolved token on
`request.state.anon_token`; `dashboard.py`'s `ensure_anon_cookie` middleware
reads it after `call_next()` and sets the cookie on the response middleware
actually receives, which is the genuinely final one regardless of whether a
route returned normally or raised — see that middleware's own docstring.

### Email verification, password reset, Google login (2026-08-06)

Three features shipped together because they share one prerequisite (a sender,
`mailer.py`) and because a Google-created account has no usable password, which
makes self-service reset the only route to also getting one.

`UserManager` grew three hooks: `on_after_register` calls `request_verify()`
(early-returning when the account is already verified, which is the Google case
— otherwise a Google signup gets emailed a confirm link it has no reason to
click), and `on_after_request_verify`/`on_after_forgot_password` send the mail.
Routers mounted in `dashboard.py`: `get_verify_router`, `get_reset_password_router`,
and `get_oauth_router` — the last only when `google_oauth_client is not None`, so
a fresh checkout and CI can still import and serve everything else.
`/api/auth-config` reports `{google, email}` so `login.html`/`register.html` can
hide a button that would 404.

Four non-obvious pieces, each of which was a real break during implementation:

- **`RedirectCookieTransport`** — the OAuth callback ends in
  `backend.login(...)`, and plain `CookieTransport.get_login_response()` returns
  a bare **204**. The callback is a full browser navigation from Google, so a 204
  leaves the user on a blank page: logged in, cookie set, no way to tell. A
  second `AuthenticationBackend` (`oauth_redirect_backend`) over the *same* JWT
  strategy and *same* `ah_auth` cookie returns a 302 to `/snipes` instead;
  sessions from either backend are interchangeable.
- **`PUBLIC_BASE_URL`** — passed to `get_oauth_router` as an explicit
  `redirect_url` rather than letting it call `request.url_for()`. Google matches
  `redirect_uri` byte-for-byte, and uvicorn here isn't configured to trust
  Railway's proxy headers (`admin._client_ip()` parsing `X-Forwarded-For` by hand
  is the tell), so the inferred scheme can be `http`. Also used for email links.
- **`csrf_token_cookie_secure=COOKIE_SECURE`** — the OAuth state cookie defaults
  to `Secure=True` and would silently vanish over local `http://`, the exact trap
  `auth.py` already documents for `ah_auth`.
- **`UserManager.oauth_callback` override** — FastAPI-Users'
  `is_verified_by_default` applies *only to newly created* accounts; its
  `associate_by_email` branch never touches `is_verified` (confirmed by reading
  `BaseUserManager.oauth_callback`). Without the override, registering with a
  password, never confirming, then logging in with Google on that same address
  leaves the account unverified — still blocked, still bannered — right after
  Google proved the very thing our confirmation email exists to prove. The
  override is gated on an explicit `EMAIL_VERIFYING_OAUTH_PROVIDERS` allowlist,
  not on "any OAuth login", so a future provider that doesn't validate addresses
  can't inherit it by accident.

`associate_by_email=True` + `is_verified_by_default=True` are safe *specifically*
because Google guarantees the address; the standard warning against the former
applies to providers that don't.

**Login gate (2026-08-09).** `get_auth_router` is mounted with
`requires_verification=True`, so `/auth/login` answers **400
`LOGIN_USER_NOT_VERIFIED`** for a password account that hasn't clicked the link.
`login.html` branches on that detail specifically — it says "confirm your email"
and offers a resend through `/auth/request-verify-token`, rather than falling
through to the generic failure and implying the password was wrong.

Three consequences worth knowing before touching this:

- **Google is exempt with no exemption code.** The two settings above already
  verify the account before any login check runs. That also makes "sign in with
  Google" the recovery path for a password account whose mail never arrived, and
  it is the `oauth_callback` override above that makes that path work.
- **The read paths are untouched.** Anonymous visitors still reach `/api/snipes`,
  `/api/status` and `/api/realms`, so this gates holding a session, not seeing
  the data. Extending it to the read paths would gain nothing (see
  `progress.md`).
- **`current_verified_user` is now defence in depth**, not a gate users meet: no
  product path yields an unverified logged-in account. Keep it anyway — it
  re-reads the DB row, so it still holds if the flag is cleared under a live
  session, which is exactly what its tests now construct.

## `mailer.py`

One async function, `send(to, subject, html) -> bool`, POSTing to
`api.resend.com/emails` through the `httpx` already in `requirements.txt` — no
new dependency. Two deliberate behaviours:

- **Unconfigured means log, not fail.** No `RESEND_API_KEY` → the message (with
  its working link) goes to the log and `send()` returns. That's what makes a
  fresh checkout usable and the test suite hermetic without mocking the network;
  CI sets no key, so it takes this path. Same posture as `billing.py` tolerating
  a missing Stripe key rather than refusing to import.
- **Never raises.** Every failure is caught and logged. `on_after_register` calls
  this, and an exception escaping there would turn a successfully-created account
  into a 500 with no way to tell the registration itself worked. An undelivered
  mail is recoverable via the resend affordance; that isn't.

## `db.py`

Async SQLAlchemy for the *relational* data only (users/sessions/subscription
state, and now forum posts) — separate from the parquet+DuckDB AH data layer.
`User` model: FastAPI-Users base fields + `stripe_customer_id`/`stripe_subscri
ption_id`/`subscription_status`/`subscription_current_period_end` (written
only by `billing.py`'s webhook) + `locked_sell_realm` (nullable, written only
by `dashboard._enforce_realm_lock`) + `nickname` (added 2026-07-29, nullable,
no uniqueness constraint — public display name for Snipe Board posts, written
only by `dashboard.update_nickname()`; see that route's entry above for why it
exists). `ForumPost` model (added 2026-07-29): `author_id` (FK to `user.id`,
using the same `fastapi_users_db_sqlalchemy.generics.GUID` type as `user.id`
itself) + `author_email` (denormalized at post time, **no longer exposed by
the API** — see `forum.py`'s row below, kept only for internal reference) +
`author_nickname` (added 2026-07-29, denormalized the same way and for the
same reason as `author_email` — a post keeps showing the nickname the poster
had *at post time* even if they change it later; nullable only because posts
made before this column existed have none) + `title` (nullable) +
`image_filename` + `created_at` (set in Python via
`datetime.now(timezone.utc)`, not a DB `server_default`, so it's identical
across the Postgres-in-production/SQLite-in-tests split).
**`WowAccount`/`WowAccountRealm` models** (added 2026-08-02, see
`wow_accounts.py`'s row): a subscribed user's self-declared WoW-account labels
(`WowAccount.owner_id` FK, `.label`) and, per account, which EU connected-
realm ids have a character there (`WowAccountRealm.wow_account_id` FK,
`.connected_realm_id`, `UniqueConstraint(wow_account_id,
connected_realm_id)`). No ORM `relationship()` on either side — this file uses
plain FK columns everywhere, no back-refs, stays consistent. No DB-level
cascade delete (SQLite test fixtures don't enable FK enforcement) —
`wow_accounts.delete_account()` deletes child realm rows explicitly, same
transaction. Tests override the session dependency with SQLite.
**`User.discord_webhook_url`/`WatchlistItem`** (added 2026-08-02, see
`watchlist.py`'s row): a nullable plain-text Discord webhook URL (no OAuth) is
the Watchlist notification delivery target; `WatchlistItem` (`owner_id` FK,
`item_id`, `pet_species_id` nullable — no `pet_quality_id` column,
deliberately: matching was never asked to be quality-granular for this
feature) carries a nullable `trigger_price_copper` (nullable specifically so a
bulk TSM-group import doesn't have to force one shared price onto every
imported item — see that row for the full reasoning), an optional `label` (a
TSM group's path, when imported that way), and `last_notified_at` (backs the
notification cooldown). Real cross-DB gap found and fixed while building this:
SQLite (tests) returns a naive `datetime` for a `DateTime(timezone=True)`
column even though `datetime.now(timezone.utc)` is always what gets written,
while Postgres (production, via asyncpg) round-trips tz-aware correctly —
`watchlist.check_triggers()`'s cooldown math normalizes
(`.replace(tzinfo=timezone.utc)` when naive) rather than assuming either
driver's behavior, confirmed live via `tests/test_watchlist.py`'s cooldown
tests failing with `TypeError: can't subtract offset-naive and offset-aware
datetimes` before the fix. **`isolated_session()`** (added 2026-08-02, real
production incident: every single `collect_all.py` cycle was throwing
`asyncpg.exceptions._base.InternalClientError: got result for unknown protocol
state 3` / `RuntimeError: ... got Future <Future pending> attached to a
different loop` inside `watchlist.check_triggers()`, confirmed live via
`railway logs` — silently swallowing every Watchlist Discord notification with
zero visible symptom on the dashboard itself, since `collect_all()`'s per-step
`try/except` means one failed step never breaks the cycle or shows up anywhere
but the log): `engine()`/`sessionmaker()`'s module-level singleton pool is
shared with the *main* FastAPI event loop, but `watchlist.check_triggers()`
runs inside its own `asyncio.run()`-created loop (a background OS thread, see
that function's docstring) — asyncpg connections are loop-bound, so handing
out a pooled connection that was opened under the main loop to this different
loop corrupts the connection. `isolated_session()` is an
`@asynccontextmanager` that creates a brand-new single-connection engine
(`pool_size=1`) and fully disposes it within the same call, so no connection
ever crosses a loop boundary; used only by
`watchlist._check_triggers_async()`, not by any request-path code (those all
correctly stay on the main loop and keep using `sessionmaker()`). Covered by
`tests/test_watchlist.py`'s `bypass_get_async_session` fixture, which
monkeypatches `db.isolated_session` to reuse the test's own SQLite engine
(aiosqlite has no loop-binding issue, so the fixture doesn't need to replicate
the create/dispose dance, just the seam). **`AnonSession`** (added 2026-08-03,
see `dashboard.py`'s Auth/tiers entry): a visitor-with-no-account's identity,
keyed by an opaque `token` (the primary key, `uuid.uuid4().hex` — same
generation convention as `forum.py`'s image filenames) set as the `ah_anon`
cookie (see `auth.py`'s row). `locked_sell_realm` mirrors `User`'s own column
exactly — the same "locked to the first sell realm ever queried" anti-abuse
rule now applies to anonymous browsing too, enforced by the same shared
atomic-lock core (`dashboard._atomic_lock_first_realm`)
`_enforce_realm_lock`/`_enforce_anon_realm_lock` both call. No cleanup/expiry
mechanism, deliberately, same precedent as `ForumPost` rows never being
reaped.

**`OAuthAccount`** (added 2026-08-06, Google login): FastAPI-Users'
`SQLAlchemyBaseOAuthAccountTableUUID` verbatim, no project-specific columns.
Nothing ever reads the stored access/refresh tokens back — Google is used purely
to establish "this person controls this address" at login, there's no Google API
we call on the user's behalf; the columns exist because the base table defines
them. `get_user_db` must pass it as the third `SQLAlchemyUserDatabase` argument
or the OAuth callback raises `NotImplementedError` at its final step.

⚠️ **`User.oauth_accounts` is mapped `lazy="selectin"`, deliberately against
FastAPI-Users' own documented `lazy="joined"`.** Eager loading is mandatory, for
two independent reasons: `auth.resolve_user_from_request()` closes its session
before returning the `User`, and `add_oauth_account()` touches
`user.oauth_accounts` after a `session.refresh()` — under asyncio a lazy
collection load raises `MissingGreenlet` in both cases, so Google login breaks
outright. But a *joined* eager load against a collection makes SQLAlchemy require
`.unique()` on every `Result` returning `User` entities. The library's own queries
do call it (`SQLAlchemyUserDatabase._get_user`); this app's don't — `admin.py`,
`billing.py`, `watchlist.py` and `grant_free_month.py` all have their own
`select(User)`, and **all six sites broke** when this first shipped as
`lazy="joined"`. Every future `select(User)` would too, which is precisely the
recurring-trap shape this project has been bitten by before. `selectin` is just as
eager, needs `.unique()` nowhere, and costs one extra indexed SELECT against a
table with one row per linked Google account. The migration adds an index on
`user_id` that the base table doesn't declare, since that selectin load runs on
every authenticated request.

## `billing.py`

**Live Stripe mode** (human decision — deployed straight to live, no test-mode
verification pass). `POST /billing/checkout` creates a Checkout Session for
the single €4.99/mo price; `POST /billing/webhook` verifies the Stripe
signature and handles `checkout.session.completed`/`customer.subscription.upda
ted`/`customer.subscription.deleted`, the only writer of the user's
subscription fields. Still on the full `sk_live_...` secret key, not a
restricted key (see "Roadmap" — human-only to change).
**`create_checkout_session`'s `success_url` fixed to `/snipes`** (2026-07-31,
real bug fix found during a repo-wide audit — was still `/` from before the
2026-07-26 routing migration moved the tool off the root path; every other
post-login/post-action redirect was updated then, this one was missed, so a
paying customer landed on the marketing page instead of the tool).
**`_period_end()` now checks both possible field locations** (2026-07-31, same
audit): `current_period_end` used to live directly on the Stripe Subscription
object; newer Stripe API versions moved it onto each `SubscriptionItem`
instead, and this project pins no `stripe.api_version`, so which shape a real
webhook sends was never confirmed directly — falls back to
`subscription["items"]["data"][0]["current_period_end"]` when the top-level
field is `None`, so an account on a newer API version doesn't silently get
`None` written for every renewal date.

## `forum.py`

Backing module for the **Snipe Board** page (renamed 2026-07-29 from an
initial "Forum" — human request, module/route names weren't renamed to match,
same precedent as `dashboard.py` serving `/snipes`). "Post a snipe you found"
feature — an image (required) + optional title, logged-out visitors can see
every post, posting requires login **and a nickname** (added 2026-07-29, human
feedback: posts were showing the account's real email publicly, the wrong
default). `create_post()` rejects with 400 if `user.nickname` is unset —
enforced here, not at registration, since that also naturally covers every
account that registered before nicknames existed; `static/snipeboard.html`'s
post dialog is what actually prompts for one inline (via `PATCH
/api/me/nickname`, see `dashboard.py`'s row above) before a first post, but
this check is the real boundary, not that client convenience.
`_post_to_json()` returns `author_nickname` (falling back to the literal
string `"Anonymous Sniper"` for the small number of posts made before this
column existed — see `db.ForumPost`) and never `author_email`. Deliberately
minimal: no editing/deleting/comments/moderation. Two `APIRouter`s: `router`
(`/api/forum/posts`, `GET` public / `POST` gated by
`auth.current_active_user`) and `image_router` (`/forum/images/{filename}`,
public, reads `IMAGE_DIR` fresh per request rather than a `StaticFiles` mount
— a fixed-at-mount-time directory can't be redirected into a tmp dir for
tests, this can via `monkeypatch.setattr(forum, "IMAGE_DIR", ...)`). Images
are plain files under `DATA/forum_images/` on the same persistent volume
`data/snapshots`/`data/listings` already use (`ALLOWED_IMAGE_TYPES` content-
type allowlist, not a client-supplied extension; `MAX_IMAGE_BYTES` = 5MB) —
server-generates the filename (`uuid4().hex` + the validated extension) so the
client's original filename is never trusted for anything, including the path
`serve_image()` reads from (`Path(filename).name` strips any directory
components as defense in depth). Wired into `dashboard.py` via
`app.include_router(forum.router)` / `app.include_router(forum.image_router)`,
plus a public `GET /snipe-board` route serving `static/snipeboard.html` (same
client-side-gate convention as `/snipes` — the page itself checks `/api/me` to
decide whether to show the "+ Post a snipe" button or a "log in to post"
link).

## `wow_accounts.py`

**New (2026-08-02)**, human request — "a user on their profile can add wow
accounts... on each account there should be the option to add realms...
display to the user which wow account they should log into." A subscribed user
registers self-declared WoW-account labels (no Blizzard OAuth/credentials
anywhere — just a string typed in on `profile.html`) and, per account, which
EU connected-realm ids have a character there; `static/dashboard.html` cross-
references each snipe row's buy-side realm against this client-side to show a
"Your account" column. Mirrors `forum.py`'s shape: own
`APIRouter(prefix="/api/wow-accounts")`, `Depends(current_subscribed_user)`
throughout (not `current_active_user` — gated the same as the paid sniping
feature itself, the first real consumer of that dependency, see `auth.py`'s
row), manual `if`/`raise HTTPException` validation, a `_account_to_json()`
serializer. `GET ""` (list this user's accounts + realm ids, no Blizzard/name
lookups — names are resolved client-side against the new `GET /api/realms/eu`,
see `dashboard.py`'s row), `POST ""` (create), `PATCH "/{account_id}"`
(rename), `DELETE "/{account_id}"` (delete, explicitly deletes child
`wow_account_realm` rows first since there's no DB-level cascade — see
`db.py`'s row), `POST "/{account_id}/realms"` (add a realm), `DELETE
"/{account_id}/realms/{connected_realm_id}"` (remove one).
**`MAX_ACCOUNTS_PER_USER=8`** (lowered from an initial 10 the same day,
2026-08-02 — matches Blizzard's real per-Battle.net-account WoW account limit,
human-specified) **`/MAX_REALMS_PER_ACCOUNT=50`** (human-specified) enforced
via `_insert_account_atomic()`/`_insert_realm_atomic()` — a single `INSERT ...
SELECT ... WHERE (SELECT COUNT(*) ...) < cap` statement, not a separate
`SELECT COUNT(*)` followed by a Python-side `if` — this app has two recorded
TOCTOU bugs from exactly that read-then-write pattern
(`dashboard._enforce_realm_lock`'s old race, `item_names.NameCache.save()`'s
lost-update race, both in `history.md`), both fixed the same way: making the
check and the write one atomic DB statement instead of two round trips.
Exposed as standalone functions (not inlined in the route handlers)
specifically so tests can drive the exact interleaving directly with
independent sessions, same testability precedent as
`dashboard._enforce_realm_lock`. The duplicate-realm case doesn't need a
second atomic check at all — `WowAccountRealm`'s `UniqueConstraint` is the
real atomic guard, caught here as an `IntegrityError`. Ownership checks
(`_get_owned_account()`) 404 rather than 403 on "exists but isn't yours" — no
existence-leaking, same non-distinguishing precedent used everywhere else in
this app. Covered by `tests/test_wow_accounts.py` (CRUD, both caps including
real concurrent-race regression tests via two independent SQLite sessions,
ownership, auth-gating) and `tests/test_dashboard.py`'s `GET /api/realms/eu`
tests. **`profile.html`'s UI was fully redesigned the same day** (human
follow-up, "this whole adding wow accounts needs a big UI/make sense update" →
"remake the entire profile page") — see `static/profile.html`'s row below for
the new numbered-cards/searchable-realm-picker shape; the account cap dropped
from an initial 10 to 8 in the same pass (see above). `GET /api/realms/eu`
(`dashboard.py`'s row) also changed shape the same day to support searching a
connected realm by any of its member names, not just its primary one — see
that row for why.

## `watchlist.py`

**New (2026-08-02)**, human request -- "user imports TSM groups or RAW itemids
and set a trigger price for when they want a notification." Watchlist tracks
specific items across every EU realm, independent of any sell realm (see
`feature-watchlist.md`, whose six open questions this module resolves -- see
that file's own updated "Open questions" section for the human decisions
behind each). Key product decisions, all explicit human calls during the same
conversation: matching is `item_id`-only (+ optional `pet_species_id`), not
bonus/ilvl-aware, matching the rest of the product's 2026-07-26 decision
rather than reopening it; `trigger_price_copper` is a plain user-set absolute
gold price, explicitly **not** a discount-vs-region-median "auto-price" the
rest of the product uses elsewhere ("Ignore this 'autoprice' sort of thing
now, we only want to trigger for whatever price the user wants" -- corrected
mid-build after an initial round of `AskUserQuestion`s had already been
answered "absolute gold price," reinforcing the same call); delivery is a per-
user Discord webhook URL (`db.User.discord_webhook_url`), chosen over in-app-
only (passive, only useful if checked) or email (real new infra, no existing
precedent in this project) via an explicit `AskUserQuestion` round. Mirrors
`wow_accounts.py`'s shape: own `APIRouter(prefix="/api/watchlist")`,
`Depends(current_subscribed_user)` throughout (premium-only, matching the
"Coming soon (premium-only)" badge the `watchlist.html` placeholder already
shipped with), manual `if`/`raise HTTPException` validation, the same atomic
`INSERT ... SELECT ... WHERE count < cap` pattern as
`wow_accounts._insert_account_atomic()` for `MAX_WATCHLIST_ITEMS_PER_USER=500`
(a UX limit, not a human-tuned number like most thresholds in this product --
picked generously enough that a real 300-item TSM group import comfortably
fits). **Route registration order matters**: `PATCH /discord-webhook` is
registered *before* the `PATCH "/{item_id}"`/`DELETE "/{item_id}"` routes --
found live while writing this module's own tests (a 422 Unprocessable Entity,
since Starlette matched `/discord-webhook` against `/{item_id}: int` first and
failed the int conversion) -- FastAPI/Starlette matches routes in registration
order, not by specificity. `POST /import-tsm` calls
`tsm_import.decode_group_export()` (via `asyncio.to_thread()`, same "any new
blocking call site needs checking" discipline as every other route in this
app, see "Real production outage" below -- CPU-bound Lua execution, not
network, but not guaranteed instant for an arbitrarily large pasted group
either), inserts each parsed item with `trigger_price_copper=None` (a TSM
export carries no price data -- forcing one shared trigger onto a whole
imported group would be meaningless) and `label` set to the item's group path,
skipping item ids already on the user's list (checked both against existing DB
rows and within the same import batch, so a group with the same item under two
sub-paths doesn't double-insert). **`check_triggers()`** is the sync entry
point `collect_all.py`'s background loop calls every ~10-min cycle (no new
scan cadence, per `feature-watchlist.md`'s resolved open question #6) --
`asyncio.run()` inside a plain sync function is safe here specifically because
`collect_all()` itself only ever runs via `asyncio.to_thread()`, a real OS
thread with no event loop of its own already running. That alone isn't the
full story, though: `_check_triggers_async()` opens its session via
`db.isolated_session()`, not `db.sessionmaker()`'s shared singleton — see
`db.py`'s row for the real production bug (every cycle silently failing on an
asyncpg cross-loop error) this fixed 2026-08-02. Reads every `WatchlistItem`
with a trigger set via a real `JOIN` against `User` (needs the owning user's
`discord_webhook_url`), computes each watched item's current region-wide
cheapest listing from `data/listings/*.parquet` (`_region_cheapest_by_item()`,
a plain `min()` over the whole region sweep -- unlike `snipe_check.py`'s sell-
realm-relative comparison, Watchlist has no sell realm to exclude), and
Discord-POSTs (`_send_discord_notification()`, a plain `requests.post()` with
a 10s timeout, wrapped in try/except, never raises) when a listing clears the
trigger. **`NOTIFY_COOLDOWN_SECONDS=4*60*60`** (4 hours, not human-specified
-- a reasonable default worth tuning with real usage, unlike this product's
usual human-tuned-constant convention) gates re-notifying the same still-cheap
item every cycle, tracked via `WatchlistItem.last_notified_at`. Covered by
`tests/test_watchlist.py` (CRUD, cap enforcement, TSM import including the
real sample string from `tests/test_tsm_import.py`, ownership, and
`check_triggers()`'s trigger/cooldown/no-webhook-still-tracks-silently
behavior against a real fixture `data/listings/*.parquet`) -- the cooldown
tests found the tz-naive/aware SQLite gap documented in `db.py`'s row above.

### Standing rule scan (experimental, 2026-08-05)

A **second, entirely different trigger shape** alongside the per-item
`trigger_price_copper` above: a standing rule over *every item in the region
sweep* rather than a user's curated list. Human's spec: "alle items, med buy
under 100g og sale avg på over 3000 (brug flagged filter stadig - flagged
items skal aldrig sendes) + hvis det er transmog test imod unique transmog -
hvis ikke unique --> ignore item."

**The buy ceiling is proportional, not flat** (human's call, 2026-08-05,
after seeing real output): an item needs a TSM region sale average of at
least `RULE_MIN_SALE_AVG_COPPER` (2,000g) and must be listed under
`RULE_BUY_FRACTION_OF_SALE_AVG` (10%) of it -- so 5,000g/500g,
50,000g/5,000g. The original flat pair (buy < 100g, avg > 3,000g) got it
wrong at both ends: it admitted 40g junk while missing item 29726 ("Pattern:
Hood of Primal Life", TSM avg 11,111g, cheapest realm 100g) which the human
found by hand -- that one lost by a *single copper*, since 100g is not
< 100g. A regression test pins its real live figures.

The 2,000g minimum is what stops a flood, measured not assumed: an
intermediate version capped sub-3,000g items at a flat 100g instead of
rejecting them, and measuring against the live sweep gave **6,580 hits**
(vs 7 before), almost all sub-1g stacked trade goods -- "buy 0g, avg 2,851g,
285,086x". A huge multiple on a tiny absolute value is not a snipe; same
failure `snipe_check.MIN_VALUE_FLOOR_G` exists to prevent. With the 2,000g
floor the live figure is **116**.

An item TSM has **no** `avg_sale_price` for is ignored, not
passed through (human's follow-up call: "hvis sale avg ikke findes på item'en
så ignore for nu") -- the conservative resolution of "unknown isn't a claim"
for an outbound notification.

**Sell-realm-free, by the human's explicit choice** when offered the
alternative of reusing `snipe_check.find_snipes()` with the user's
`locked_sell_realm`. The consequence is stated rather than papered over: of
the three flags the dashboard's "Hide flagged (sniper filter)" checkbox ORs
together, only two are computable without a sell realm.

| flag | on this path |
|---|---|
| `sus_item_suspect` | applied unchanged (`snipe_check.is_sus_item()`, a pure function) |
| sniper filter's cluster comparison | applied (`_rule_cluster_suspect()`, needs only other realms' floors) |
| `price_suspect` | **not applied — not computable**, it is `sell_p_g >= 10 * region_median_g` and there is no `sell_p_g` here |

`RULE_CLUSTER_N`/`_CLOSE_MULTIPLE`/`_MIN_REALMS` are **imported from
`snipe_check`, never re-declared**, so this rule and the dashboard checkbox
cannot drift apart — the human's explicit worry ("kræver måske den laves i
backend også?"). `SNIPER_FILTER_HIGH_VALUE_EXEMPT_G` is deliberately *not*
honored: it only ever suppresses the flag (sends more), and the hard
requirement here runs the other way.

The unique-transmog test is the **opposite disposition** from
`snipe_check._filter_by_appearance()`, deliberately: that function answers
"give me unique-transmog items", whereas this rule asks "*if* this is
transmog, is it unique?" — so an item with no appearance at all (mount,
recipe, caged pet) is simply not transmog, the test does not apply, and it
passes. Different predicate, not an inconsistency.

**Profession tools and Blizzard-"Junk" items are excluded outright**
(2026-08-05, both from real delivered messages the human objected to:
"Burnt Rolling Pin", a `PROFESSION_TOOL`, and "Undelivered Love Letter",
item 67386). Both rules went into `snipe_check.is_sus_item()` rather than
into this module, because that one predicate is what the dashboard's "Hide
flagged" checkbox *and* this rule both already consult — the human asked for
them in "both backend and filter version (frontend)", and one shared
predicate is how that stays true. Junk uses Blizzard's own class 15 /
subclass 0, confirmed live, and is subclass-specific so mounts (15/5) and
recipes (class 9) survive. Note this means a profession tool now fails
`is_sus_item()` *before* the transmog test it used to slip past.

**Notifications are Discord embeds, not `content` strings** (2026-08-05,
human request with a screenshot -- the plain-text form "is not intiative and
easy to read"). `_embed_message()` builds one embed whose title links to
**undermine.exchange** for the realm the listing is on (`_undermine_url()`,
returning None rather than a guessed URL if the realm lookup fails), with
Price / TSM sale avg / Multiple as inline fields so consecutive finds line up
column-wise. Both notification shapes use it, in different colors.

**Filter order is load-bearing, not cosmetic.** The two cheap purely-local
tests (the DuckDB pass, then the TSM cache) run first so the later
`NameCache` lookups — which can make a *live Blizzard call* for an
unresolved item — only ever see a handful. Measured against real production
data 2026-08-05: 7,872 items under 100g → **14** after the sale-avg floor →
0 cluster-flagged → 14 reaching `NameCache`, of which **0** needed a live
call. The DuckDB pass itself is 0.12s over 92 realm files / 40 MB, which is
what makes it safe to run on the 45s-cadence cycles inside the publish
window.

Runs in its own `asyncio.run()` + `db.isolated_session()` and its own
try/except (`rule_error` in the result dict), so an experimental rule can
never take the established per-item path down with it — and as a separate
coroutine rather than folded into `_check_triggers_async()`, which returns
early when no user has any `WatchlistItem` at all, the exact state this
feature is most likely to be tested in. Cooldown state lives in a JSON file
(`_rule_state_path()`, a *function* so the tests' monkeypatched `DATA` is
honored) keyed per `(item_id, pet_species_id)` — a hit is a fact about the
region, so one hit fires one round of messages to every recipient; keying
per user would let a message to one recipient silence another.
`RULE_MAX_NOTIFICATIONS_PER_CYCLE=10` caps a first-run flood, and over-cap
hits are **not** marked notified so they return next cycle.
Recipients are **every subscriber with a webhook set** (human's call,
2026-08-05, widening the assistant's more cautious superuser-only default),
gated through `auth.has_active_subscription()` in Python rather than an
equivalent SQL `WHERE` — that helper is this app's single definition of
"subscribed" (`is_superuser or subscription_status == "active"`), and
re-expressing it as SQL would be a second copy free to drift.

## `/api/speed` and `/speed` (experimental, 2026-08-12)

Read-only wrapper over `speed_check.find_speed_listings()` — the +Speed
tertiary listing census. **Shares no filter, threshold or pricing logic with
`/api/snipes`**: no discount, no sell realm, no AH cut, no `MIN_VALUE_FLOOR_G`,
no class quotas, no appearance/sale-rate filter. Query params `items`,
`min_gold`, `max_gold`, `min_gap`, `name_contains`, `tarnished`, `armor`,
`quality`, `ilvl` (comma-separated levels; filters in SQL, and narrows the
reference stats to the same tier), `top`, `sort` (defaults to `price`, cheapest first), `names`;
`armor`/`quality` are comma-separated and validated on the route so a typo is
a 400 rather than a 500 raised inside the worker thread; `tarnished=true` resolves to the server-side
`speed_check.TARNISHED_NAME_MATCH` rather than the page posting the phrase
itself (same reasoning as `/api/me` sending the tier caps instead of letting
the frontend keep a second, driftable copy), and the response echoes both
`name_filter` and `tarnished_match` back for labelling. The name filter runs
inside the existing `to_thread` worker — it resolves names via `NameCache`,
which blocks on a cache miss. rows are built by
`_speed_row_to_json()` (its own serializer — `_row_to_json()` is shaped around
a snipe and reusing it would have meant faking `buy_realm`/`sell_now`/
`discount` or widening a function the whole snipe path depends on). Money is
exposed as both `_g` and `_copper`, per the units rule.

**Event-loop discipline applies here too**: both the DuckDB scan (`_run_query`,
a multi-second burn over every realm's parquet) and the `names=true` row build
(`_build_rows`, blocking Blizzard calls on a cold cache) run inside
`asyncio.to_thread()`. This is the failure mode that has already bitten twice.

**Auth, and the open product question in it.** The route requires a logged-in,
**verified** account (401 for anonymous, 403 for unverified — the same
distinction `auth.current_verified_user` makes), unlike `/api/snipes`'
anonymous tier. Reason: the census is region-wide by construction, so there is
no sell realm for `_enforce_realm_lock()` to pin and the free tier's one-realm
lock has nothing to bite on. Gating the whole route was the conservative
default for an experimental signal that may carry real value — **it is a
product decision the human should confirm**, and opening it up is a one-line
change (drop the checks and mirror `api_snipes()`'s anonymous fallthrough).
Row cap reuses the existing per-tier `_snipe_cap()` numbers rather than
inventing a second set. Uses `auth.resolve_user_from_request()`, not
`Depends(current_verified_user)`, for the same pool-exhaustion reason
`api_snipes()` does.

`/speed` serves `static/speed.html` (public route, client-side gate — same
convention as every other page here).
