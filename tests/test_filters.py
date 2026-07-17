"""Pipeline product-filter tests: the multi-unit SKU cut (single retail units only)."""

import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_sealed_db import is_multi_unit, is_us_exclusive, is_sealed, classify


class MultiUnitCutTest(unittest.TestCase):
    def test_cases_and_bundles_excluded(self):
        for name in (
            "Surging Sparks Booster Box Case",
            "Pitch Black Pokemon Center Elite Trainer Box Case (Exclusive)",
            "Evolving Skies Elite Trainer Box [Set of 2]",
            "Chaos Rising Booster Pack Art Bundle [Set of 4]",
            "Pitch Black Half Booster Boxes",
            "Forbidden Light Theme Deck Display",
            "Mysterious Powers Tin [Set of 3]",
            "Stellar Crown Sleeved Booster Master Carton",  # 144-pack distributor carton
        ):
            self.assertTrue(is_multi_unit(name), name)

    def test_single_retail_units_kept(self):
        for name in (
            "Surging Sparks Booster Box",
            "Surging Sparks Elite Trainer Box",
            "Surging Sparks Booster Bundle (LGS)",
            "Surging Sparks Booster Pack",
            "Kanto Power Mini Tin [Charizard]",
            # "Case File" is a single Detective Pikachu product, not a case
            "Detective Pikachu: Charizard GX Case File",
            "Detective Pikachu On the Case Figure Collection",
        ):
            self.assertFalse(is_multi_unit(name), name)

    def test_us_retailer_exclusives_flagged(self):
        for name in (
            "Costco Pokemon Collector 3-Pack: Eevee Treasure Chest + 2 Poke Ball Tins",
            "Evolving Powers Premium Collection (Target Exclusive)",
            "Prismatic Evolutions Elite Trainer Box and Pokeball (Sam's Club)",
            "General Mills Promo Booster Pack [Kanto]",
            "Dragonite Dragons Tin (Walgreens Exclusive)",
        ):
            self.assertTrue(is_us_exclusive(name), name)

    def test_pokemon_center_exclusive_NOT_flagged(self):
        # "(Exclusive)" alone = Pokemon Center on TCGplayer; PC sells on CM
        for name in (
            "Pitch Black Pokemon Center Elite Trainer Box (Exclusive)",
            "Surging Sparks Booster Bundle (LGS)",
            "Surging Sparks Booster Box",
        ):
            self.assertFalse(is_us_exclusive(name), name)

    def test_world_championship_decks_excluded(self):
        self.assertFalse(is_sealed(
            {"name": "2016 World Championship Deck: Shintaro Ito (Magical Symphony)",
             "extendedData": []}))

    def test_theme_decks_kept(self):
        # user decision: ALL theme decks stay (vintage ones are $500-$2050 grails)
        self.assertTrue(is_sealed(
            {"name": 'Legendary Collection Theme Deck - "Lava"', "extendedData": []}))
        self.assertEqual(classify('Legendary Collection Theme Deck - "Lava"'), "deck")

    def test_kept_products_still_classify(self):
        self.assertEqual(classify("Surging Sparks Booster Box"), "booster_box")
        self.assertEqual(classify("Detective Pikachu: Charizard GX Case File"), "collection")


if __name__ == "__main__":
    unittest.main()
