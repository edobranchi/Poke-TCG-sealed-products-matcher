"""Curation-gate tests: decision bootstrap + pending store."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_sealed_db import (STATE_SCHEMA, OUTPUT_SCHEMA, load_decisions,
                             save_pending, save_cm_catalog)


def make_state():
    con = sqlite3.connect(":memory:")
    con.executescript(STATE_SCHEMA)
    return con


class BootstrapTest(unittest.TestCase):
    def test_bootstrap_from_published_db_grandfathers_catalog(self):
        with tempfile.TemporaryDirectory() as out:
            pub = sqlite3.connect(os.path.join(out, "sealed_prices.db"))
            pub.executescript(OUTPUT_SCHEMA)
            pub.execute("INSERT INTO sealed_sets VALUES (1,'Set','S',NULL)")
            pub.execute("INSERT INTO sealed_products VALUES "
                        "(100,1,'A','a',NULL,NULL,'etb',0,NULL,0,NULL)")
            pub.commit(); pub.close()
            state = make_state()
            current = [{"productId": 100}, {"productId": 200}]  # 200 = new product
            decisions, boot = load_decisions(state, out, current)
            self.assertTrue(boot)
            self.assertEqual(decisions, {100: ("keep", None)})  # 200 NOT grandfathered
            rows = state.execute("SELECT product_id, decision FROM product_decisions").fetchall()
            self.assertEqual(rows, [(100, "keep")])

    def test_bootstrap_fresh_setup_keeps_everything(self):
        with tempfile.TemporaryDirectory() as out:  # no published db
            state = make_state()
            current = [{"productId": 1}, {"productId": 2}]
            decisions, boot = load_decisions(state, out, current)
            self.assertTrue(boot)
            self.assertEqual(set(decisions), {1, 2})

    def test_existing_decisions_returned_untouched(self):
        state = make_state()
        state.execute("INSERT INTO product_decisions "
                      "(product_id, decision, cm_id, decided_at) VALUES (5,'drop',NULL,'t')")
        state.execute("INSERT INTO product_decisions "
                      "(product_id, decision, cm_id, decided_at) VALUES (6,'keep',77,'t')")
        decisions, boot = load_decisions(state, "/nonexistent", [{"productId": 9}])
        self.assertFalse(boot)
        self.assertEqual(decisions, {5: ("drop", None), 6: ("keep", 77)})

    def test_bootstrap_captures_names(self):
        with tempfile.TemporaryDirectory() as out:
            pub = sqlite3.connect(os.path.join(out, "sealed_prices.db"))
            pub.executescript(OUTPUT_SCHEMA)
            pub.execute("INSERT INTO sealed_sets VALUES (1,'Surging Sparks','SSP',NULL)")
            pub.execute("INSERT INTO sealed_products VALUES "
                        "(100,1,'Booster Box','booster box',NULL,NULL,'booster_box',0,NULL,0,NULL)")
            pub.commit(); pub.close()
            state = make_state()
            load_decisions(state, out, [])
            row = state.execute("SELECT name, group_name FROM product_decisions "
                                "WHERE product_id=100").fetchone()
            self.assertEqual(row, ("Booster Box", "Surging Sparks"))


class PendingStoreTest(unittest.TestCase):
    def _pend(self, pid, name="New Thing"):
        return {"productId": pid, "groupId": 1, "name": name, "imageUrl": "i",
                "url": "u", "product_type": "etb", "is_presale": 0,
                "released_on": None, "us_exclusive": 0}

    def test_store_preserves_first_seen_and_prunes_decided(self):
        state = make_state()
        reports = {"matched": {10: (500, 0.9)}, "candidates": {10: [[0.9, 500, "CM Thing"]]}}
        save_pending(state, [self._pend(10)], {1: "SetName"}, reports)
        first = state.execute("SELECT first_seen FROM pending_products WHERE product_id=10").fetchone()[0]
        # second run: still pending -> first_seen unchanged, fields refreshed
        save_pending(state, [self._pend(10, "New Thing v2")], {1: "SetName"}, reports)
        row = state.execute("SELECT first_seen, name, heuristic_cm, candidates "
                            "FROM pending_products WHERE product_id=10").fetchone()
        self.assertEqual(row[0], first)
        self.assertEqual(row[1], "New Thing v2")
        self.assertEqual(row[2], 500)
        self.assertEqual(json.loads(row[3])[0][1], 500)
        # third run: product decided elsewhere -> disappears from pending
        save_pending(state, [], {}, {"matched": {}, "candidates": {}})
        self.assertEqual(
            state.execute("SELECT COUNT(*) FROM pending_products").fetchone()[0], 0)


class CmCatalogTest(unittest.TestCase):
    def test_persist_replaces_snapshot(self):
        state = make_state()
        save_cm_catalog(state, [{"idProduct": 1, "name": "A", "categoryName": "Pokémon Display",
                                    "idExpansion": 9}], "2026-07-17")
        save_cm_catalog(state, [{"idProduct": 2, "name": "B", "categoryName": "Pokémon Booster",
                                    "idExpansion": 9}], "2026-07-18")
        rows = state.execute("SELECT id_product, name FROM cm_catalog").fetchall()
        self.assertEqual(rows, [(2, "B")])  # full replace, not append


if __name__ == "__main__":
    unittest.main()
