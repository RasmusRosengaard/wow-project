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
```

The loop polls every 10 min but only downloads when Blizzard publishes a new
hourly dump (`If-Modified-Since`), so it's ~6 tiny requests/hour against a
36,000/hour limit. The collector survives errors (retries with backoff on
429/5xx, skips garbled responses) and logs to the console plus a rotating
`data/logs/collector.log`. A big EU realm produces roughly 50–100k non-commodity
auctions per snapshot; zstd parquet keeps 48h of data in the tens of MB.
Prices are in **copper** (10,000 copper = 1 gold).

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

Region commodity feed (`/data/wow/auctions/commodities`) → appearance-scarcity
layer (ItemModifiedAppearance mappings via wago.tools + the static item API) →
deal score (discount vs. sold-percentile × liquidity × appearance rarity) →
Discord webhook alerts (first paid feature) → web dashboard.

## Notes

- The auction data belongs to Blizzard. Before charging anyone for anything
  built on it, read the current **Blizzard Developer API Terms of Use** — the
  free-addon / paid-external-service pattern (TSM, Raider.IO) is established,
  but confirm the fine print yourself.
- Keep `WoW`/`Warcraft` out of any product name; "for World of Warcraft" as a
  description is the accepted form.
