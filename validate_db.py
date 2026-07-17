#!/usr/bin/env python3
"""Sanity-check a built sealed_prices.db before it goes anywhere.

FAIL means don't publish, warnings are for a human to look at. Exits 1 on any
FAIL. The most interesting check is the price divergence one at the bottom:
if the same product costs 5x more on one marketplace than the other, the
Cardmarket match is probably wrong.

Usage: python3 validate_db.py [--db out/sealed_prices.db] [--ratio-warn 3.5]
"""

import argparse
import datetime as dt
import sqlite3
import sys

ALLOWED_TYPES = {"booster_box", "etb", "booster_bundle", "booster_pack",
                 "tin", "collection", "deck", "blister"}

failures, warnings = [], []


def fail(msg):
    failures.append(msg)
    print(f"  FAIL  {msg}")


def warn(msg):
    warnings.append(msg)
    print(f"  warn  {msg}")


def ok(msg):
    print(f"  ok    {msg}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="out/sealed_prices.db")
    parser.add_argument("--ratio-warn", type=float, default=3.5)
    args = parser.parse_args()
    db = sqlite3.connect(args.db)

    print("== schema / meta ==")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    today = int(dt.date.today().strftime("%Y%m%d"))
    (ok if version // 100 == today else warn)(f"user_version {version} (today is {today})")
    meta = dict(db.execute("SELECT key, value FROM meta"))
    for key in ("schema_version", "generated_at", "source_groups", "source_products",
                "cm_matched_products"):
        (ok if key in meta else fail)(f"meta.{key} = {meta.get(key)}")
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("meta", "sealed_sets", "sealed_products", "sealed_latest_prices"):
        (ok if table in tables else fail)(f"table {table}")

    print("== referential integrity ==")
    count = db.execute("SELECT COUNT(*) FROM sealed_products p LEFT JOIN sealed_sets s "
                       "ON s.group_id=p.group_id WHERE s.group_id IS NULL").fetchone()[0]
    (ok if count == 0 else fail)(f"products pointing at a missing set: {count}")
    count = db.execute("SELECT COUNT(*) FROM sealed_latest_prices l LEFT JOIN sealed_products p "
                       "USING(product_id) WHERE p.product_id IS NULL").fetchone()[0]
    (ok if count == 0 else fail)(f"price rows for missing products: {count}")
    count = db.execute("SELECT COUNT(*) FROM sealed_sets s WHERE NOT EXISTS "
                       "(SELECT 1 FROM sealed_products p WHERE p.group_id=s.group_id)").fetchone()[0]
    (ok if count == 0 else fail)(f"sets with no products: {count}")

    print("== fields ==")
    count = db.execute("SELECT COUNT(*) FROM sealed_products "
                       "WHERE name_lower != lower(name)").fetchone()[0]
    (ok if count == 0 else fail)(f"name_lower out of sync: {count}")
    placeholders = ",".join("?" * len(ALLOWED_TYPES))
    count = db.execute(f"SELECT COUNT(*) FROM sealed_products WHERE product_type NOT IN "
                       f"({placeholders})", tuple(ALLOWED_TYPES)).fetchone()[0]
    (ok if count == 0 else fail)(f"unknown product types: {count}")
    count = db.execute("SELECT COUNT(*) FROM sealed_products "
                       "WHERE image_url IS NULL OR image_url=''").fetchone()[0]
    (ok if count == 0 else warn)(f"products without an image: {count}")
    count = db.execute("SELECT COUNT(*) FROM sealed_products WHERE image_url NOT LIKE "
                       "'https://tcgplayer-cdn.tcgplayer.com/product/%'").fetchone()[0]
    (ok if count == 0 else warn)(f"unexpected image urls: {count}")
    count = db.execute("SELECT COUNT(*) FROM sealed_products "
                       "WHERE url IS NULL OR url=''").fetchone()[0]
    (ok if count == 0 else warn)(f"products without a store link: {count}")
    duplicates = db.execute("SELECT name, group_id, COUNT(*) c FROM sealed_products "
                            "GROUP BY 1,2 HAVING c>1").fetchall()
    (ok if not duplicates else warn)(f"duplicate name+set pairs: {len(duplicates)} {duplicates[:3]}")
    count = db.execute("SELECT COUNT(*) FROM sealed_products WHERE us_exclusive=1 "
                       "AND cardmarket_id IS NOT NULL").fetchone()[0]
    (ok if count == 0 else fail)(f"us-exclusive products with a CM match (shouldn't happen): {count}")

    print("== cardmarket ids ==")
    heavy_reuse = db.execute("SELECT cardmarket_id, COUNT(*) c FROM sealed_products "
                             "WHERE cardmarket_id IS NOT NULL GROUP BY 1 HAVING c>2").fetchall()
    (ok if not heavy_reuse else warn)(f"CM ids shared by more than 2 products: {heavy_reuse}")

    print("== prices ==")
    count = db.execute("SELECT COUNT(*) FROM sealed_latest_prices WHERE "
                       "COALESCE(tcgplayer_market,1)<=0 OR COALESCE(tcgplayer_low,1)<=0 OR "
                       "COALESCE(cardmarket_trend,1)<=0 OR COALESCE(cardmarket_low,1)<=0").fetchone()[0]
    (ok if count == 0 else fail)(f"zero or negative prices: {count}")
    count = db.execute("SELECT COUNT(*) FROM sealed_latest_prices WHERE "
                       "tcgplayer_market IS NULL AND cardmarket_trend IS NULL").fetchone()[0]
    (ok if count == 0 else fail)(f"price rows with no price at all: {count}")
    stale = db.execute("SELECT COUNT(*) FROM sealed_latest_prices WHERE "
                       "price_date < date('now', '-3 days')").fetchone()[0]
    (ok if stale == 0 else warn)(f"price rows older than 3 days: {stale}")
    top_usd = db.execute("SELECT p.name, l.tcgplayer_market FROM sealed_latest_prices l "
                         "JOIN sealed_products p USING(product_id) "
                         "ORDER BY l.tcgplayer_market DESC LIMIT 5").fetchall()
    print("        most expensive $:", [(name[:45], price) for name, price in top_usd])
    top_eur = db.execute("SELECT p.name, l.cardmarket_trend FROM sealed_latest_prices l "
                         "JOIN sealed_products p USING(product_id) "
                         "ORDER BY l.cardmarket_trend DESC LIMIT 5").fetchall()
    print("        most expensive €:", [(name[:45], price) for name, price in top_eur])

    print(f"== price divergence (ratio above {args.ratio_warn} = match probably wrong) ==")
    both_priced = db.execute(
        "SELECT p.product_id, p.name, l.tcgplayer_market, l.cardmarket_trend "
        "FROM sealed_latest_prices l JOIN sealed_products p USING(product_id) "
        "WHERE l.tcgplayer_market IS NOT NULL AND l.cardmarket_trend IS NOT NULL").fetchall()
    suspicious = []
    for pid, name, usd, eur in both_priced:
        if min(usd, eur) < 5:  # ratios between tiny prices mean nothing
            continue
        ratio = max(usd / eur, eur / usd)
        if ratio > args.ratio_warn:
            suspicious.append((round(ratio, 1), pid, name, usd, eur))
    suspicious.sort(reverse=True)
    (ok if not suspicious else warn)(f"products with a suspicious $/€ ratio: {len(suspicious)}")
    for ratio, pid, name, usd, eur in suspicious[:20]:
        print(f"        x{ratio:5}  ${usd:9.2f} / €{eur:9.2f}  {name}  ({pid})")

    print("== coverage floors (recent sets, the products people actually look up) ==")
    for product_type, floor in (("booster_box", 1.0), ("etb", 0.9)):
        total, matched = db.execute(
            "SELECT COUNT(*), SUM(CASE WHEN l.tcgplayer_market IS NOT NULL "
            "AND l.cardmarket_trend IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM sealed_products p JOIN sealed_sets s ON s.group_id=p.group_id "
            "LEFT JOIN sealed_latest_prices l USING(product_id) "
            "WHERE p.product_type=? AND s.published_on>='2020-01-01' AND p.us_exclusive=0 "
            "AND p.is_presale=0", (product_type,)).fetchone()
        coverage = (matched or 0) / max(1, total)
        (ok if coverage >= floor else fail)(
            f"modern {product_type} priced on both sides: {matched}/{total} "
            f"({coverage:.0%}, floor {floor:.0%})")

    print(f"\n{'=' * 50}\nRESULT: {len(failures)} FAIL, {len(warnings)} warnings")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
