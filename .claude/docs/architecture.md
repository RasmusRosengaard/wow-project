# Architecture, data layout and Blizzard API facts

Where data lives on disk, how it flows, and the API constraints that shape all
of it. Trust the API facts here rather than guessing.

## Architecture & data layout


```
Blizzard API ──> data/snapshots/{cr_id}/{epoch_ts}.parquet   (sell realms; only the
                        │                                      LATEST is kept automatically
                        │                                      since 2026-07-25, see
                        │                                      collect_all.py's module docstring)
                        v
                 snipe_check.find_snipes() reads it directly -- no diffing needed,
                 pricing is the sell realm's own current cheapest live listing

data/state/{cr_id}.json  — Last-Modified cursor for the sell-realm collector

Blizzard API ──> data/listings/{cr_id}.parquet   (region scanner, ALL EU realms;
                                                    latest sweep only, overwritten,
                                                    no history — buy side)

data/state/sweep_publish.json — when Blizzard actually published each realm's
                                dump, recorded by every sweep (2026-08-06).
                                Diagnostics only; nothing on the pricing path
                                reads it. See below.
```

### `data/state/sweep_publish.json`

`{cr_id (str): {last_modified, published_ts, first_seen_ts}}`, written once per
sweep by `scan_region.sweep()`, read by `scan_region.load_publish_state()`.

`published_ts` is Blizzard's own publish moment (the dump's `Last-Modified`,
parsed); `first_seen_ts` is the sweep that **first** observed that value, so
`first_seen_ts - published_ts` is our detection lag for that realm.
`first_seen_ts` deliberately does not move while the dump is unchanged —
otherwise it would measure our poll cadence rather than the lag.

Added because the buy side previously threw the `Last-Modified` header away
(`scan_one()` fetches unconditionally), which made a sniper-list alert
timestamp ambiguous: a listing appearing for the first time and an old listing
merely becoming *newly eligible* look identical from outside. Three things
produce the latter — the 4h `watchlist.NOTIFY_COOLDOWN_SECONDS` expiring, the
6h `tsm.REFRESH_INTERVAL_SECONDS` sale-average refresh, and NameCache/
appearance entries converging via `collect_all.py`'s prewarms. This file is
what tells them apart.

It is also the raw material for the **per-realm learned publish offset** that
`dashboard.py`'s `TIGHT_WINDOW_*` comment already names as the likely endgame:
one hand-aimed window cannot fit every realm's own offset, and a slot that
re-phased once (2026-08-05, Draenor :18-:26 -> :38-:48) will re-phase again.
Note the tight window only helps the ~36 deep-collected sell realms; the
sniper-list feed is region-sweep-only and gets no benefit from it.

**Manual/ad-hoc path only** (diff_snapshots.py is no longer run automatically): a
human who wants the sale-classification signal first accumulates real history
themselves (e.g. `fetch_snapshot.py --loop` run locally, which is unaffected by
collect_all.py's prune-to-latest), then runs `diff_snapshots.py --cr-id X` by
hand to produce `data/events/{cr_id}.parquet` (derived; recomputed from
scratch each run — always safe to delete), which `analyze.py`'s DuckDB views
(`snaps`, `ev`, `sales`, `span`) then read. `analyze.connect()` works fine
with no events file too — `ev`/`sales` just come back empty.

Snapshot schema is `SCHEMA` in `fetch_snapshot.py`; event schema is
`EVENT_SCHEMA` in `diff_snapshots.py`; listing schema is `LISTING_SCHEMA` in
`scan_region.py`. Changing any must handle previously written files
(regenerate, or read with `union_by_name`) — globs assume uniform schema.


## Blizzard API facts (trust these, don't guess)


- OAuth: `POST https://oauth.battle.net/token`, HTTP basic auth with client
  id/secret, `grant_type=client_credentials`. Token lasts ~24h; cached in-process.
- Base `https://eu.api.blizzard.com`; namespace param `dynamic-eu` (auctions,
  realms) or `static-eu` (items, appearances, media).
- Non-commodity AH: `GET /data/wow/connected-realm/{crId}/auctions`. Updates
  roughly hourly, at no fixed clock time. Honor `If-Modified-Since` /
  `Last-Modified` (implemented; the Last-Modified timestamp is the canonical
  `snapshot_ts`).
- Commodities (region-wide): `GET /data/wow/auctions/commodities` —
  **Phase 2, explicitly out of scope**, not implemented.
- Realm lookup: `GET /data/wow/search/connected-realm?realms.slug={slug}`.
- Item class/subclass: `GET /data/wow/item-class/index` + per-class
  `itemSubclasses` — confirmed live 2026-07-24: 2=Weapon, 4=Armor,
  1=Container, 19=Profession, 20=Housing, 17=Battle Pets, 12=Quest,
  15=Miscellaneous with subclass 5=Mount. 9=Recipe (confirmed live
  2026-07-28, distinct from Profession's 19 — see `snipe_check.py`'s
  `CLASS_BUCKET_RULES` row below).
- Rate limit 36,000 req/h, 100 req/s. Headroom is not an invitation — stay
  polite. `collect_all._prewarm_item_base_levels()` and `dashboard._build_rows()`'s
  `NameCache.ensure_many()`/`.ensure_icons_many()` calls are where this
  pipeline makes bulk Blizzard calls; the first is explicitly capped per
  call (see its file-table entry above).
- `time_left` buckets: SHORT <30m, MEDIUM 30m–2h, LONG 2–12h, VERY_LONG 12–48h.
  Players list at 12/24/48h durations.
- Prices are **copper** (10,000 = 1 gold) end to end; only format as gold at
  display boundaries.
- Battle pets: item_id 82800 cages + `pet_species_id` / `pet_quality_id` /
  `pet_level` fields. `bonus_key`/`market_key` are empty for pets — matching
  uses the pet identity fields instead.
- `auction_id` is stable for a listing's lifetime → it is the diff key. Seller
  identity is never exposed by the API.
