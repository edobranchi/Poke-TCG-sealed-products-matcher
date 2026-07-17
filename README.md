# Pokemon Sealed Price Collector

Tracks prices of Pokemon TCG sealed products (booster boxes, ETBs, tins,
blisters...) in both USD and EUR, and packages them into a small SQLite file
that my collection app syncs daily.

I built this because no free API covers sealed products in both currencies.
What does exist:

- [tcgcsv.com](https://tcgcsv.com) republishes TCGplayer's full catalog and
  prices as daily JSON dumps (USD)
- Cardmarket publishes its price guide and product list as
  [public downloads](https://help.cardmarket.com/en/Downloads) (EUR)

The catch: the two sites use completely unrelated product ids, so linking
"Surging Sparks Booster Box" on one to the other has to be done by comparing
names. Names disagree constantly ("3 Pack Blisters [Zapdos]" vs
": Zapdos 3-Pack Blister"), which is where most of the interesting code lives.

## How it works

`build_sealed_db.py` runs once a day:

1. fetches the TCGplayer catalog, keeps sealed single retail units only
   (no distributor cases, no "set of 2" bundles - those are sliced differently
   on each marketplace and were a constant source of wrong matches)
2. fetches Cardmarket's exports
3. matches Cardmarket products onto TCGplayer ones by normalized name
   similarity, set-scoped, with category compatibility and a bunch of guard
   rails (a 1st edition vintage box must never inherit the unlimited print's
   price - that would be wrong by 20x)
4. appends today's prices to a private history db and computes 7/30-day
   changes from it
5. writes `out/sealed_prices.db` (about 800 KB: sets, products, latest prices
   in both currencies) plus a `meta.json` with version and checksum

Ambiguous matches stay unmatched on purpose. A missing EUR price is annoying;
a wrong one is worse.

`collector_app.py` wraps the pipeline in a scheduler and a local web console:

- **Triage** - products the collector has never seen stay unpublished until I
  approve them here (with the matcher's proposal preloaded, one click for a
  whole new set)
- **Catalog** - browse what's published, filter by set/type/name
- **Divergence** - products where USD and EUR disagree by more than ~3.5x,
  which almost always means a wrong match
- **Decisions** - everything decided so far, with undo and a yaml export
- **Rematch** - paste a Cardmarket id or URL onto any product when the
  matcher couldn't figure it out

`validate_db.py` checks every build (referential integrity, price sanity,
coverage floors on the products people actually look up) and blocks
publishing on failure.

## Running it

```bash
pip3 install -r requirements.txt

# quick test - first 10 sets only, takes ~30s
python3 build_sealed_db.py --limit-groups 10 --out-dir /tmp/test --state /tmp/test_state.db

# full run, ~5 minutes (throttled to be polite to tcgcsv)
python3 build_sealed_db.py

# check the result
python3 validate_db.py

# console at http://localhost:8811
python3 collector_app.py

# tests
python3 -m unittest discover -s tests
```

## Files

| file | what |
|---|---|
| `build_sealed_db.py` | the pipeline |
| `match_cardmarket.py` | the name matcher |
| `cm_overrides.yaml` | hand-verified match corrections (a few hundred by now, mostly vintage) |
| `collector_app.py` | scheduler + web console |
| `validate_db.py` | pre-publish checks |
| `collector_state.db` | price history + all console decisions. **Not in git - back it up.** |
| `out/sealed_prices.db` | the published output |

## Notes

- Prices used: TCGplayer `marketPrice` + `lowPrice`, Cardmarket `trend` +
  `avg30` + `low`. Cardmarket's 1/7/30-day averages are often null for sealed
  products, `trend` is the reliable one.
- Presale flags and release dates come from TCGplayer and clear automatically,
  since the output is rebuilt from the live data every run.
- US retailer exclusives (Costco bundles etc.) are kept but flagged - they
  have no European market, so they're never matched to Cardmarket.
- The whole thing is throttled and cache-friendly toward its sources: one
  catalog sweep per day, ~2 requests per set, plus two file downloads from
  Cardmarket. Be nice to free data sources.

## Disclaimer

Fan project, not affiliated with or endorsed by The Pokémon Company, Nintendo,
TCGplayer or Cardmarket. No card images or game content included, just price
numbers from publicly available data. MIT licensed.
