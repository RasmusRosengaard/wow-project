# WoW AH Sale-Inference Prototype (EU retail)

A 48-hour experiment answering the question the whole product depends on:
**can we reliably infer *actual sales* from Blizzard's hourly auction snapshots?**
If yes, this pipeline becomes the brain behind a snipe validator (deal scoring,
liquidity, appearance rarity). If the signal is too noisy, we find out for the
cost of one weekend.

What it does: polls one connected realm's auctions hourly → parquet snapshots →
diffs consecutive snapshots → classifies every vanished auction (`inferred_sale`,
`likely_relisted`, `ambiguous`, `likely_expired`, `bid_only_gone`) → DuckDB
summaries: sales/day, sold-price percentiles, sell-through, current cheapest.

## Setup (one-time, ~10 minutes)

1. **API client (free):** log in at https://develop.battle.net → API Access →
   *Create Client*. Name: anything. Redirect URL: `https://localhost` (unused).
   Copy the Client ID and Secret.
2. **Python 3.10+:**
   ```
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. `cp .env.example .env` and paste in your ID/secret. Never commit `.env`.
4. Optional sanity check (no API calls): `pytest -q` — the inference logic's
   test suite should pass.

## Run

```
# 1. Find your connected-realm id (slug = lowercase, spaces -> hyphens)
python fetch_snapshot.py --find silvermoon

# 2. Collect. Use your HOME realm — verification requires posting your own
#    test auctions there. Leave running 48h+ (terminal, tmux, or a service).
python fetch_snapshot.py --cr-id 1096 --loop

# 3. After 48h: build events, then analyze
python diff_snapshots.py --cr-id 1096
python analyze.py --cr-id 1096 summary
python analyze.py --cr-id 1096 item 152510 --price 2500000   # price in copper

# 4. Verification: trace your test items to see how each vanish was classified
python analyze.py --cr-id 1096 trace <test_item_id>

# 5. Region scanner (Phase 1): sweep every other EU realm's current listings
#    (no history, just latest — the buy side of the cross-realm engine)
python scan_region.py --exclude 1096

# 6. Snipe check (Phase 1): flag scanner listings priced well below your sell
#    realm's sold-price percentile, net of the 5% AH cut
python snipe_check.py --sell 1096
python snipe_check.py --sell 1096 --items-file watchlist.txt --min-discount 0.3

# 7. Or run the whole pipeline in one pass (poll -> scan -> diff -> snipe-check),
#    re-run roughly hourly to match Blizzard's dump cadence
python run_cycle.py --sell 1096

# 8. Live dashboard: browse snipe results in a browser instead of the terminal
python dashboard.py --sell 1096
```

## Scheduled automation (Windows)

`run_cycle_task.ps1` wraps `run_cycle.py` for Windows Task Scheduler (no
console in a scheduled task, so it appends output to
`data/logs/run_cycle_task.log` instead). To (re)install an hourly task:

```powershell
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File "<repo>\run_cycle_task.ps1"'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "AHSnipePipeline" -Action $action -Trigger $trigger -RunLevel Limited
```

A cloud-hosted scheduler won't work for this pipeline: it needs the local
`.env` credentials and the local `data/` snapshot history (gitignored, never
pushed), neither of which a fresh cloud checkout would have.

The loop polls every 10 min but only downloads when Blizzard publishes a new
hourly dump (`If-Modified-Since`), so it's ~6 tiny requests/hour against a
36,000/hour limit. The collector survives errors (retries with backoff on
429/5xx, skips garbled responses) and logs to the console plus a rotating
`data/logs/collector.log`. A big EU realm produces roughly 50–100k non-commodity
auctions per snapshot; zstd parquet keeps 48h of data in the tens of MB.
Prices are in **copper** (10,000 copper = 1 gold).

`scan_region.py` is separate from the sell-realm collector: it sweeps *every*
EU connected realm's current listings into `data/listings/{cr_id}.parquet`,
overwriting each sweep — no snapshot history, no diffing, since the buy side
only needs "what's listed right now." Pass `--exclude` with your sell-realm
id(s) to skip realms already covered by `fetch_snapshot.py`. Use `--loop` to
sweep hourly (matches Blizzard's dump cadence); logs go to
`data/logs/scanner.log`. It has no relationship to sale inference — it's the
raw material `snipe_check.py` joins against sell-realm sold-price percentiles.

`snipe_check.py` matches listings to sell-realm sales on `(item_id, bonus_key)`
— the same exact-variant key the relist heuristic uses — requires a minimum
sales/day on the sell realm (liquidity floor, default 0.5/day) before trusting
its percentile, then flags listings where `sell_price * 0.95 - buy_price`
clears `--min-discount` (default 30%). Every flagged listing is guaranteed
unsoulbound (BoP items can't be listed on the AH), so it can always ride the
warband bank to your sell realm — the one real risk `snipe_check.py` doesn't
model is equipping/using the item before you move it, which locks it to that
character.

## Dashboard

`python dashboard.py --sell 1096` serves a live, auto-refreshing web view of
`snipe_check.py`'s results at `http://127.0.0.1:8000`, styled to feel like the
game itself: item names colored by rarity, gold/silver/copper coin icons for
prices instead of raw numbers, realm *names* instead of raw connected-realm
ids, and a mouse-hover tooltip on each item (icon, colored name, buy/sell
price, discount, sales/day). Click an item's **icon** to open its page on
[Undermine Exchange](https://undermine.exchange/) filtered to your sell
realm, in a new tab, so you can eyeball an independent price history next to
the inferred one — the link lives on the icon rather than inside the tooltip,
since the tooltip follows your cursor and a link inside it would be
unreachable.

The variant column shows `ilvl NNN` when the listing's bonus-list data
includes an item-level modifier *and* that value is plausible relative to the
item's own catalog level — otherwise it falls back to a bonus-count summary
(the raw `b:.../m:...` string is always available on hover). That guard exists
because the modifier isn't officially documented by Blizzard and produced
nonsense for items outside the modern scaling system (a classic wand once
showed "ilvl 1112" against a real level of ~35). **This smarter display is
dashboard-only, by design** — `snipe_check.py`'s terminal output intentionally
keeps printing the raw variant string; there's no shared formatting layer
between the CLI and the web UI.

Names/icons/quality/catalog-level are resolved via Blizzard's static item/pet
API and cached locally (`data/item_names.json`) — only genuinely new ids make
a network call. The table is sortable/filterable, shows the same NOTE the CLI
prints, and has freshness indicators so a stalled scheduled pipeline is
visible instead of hidden behind a page that still looks "live." It's
**read-only**: it doesn't run the pipeline itself, so `run_cycle.py` (or the
`AHSnipePipeline` scheduled task) still has to be producing fresh `data/`
files for it to show anything current — the dashboard and the scheduled task
are fully decoupled, so the task keeps collecting on its own hourly cadence
whether or not the dashboard is even running, and the dashboard just reflects
whatever's newest on disk each time it polls.

It's a local, single-user tool for now — no login, no accounts, no hosting.
`Dockerfile`/`.dockerignore` package the dashboard (not the collection
pipeline) into a container for when you want to run it somewhere other than
bare `python dashboard.py`:

```
docker build -t ah-dashboard .
docker run -p 8000:8000 -v ${PWD}/data:/app/data ah-dashboard
```

`data/` and `.env` stay off the image (gitignored, host-local) and get
mounted in at run time. This is a minimal image for local experimentation,
not a hardened production deploy.

## How inference works — and its limits

- `time_left` buckets: SHORT <30m, MEDIUM 30m–2h, LONG 2–12h, VERY_LONG 12–48h.
  An auction that vanishes while LONG/VERY_LONG, within a gap shorter than the
  bucket's minimum remaining time, **cannot have expired** → sold or cancelled.
- **Cancel–relist:** an identical (item, bonuses, buyout, qty) listing
  reappearing under a new auction id in the same interval → `likely_relisted`,
  excluded from sales.
- **Bid-only auctions** can't be insta-bought → excluded.
- **Known blind spot:** a cancel *without* a relist is indistinguishable from a
  sale. Every AH data service shares some version of this problem; the protocol
  below measures how big the noise floor actually is. Sellers who cancel mostly
  relist (that's why they cancelled), so the hypothesis is that the residual
  noise is small — verify, don't assume.

## Verification protocol (the actual point of the weekend)

From your own characters, post distinctive cheap items with a buyout, 48h
duration, then check what the pipeline says:

1. **Cancel two** at noted times, don't repost → these will show up as
   `inferred_sale`. This is the false-positive class; note it happened.
2. **Cancel one and instantly repost** at the same price → should classify as
   `likely_relisted`.
3. **Post one at 12h duration and let it expire** → must NOT appear as
   `inferred_sale`.
4. **Have a guildmate / second account buy one** → must appear as
   `inferred_sale` at the right price.

Then sanity-check scale: pick a famously liquid item and compare its `per_day`
against TSM's regional sale rate. Same order of magnitude = signal is real.

## Roadmap once the signal validates

Warbands changed the game: gold is account-wide and unsoulbound BoEs move
between realms via the warband bank, but the gear AH is still per realm — cheap
listings rot on low-pop realms while hub realms pay full price. So the plan:

Cross-realm snipe engine (scan every EU realm's listings, validate against
*sold* prices on your chosen high-pop sell realms, net of the 5% AH cut) →
region commodity feed (`/data/wow/auctions/commodities`) → appearance-scarcity
layer (ItemModifiedAppearance mappings via wago.tools + the static item API —
whether it still needs a per-item transferability flag is open, see CLAUDE.md
Phase 3) → deal score with buy-realm → sell-realm routing → Discord webhook
alerts (first paid feature) → web dashboard.

A local, single-user, read-only version of the web dashboard (`dashboard.py`)
was pulled forward and is already available (see above) — what's still ahead
is the appearance layer, deal score, Discord alerts, and turning the
dashboard into an actual hosted multi-user product (accounts, subscriptions).

## Notes

- The auction data belongs to Blizzard. Before charging anyone for anything
  built on it, read the current **Blizzard Developer API Terms of Use** — the
  free-addon / paid-external-service pattern (TSM, Raider.IO) is established,
  but confirm the fine print yourself.
- Keep `WoW`/`Warcraft` out of any product name; "for World of Warcraft" as a
  description is the accepted form.
