#!/usr/bin/env python3
"""Build sealed_prices.db - Pokemon sealed product prices from two sources.

One run does the whole thing:
  - pull the TCGplayer catalog + prices from tcgcsv.com (category 3 = Pokemon)
  - pull Cardmarket's public price guide exports (EUR prices)
  - link Cardmarket products onto TCGplayer ones by name (match_cardmarket.py)
  - append today's prices to collector_state.db (full private history, also
    used to compute the 7/30 day change columns)
  - write the small output db with latest prices only, plus meta.json

Products never seen before are held back ("pending") until someone approves
them in the web console - see collector_app.py. The output file is written to
a temp file and renamed, so a failed run never corrupts the previous one.

Usage:
  python3 build_sealed_db.py [--out-dir out] [--state collector_state.db]
                             [--limit-groups N] [--verbose]
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import unicodedata

import requests
import yaml

from match_cardmarket import match_group

log = logging.getLogger("collector")

TCGCSV_BASE = "https://tcgcsv.com/tcgplayer/3"  # category 3 = Pokemon
TCGDEX_SETS_URL = "https://api.tcgdex.net/v2/en/sets"
LOGO_OVERRIDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo_overrides.yaml")
CM_PRODUCTS_URL = ("https://downloads.s3.cardmarket.com/productCatalog/"
                   "productList/products_nonsingles_6.json")
CM_PRICES_URL = ("https://downloads.s3.cardmarket.com/productCatalog/"
                 "priceGuide/price_guide_6.json")

REQUEST_DELAY = 0.25  # tcgcsv asks to keep it polite
TIMEOUT = 60
RETRIES = 3
SCHEMA_VERSION = 1
CM_CARRY_DAYS = 30  # how long to reuse old CM prices if their server is down
USER_AGENT = "pokescandex-sealed-collector/1.0"

# Things that pass the "no card number" test but that we don't want anyway.
EXCLUDE_NAMES = re.compile(r"code card|jumbo|oversize|world championship", re.IGNORECASE)
EXCLUDE_GROUPS = {"World Championship Decks"}

# We only track single retail units - one booster box, one tin, one ETB.
# Distributor cases, "set of 2" bundles etc. are sliced differently on each
# marketplace and were a constant source of wrong matches.
# ("Case File" is a Detective Pikachu product, not a case of anything.)
MULTI_UNIT = re.compile(
    r"set of \d+|half booster|"
    r"(deck|tin|blister|bundle|collection|kit|box) display\b|"
    r"\bcases?\b|\bcartons?\b",
    re.IGNORECASE)
NOT_ACTUALLY_MULTI = re.compile(r"case file|on the case", re.IGNORECASE)

# US big-box retailer repacks (Costco bundles etc). Kept in the catalog but
# flagged: they have no European market, so no Cardmarket matching, and the
# app hides them when showing EUR prices. A plain "(Exclusive)" without a
# retailer name means Pokemon Center, which does sell in Europe.
US_RETAILERS = re.compile(
    r"costco|target|walmart|wal-mart|sam'?s club|gamestop|best buy|meijer|"
    r"kroger|7-eleven|general mills|toys ?.?r.? ?us|dollar general|walgreens|\bcvs\b",
    re.IGNORECASE)

# First matching pattern wins, so order matters ("Booster Bundle" has to be
# checked before "Booster Box").
TYPE_PATTERNS = [
    ("etb", re.compile(r"elite trainer box", re.I)),
    ("booster_bundle", re.compile(r"booster bundle", re.I)),
    ("booster_box", re.compile(r"booster (box|display)", re.I)),
    ("blister", re.compile(r"blister|checklane", re.I)),
    ("booster_pack", re.compile(r"booster pack|sleeved booster|fun pack|packs?$", re.I)),
    ("tin", re.compile(r"\btins?\b", re.I)),
    ("deck", re.compile(r"\bdecks?\b|battle arena|league battle|trainer kit|battle academy", re.I)),
    ("collection", re.compile(
        r"collection|premium|box set|\bbundle\b|\bbox\b|\bcase\b|build & battle|trading card game classic", re.I)),
]

CM_KEEP_CATEGORIES = {
    "Pokémon Display", "Pokémon Booster", "Pokémon Box Set",
    "Pokémon Elite Trainer Boxes", "Pokémon Tins", "Pokémon Theme Deck",
    "Pokémon Blisters", "Pokémon Trainer Kits",
}

OUTPUT_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sealed_sets (
  group_id          INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  abbreviation      TEXT,
  published_on      TEXT,
  tcgdex_logo_url   TEXT
);
CREATE TABLE sealed_products (
  product_id    INTEGER PRIMARY KEY,
  group_id      INTEGER NOT NULL,
  name          TEXT NOT NULL,
  name_lower    TEXT NOT NULL,
  image_url     TEXT,
  url           TEXT,
  product_type  TEXT NOT NULL,
  is_presale    INTEGER NOT NULL DEFAULT 0,
  released_on   TEXT,
  us_exclusive  INTEGER NOT NULL DEFAULT 0,
  cardmarket_id INTEGER
);
CREATE INDEX idx_products_group ON sealed_products(group_id);
CREATE TABLE sealed_latest_prices (
  product_id        INTEGER PRIMARY KEY,
  price_date        TEXT NOT NULL,
  tcgplayer_market  REAL,
  tcgplayer_low     REAL,
  tp_change_7d      REAL,
  tp_change_30d     REAL,
  cm_price_date     TEXT,
  cardmarket_trend  REAL,
  cardmarket_avg30  REAL,
  cardmarket_low    REAL,
  cm_change_7d      REAL,
  cm_change_30d     REAL
);
"""

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
  product_id INTEGER NOT NULL,
  date       TEXT NOT NULL,
  tp_market  REAL, tp_low REAL,
  cm_trend   REAL, cm_avg30 REAL, cm_low REAL,
  cm_date    TEXT,
  PRIMARY KEY (product_id, date)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  stage TEXT,
  message TEXT,
  products INTEGER, priced INTEGER, cm_matched INTEGER,
  published_version INTEGER,
  pending INTEGER
);
-- decisions made in the web console. cm_id: NULL = let the matcher decide,
-- a positive id = forced match, -1 = never match this product.
-- name/group_name are stored here too because a dropped product doesn't
-- exist anywhere else to look up.
CREATE TABLE IF NOT EXISTS product_decisions (
  product_id INTEGER PRIMARY KEY,
  decision   TEXT NOT NULL,
  cm_id      INTEGER,
  decided_at TEXT NOT NULL,
  name       TEXT,
  group_name TEXT
);
-- new products waiting for a human decision, rebuilt every run
CREATE TABLE IF NOT EXISTS pending_products (
  product_id      INTEGER PRIMARY KEY,
  first_seen      TEXT NOT NULL,
  name            TEXT NOT NULL,
  group_name      TEXT,
  product_type    TEXT,
  image_url       TEXT,
  url             TEXT,
  us_exclusive    INTEGER,
  heuristic_cm    INTEGER,
  heuristic_score REAL,
  candidates      TEXT
);
CREATE TABLE IF NOT EXISTS divergence_ack (
  product_id INTEGER PRIMARY KEY,
  acked_at   TEXT NOT NULL
);
-- snapshot of Cardmarket's full non-singles list, for the console's
-- lookup/search when manually matching
CREATE TABLE IF NOT EXISTS cm_catalog (
  id_product   INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  category     TEXT,
  id_expansion INTEGER,
  updated_at   TEXT NOT NULL
);
"""


def fetch_json(session, url, delay=REQUEST_DELAY):
    last_error = None
    for attempt in range(RETRIES):
        try:
            time.sleep(delay * (2 ** attempt) if attempt else delay)
            response = session.get(url, timeout=TIMEOUT)
            if response.status_code == 200:
                return response.json()
            last_error = f"HTTP {response.status_code}"
        except Exception as e:
            last_error = str(e)
    raise RuntimeError(f"giving up on {url} after {RETRIES} tries: {last_error}")


def is_sealed(product):
    """Sealed products have no card number in their extended data."""
    if EXCLUDE_NAMES.search(product.get("name", "")):
        return False
    return not any(e.get("name") == "Number" for e in product.get("extendedData") or [])


def is_multi_unit(name):
    return bool(MULTI_UNIT.search(name)) and not NOT_ACTUALLY_MULTI.search(name)


def is_us_exclusive(name):
    return bool(US_RETAILERS.search(name))


def classify(name):
    for product_type, pattern in TYPE_PATTERNS:
        if pattern.search(name):
            return product_type
    return None


# ---------------------------------------------------------------- fetching

def fetch_tcgplayer(session, limit_groups=None):
    groups = fetch_json(session, f"{TCGCSV_BASE}/groups")["results"]
    if limit_groups:
        groups = groups[:limit_groups]
    log.info("tcgcsv: %d groups", len(groups))

    sets, products, prices, dropped = [], [], {}, []
    for i, group in enumerate(groups):
        if group["name"] in EXCLUDE_GROUPS:
            continue
        group_id = group["groupId"]
        raw = fetch_json(session, f"{TCGCSV_BASE}/{group_id}/products")["results"]
        kept = []
        for product in raw:
            if not is_sealed(product):
                continue
            if is_multi_unit(product["name"]):
                dropped.append((group_id, product["productId"], "[multi-unit] " + product["name"]))
                continue
            product_type = classify(product["name"])
            if product_type is None:
                dropped.append((group_id, product["productId"], product["name"]))
                continue
            presale = product.get("presaleInfo") or {}
            kept.append({
                "productId": product["productId"], "groupId": group_id,
                "name": product["name"], "imageUrl": product.get("imageUrl"),
                "url": product.get("url"), "product_type": product_type,
                "is_presale": 1 if presale.get("isPresale") else 0,
                "released_on": presale.get("releasedOn"),
                "us_exclusive": 1 if is_us_exclusive(product["name"]) else 0,
            })
        if not kept:
            continue
        sets.append({"group_id": group_id, "name": group["name"],
                     "abbreviation": group.get("abbreviation"),
                     "published_on": group.get("publishedOn")})
        products.extend(kept)
        kept_ids = {p["productId"] for p in kept}
        for price in fetch_json(session, f"{TCGCSV_BASE}/{group_id}/prices")["results"]:
            # sealed products only have the "Normal" subtype
            if price["productId"] in kept_ids and price.get("subTypeName") == "Normal":
                prices[price["productId"]] = (price.get("marketPrice"), price.get("lowPrice"))
        if (i + 1) % 25 == 0:
            log.info("tcgcsv: %d/%d groups, %d sealed products", i + 1, len(groups), len(products))

    unpriced = [p["productId"] for p in products if p["productId"] not in prices]
    log.info("tcgcsv done: %d sets, %d products, %d priced, %d unpriced, %d dropped-unclassified",
             len(sets), len(products), len(prices), len(unpriced), len(dropped))
    return sets, products, prices, dropped, unpriced


def fetch_cardmarket(session):
    catalog = fetch_json(session, CM_PRODUCTS_URL, delay=0)
    guide = fetch_json(session, CM_PRICES_URL, delay=0)
    everything = catalog["products"]
    matchable = [p for p in everything if p["categoryName"] in CM_KEEP_CATEGORIES]
    prices = {row["idProduct"]: row for row in guide["priceGuides"]}
    guide_date = (guide.get("createdAt") or "")[:10]
    log.info("cardmarket done: %d matchable products (of %d), %d price rows, guide date %s",
             len(matchable), len(everything), len(prices), guide_date)
    return matchable, prices, guide_date, everything


def save_cm_catalog(state_db, everything, when):
    with state_db:
        state_db.execute("DELETE FROM cm_catalog")
        state_db.executemany(
            "INSERT INTO cm_catalog VALUES (?,?,?,?,?)",
            [(p["idProduct"], p["name"], p.get("categoryName"),
              p.get("idExpansion"), when) for p in everything])


# ---------------------------------------------------------------- TCGDex logo lookup

def _norm_name(name):
    """Lowercase, strip accents and punctuation, drop the word 'and'
    (so 'Black & White' and 'Black and White' both normalise to
    'black white'), collapse whitespace."""
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r"[^\w\s]", "", name.lower())   # strips & → nothing
    name = re.sub(r"\band\b", "", name)            # 'and' → same as '&'
    return re.sub(r"\s+", " ", name).strip()


def fetch_tcgdex_logo_map(session):
    """Returns {normalized_set_name: logo_url} from TCGDex /sets.

    logo_url is the base URL without extension, e.g.
    'https://assets.tcgdex.net/en/swsh/swsh3/logo'.
    The app appends '.png' to load it via CachedNetworkImage.
    Returns an empty dict (non-fatal) if TCGDex is unreachable.
    """
    try:
        resp = session.get(TCGDEX_SETS_URL, timeout=30)
        if resp.status_code != 200:
            log.warning("tcgdex sets: HTTP %d — running without logos", resp.status_code)
            return {}
        out = {}
        for s in resp.json():
            key = _norm_name(s.get("name", ""))
            if key and s.get("logo"):
                out[key] = s["logo"]
        log.info("tcgdex logos: %d sets indexed", len(out))
        return out
    except Exception as e:
        log.warning("tcgdex logo fetch failed (%s) — running without logos", e)
        return {}


# Prefixes TCGCSV adds that TCGDex omits:
#   "SV08: "  "SWSH12: "  "ME05: "  "ME: "  "SV: "
_SERIES_PREFIX = re.compile(r"^[A-Za-z]+\d*:\s*")
#   "SM - "  "XY - "  "SWSH - "
_DASH_PREFIX = re.compile(r"^[A-Z]+\s+-\s+")
#   "Sword & Shield Base Set" → "Sword & Shield"
_BASE_SET_SUFFIX = re.compile(r"\s+base set$", re.IGNORECASE)


def load_logo_overrides():
    """Load manual logo assignments from logo_overrides.yaml (if present)."""
    if not os.path.exists(LOGO_OVERRIDES_FILE):
        return {}
    try:
        with open(LOGO_OVERRIDES_FILE) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        log.warning("logo_overrides load failed: %s", e)
        return {}


def _find_logo(name, logo_map, logo_overrides=None):
    """Multi-stage logo lookup against the TCGDex name map.

    Stages (in order, first hit wins):
      0. Manual override from logo_overrides.yaml (exact group name match)
      1. Full name — exact normalised match, then fuzzy (>=0.85)
      2. Strip 'CODE: ' series prefix  (SV08: / SWSH12: / ME05: / SV: …)
      3. Strip 'SERIES - ' dash prefix (SM - / XY - / …)
      4. Strip ' Base Set' suffix      (Sword & Shield Base Set → …)

    The 0.85 fuzzy cutoff is strict on purpose: a wrong logo is worse
    than no logo (the app falls back to the era-colour icon).
    """
    if logo_overrides and name in logo_overrides:
        return logo_overrides[name]
    if not logo_map:
        return None

    def _try(n):
        key = _norm_name(n)
        if key in logo_map:
            return logo_map[key]
        m = difflib.get_close_matches(key, logo_map.keys(), n=1, cutoff=0.85)
        return logo_map[m[0]] if m else None

    result = _try(name)
    if result:
        return result

    # Strip "SV08: " / "SWSH12: " / "ME05: " / "SV: " style prefixes.
    stripped = _SERIES_PREFIX.sub("", name)
    if stripped != name:
        result = _try(stripped)
        if result:
            return result

    # Strip "SM - " / "XY - " dash prefixes.
    stripped = _DASH_PREFIX.sub("", name)
    if stripped != name:
        result = _try(stripped)
        if result:
            return result

    # Strip " Base Set" suffix alone.
    stripped = _BASE_SET_SUFFIX.sub("", name)
    if stripped != name:
        result = _try(stripped)
        if result:
            return result

    # Strip BOTH series prefix AND base-set suffix — handles
    # "SV01: Scarlet & Violet Base Set" → "Scarlet & Violet".
    stripped = _BASE_SET_SUFFIX.sub("", _SERIES_PREFIX.sub("", name))
    if stripped != name:
        result = _try(stripped)
        if result:
            return result

    return None


# ---------------------------------------------------------------- matching

def match_all(sets, products, cm_products, overrides):
    products_by_group = {}
    for p in products:
        if p["us_exclusive"]:
            continue  # no European market for these, don't even try
        products_by_group.setdefault(p["groupId"], []).append(p)
    matched, review, unmatched = {}, [], []
    for s in sets:
        report = match_group(s["name"], products_by_group.get(s["group_id"], []),
                             cm_products, overrides)
        matched.update(report.matched)
        review.extend(report.review)
        unmatched.extend(report.unmatched)
    log.info("match done: %d/%d matched (%.0f%%), %d flagged for review, %d unmatched",
             len(matched), len(products), 100 * len(matched) / max(1, len(products)),
             len(review), len(unmatched))
    return matched, review, unmatched


# ---------------------------------------------------------------- building

def percent_change(current, then):
    if current is None or then is None or then <= 0:
        return None
    return round((current - then) / then * 100.0, 2)


def price_around(state_db, product_id, target_date, column):
    """Closest recorded price within 2 days of a date. NULL-safe: a gap in
    the history (server was down) gives no value, never a wrong one."""
    row = state_db.execute(
        f"SELECT {column} FROM price_history WHERE product_id=? AND {column} IS NOT NULL "
        "AND date BETWEEN date(?,'-2 days') AND date(?,'+2 days') "
        "ORDER BY ABS(JULIANDAY(date)-JULIANDAY(?)) LIMIT 1",
        (product_id, target_date, target_date, target_date)).fetchone()
    return row[0] if row else None


def build_output(state_db, out_dir, today, sets, products, tp_prices,
                 matched, cm_prices, cm_date, cm_available):
    # today's snapshot goes into the private history first
    with state_db:
        for product in products:
            pid = product["productId"]
            tp_market, tp_low = tp_prices.get(pid, (None, None))
            cm_trend = cm_avg30 = cm_low = None
            row_cm_date = None
            if cm_available and pid in matched:
                guide_row = cm_prices.get(matched[pid][0])
                if guide_row:
                    cm_trend = guide_row.get("trend")
                    cm_avg30 = guide_row.get("avg30")
                    cm_low = guide_row.get("low")
                    row_cm_date = cm_date
            if not cm_available:
                # Cardmarket down: reuse the last known prices for up to a
                # month, so a one-day outage doesn't look like a delisting
                previous = state_db.execute(
                    "SELECT cm_trend, cm_avg30, cm_low, cm_date FROM price_history "
                    "WHERE product_id=? AND cm_trend IS NOT NULL "
                    "AND date >= date(?, ?) ORDER BY date DESC LIMIT 1",
                    (pid, today, f"-{CM_CARRY_DAYS} days")).fetchone()
                if previous:
                    cm_trend, cm_avg30, cm_low, row_cm_date = previous
            state_db.execute(
                "INSERT OR REPLACE INTO price_history VALUES (?,?,?,?,?,?,?,?)",
                (pid, today, tp_market, tp_low, cm_trend, cm_avg30, cm_low, row_cm_date))

    week_ago = (dt.date.fromisoformat(today) - dt.timedelta(days=7)).isoformat()
    month_ago = (dt.date.fromisoformat(today) - dt.timedelta(days=30)).isoformat()

    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "sealed_prices.db")
    tmp_path = db_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    out = sqlite3.connect(tmp_path)
    out.executescript(OUTPUT_SCHEMA)
    out.executemany(
        "INSERT INTO sealed_sets VALUES (:group_id,:name,:abbreviation,:published_on,:tcgdex_logo_url)",
        sets)

    priced_count = 0
    for product in products:
        pid = product["productId"]
        cm_id = matched.get(pid, (None,))[0]
        out.execute(
            "INSERT INTO sealed_products VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, product["groupId"], product["name"], product["name"].lower(),
             product["imageUrl"], product["url"], product["product_type"],
             product["is_presale"], product["released_on"],
             product["us_exclusive"], cm_id))
        row = state_db.execute(
            "SELECT tp_market, tp_low, cm_trend, cm_avg30, cm_low, cm_date "
            "FROM price_history WHERE product_id=? AND date=?", (pid, today)).fetchone()
        if not row:
            continue
        tp_market, tp_low, cm_trend, cm_avg30, cm_low, row_cm_date = row
        if tp_market is None and cm_trend is None:
            continue
        priced_count += 1
        out.execute(
            "INSERT INTO sealed_latest_prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, today, tp_market, tp_low,
             percent_change(tp_market, price_around(state_db, pid, week_ago, "tp_market")),
             percent_change(tp_market, price_around(state_db, pid, month_ago, "tp_market")),
             row_cm_date, cm_trend, cm_avg30, cm_low,
             percent_change(cm_trend, price_around(state_db, pid, week_ago, "cm_trend")),
             percent_change(cm_trend, price_around(state_db, pid, month_ago, "cm_trend"))))

    # version is the date plus a counter for same-day rebuilds
    version = int(today.replace("-", "")) * 100 + 1
    meta_path = os.path.join(out_dir, "meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                previous_version = json.load(f)["version"]
            if previous_version // 100 == version // 100:
                version = previous_version + 1
        except Exception:
            pass
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_groups": str(len(sets)),
        "source_products": str(len(products)),
        "cm_matched_products": str(len(matched)),
    }
    out.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    out.execute(f"PRAGMA user_version = {version}")
    out.commit()
    out.execute("VACUUM")
    out.close()
    os.replace(tmp_path, db_path)

    sha256 = hashlib.sha256(open(db_path, "rb").read()).hexdigest()
    with open(meta_path, "w") as f:
        json.dump({"version": version, "schemaVersion": SCHEMA_VERSION, "sha256": sha256,
                   "sizeBytes": os.path.getsize(db_path),
                   "generatedAt": meta["generated_at"]}, f, indent=1)
    log.info("build done: %s — %d sets, %d products, %d priced, version=%d, %.0f KB",
             db_path, len(sets), len(products), priced_count, version,
             os.path.getsize(db_path) / 1024)
    return version, priced_count


# ---------------------------------------------------------------- curation gate

def load_decisions(state_db, out_dir, products):
    """Return {product_id: (decision, cm_id)} from the console's decisions.

    On the very first run there are no decisions yet - everything already in
    the published db gets grandfathered as 'keep' (or everything currently
    fetched, on a completely fresh setup)."""
    rows = state_db.execute(
        "SELECT product_id, decision, cm_id FROM product_decisions").fetchall()
    if rows:
        return {pid: (decision, cm_id) for pid, decision, cm_id in rows}, False

    db_path = os.path.join(out_dir, "sealed_prices.db")
    if os.path.exists(db_path):
        published = sqlite3.connect(db_path)
        seed = published.execute(
            "SELECT p.product_id, p.name, s.name FROM sealed_products p "
            "JOIN sealed_sets s ON s.group_id=p.group_id").fetchall()
        published.close()
    else:
        seed = [(p["productId"], p.get("name"), None) for p in products]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with state_db:
        state_db.executemany(
            "INSERT OR IGNORE INTO product_decisions "
            "(product_id, decision, cm_id, decided_at, name, group_name) "
            "VALUES (?, 'keep', NULL, ?, ?, ?)",
            [(pid, now, name, group_name) for pid, name, group_name in seed])
    log.info("first run: grandfathered %d products as 'keep'", len(seed))
    return {pid: ("keep", None) for pid, _, _ in seed}, True


def save_pending(state_db, pending, group_names, match_info):
    """Refresh the pending list for the console, keeping first_seen dates."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with state_db:
        still_pending = [p["productId"] for p in pending]
        state_db.execute(
            "DELETE FROM pending_products WHERE product_id NOT IN (%s)"
            % (",".join("?" * len(still_pending)) or "-1"), still_pending)
        for product in pending:
            pid = product["productId"]
            candidates = match_info.get("candidates", {}).get(pid, [])
            proposal = match_info.get("matched", {}).get(pid)
            state_db.execute(
                "INSERT INTO pending_products VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(product_id) DO UPDATE SET name=excluded.name, "
                "group_name=excluded.group_name, product_type=excluded.product_type, "
                "image_url=excluded.image_url, url=excluded.url, "
                "us_exclusive=excluded.us_exclusive, heuristic_cm=excluded.heuristic_cm, "
                "heuristic_score=excluded.heuristic_score, candidates=excluded.candidates",
                (pid, now, product["name"], group_names.get(product["groupId"]),
                 product["product_type"], product["imageUrl"], product["url"],
                 product["us_exclusive"],
                 proposal[0] if proposal else None, proposal[1] if proposal else None,
                 json.dumps(candidates)))


# ---------------------------------------------------------------- the run

def run(out_dir="out", state_path="collector_state.db", limit_groups=None,
        overrides_path="cm_overrides.yaml"):
    """One full collector run. Returns (version, report). Raises on failure."""
    state_db = sqlite3.connect(state_path)
    state_db.execute("PRAGMA journal_mode=WAL")
    state_db.executescript(STATE_SCHEMA)
    for migration in ("ALTER TABLE runs ADD COLUMN pending INTEGER",
                      "ALTER TABLE product_decisions ADD COLUMN name TEXT",
                      "ALTER TABLE product_decisions ADD COLUMN group_name TEXT"):
        try:
            state_db.execute(migration)
        except sqlite3.OperationalError:
            pass  # column already there

    run_id = state_db.execute(
        "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
        (dt.datetime.now(dt.timezone.utc).isoformat(),)).lastrowid
    state_db.commit()

    stage = "init"
    try:
        overrides = {}
        if os.path.exists(overrides_path):
            with open(overrides_path) as f:
                overrides = yaml.safe_load(f) or {}

        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        today = dt.date.today().isoformat()

        stage = "tcgdex_logos"
        logo_map = fetch_tcgdex_logo_map(session)
        logo_overrides = load_logo_overrides()
        if logo_overrides:
            log.info("logo_overrides: %d manual assignment(s)", len(logo_overrides))

        stage = "tcgcsv"
        sets, products, tp_prices, dropped, unpriced = fetch_tcgplayer(session, limit_groups)

        # Enrich each set with a TCGDex logo URL (None when no match found).
        for s in sets:
            s["tcgdex_logo_url"] = _find_logo(s["name"], logo_map, logo_overrides)

        stage = "gate"
        decisions, _ = load_decisions(state_db, out_dir, products)
        approved, pending = [], []
        for product in products:
            decision = decisions.get(product["productId"])
            if decision is None:
                pending.append(product)
            elif decision[0] == "keep":
                approved.append(product)
            # dropped products just disappear
        # decisions made in the console act as overrides too, with priority
        overrides = dict(overrides)
        overrides["matches"] = dict(overrides.get("matches") or {})
        overrides["never"] = list(overrides.get("never") or [])
        for pid, (decision, cm_id) in decisions.items():
            if decision == "keep" and cm_id and cm_id > 0:
                overrides["matches"][pid] = cm_id
            elif decision == "keep" and cm_id == -1:
                overrides["never"].append(pid)
        group_names = {s["group_id"]: s["name"] for s in sets}
        approved_groups = {p["groupId"] for p in approved}
        sets = [s for s in sets if s["group_id"] in approved_groups]
        log.info("gate: %d approved, %d pending, %d dropped by decision",
                 len(approved), len(pending), len(products) - len(approved) - len(pending))

        stage = "cardmarket"
        cm_available = True
        try:
            cm_products, cm_prices, cm_date, cm_everything = fetch_cardmarket(session)
            save_cm_catalog(state_db, cm_everything, today)
        except RuntimeError as e:
            log.warning("cardmarket unavailable (%s), carrying old prices forward", e)
            cm_available, cm_products, cm_prices, cm_date = False, [], {}, None

        stage = "match"
        matched, review, unmatched = {}, [], []
        pending_match_info = {"matched": {}, "candidates": {}}
        if cm_available:
            matched, review, unmatched = match_all(sets, approved, cm_products, overrides)
            # also score the pending products so the console can show proposals
            if pending:
                pending_by_group = {}
                for p in pending:
                    pending_by_group.setdefault(p["groupId"], []).append(p)
                for group_id, group_products in pending_by_group.items():
                    report = match_group(group_names.get(group_id, ""),
                                         group_products, cm_products, overrides)
                    pending_match_info["matched"].update(report.matched)
                    pending_match_info["candidates"].update(report.candidates)

        stage = "build"
        version, priced_count = build_output(state_db, out_dir, today, sets, approved,
                                             tp_prices, matched, cm_prices, cm_date,
                                             cm_available)
        save_pending(state_db, pending, group_names, pending_match_info)

        state_db.execute(
            "UPDATE runs SET finished_at=?, status='ok', stage=?, products=?, priced=?, "
            "cm_matched=?, published_version=?, pending=? WHERE id=?",
            (dt.datetime.now(dt.timezone.utc).isoformat(), stage, len(approved),
             priced_count, len(matched), version, len(pending), run_id))
        state_db.commit()

        report = {"sets": len(sets), "products": len(approved), "priced": priced_count,
                  "cm_matched": len(matched), "pending": len(pending), "review": review,
                  "unmatched": unmatched, "dropped": dropped, "unpriced": unpriced}
        with open(os.path.join(out_dir, "report.json"), "w") as f:
            json.dump(report, f, indent=1)
        return version, report
    except Exception as e:
        state_db.execute(
            "UPDATE runs SET finished_at=?, status='failed', stage=?, message=? WHERE id=?",
            (dt.datetime.now(dt.timezone.utc).isoformat(), stage, str(e), run_id))
        state_db.commit()
        raise
    finally:
        state_db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--state", default="collector_state.db")
    parser.add_argument("--limit-groups", type=int, default=None,
                        help="only process the first N groups (quick test runs)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        version, report = run(args.out_dir, args.state, args.limit_groups)
    except Exception as e:
        log.error("run FAILED: %s", e)
        sys.exit(1)

    print(f"\npublished version {version}: {report['products']} products, "
          f"{report['priced']} priced, {report['cm_matched']} matched on Cardmarket, "
          f"{report['pending']} waiting for review")
    if report["unmatched"]:
        print(f"\n{len(report['unmatched'])} unmatched (best candidate shown):")
        for tp_id, name, candidates in report["unmatched"][:40]:
            best = f" | best: {candidates[0][0]} {candidates[0][2]!r}" if candidates else ""
            print(f"  {tp_id}  {name}{best}")


if __name__ == "__main__":
    main()
