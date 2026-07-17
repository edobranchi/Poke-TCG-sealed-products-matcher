"""Matcher tests using REAL product names captured live on 2026-07-16
(TCGplayer group 23651 'SV08: Surging Sparks' via tcgcsv, Cardmarket
products_nonsingles_6.json). These names are the spec: they encode the actual
naming-style divergences between the two marketplaces.
"""

import unittest

from match_cardmarket import match_group, tokenize, set_name_tokens

GROUP = "SV08: Surging Sparks"

TP = [
    {"productId": 565606, "name": "Surging Sparks Booster Box", "product_type": "booster_box"},
    {"productId": 565630, "name": "Surging Sparks Elite Trainer Box", "product_type": "etb"},
    {"productId": 565640, "name": "Surging Sparks Booster Bundle (LGS)", "product_type": "booster_bundle"},
    {"productId": 565610, "name": "Surging Sparks Booster Pack", "product_type": "booster_pack"},
    {"productId": 565611, "name": "Surging Sparks Sleeved Booster Pack", "product_type": "booster_pack"},
    {"productId": 565650, "name": "Surging Sparks 3 Pack Blisters [Zapdos]", "product_type": "blister"},
    {"productId": 565651, "name": "Surging Sparks Single Pack Blister [Wooper]", "product_type": "blister"},
    {"productId": 565652, "name": "Surging Sparks Premium Checklane Blister [Alakazam]", "product_type": "blister"},
    {"productId": 565660, "name": "Surging Sparks Booster Box Case", "product_type": "booster_box"},
]

CM = [
    {"idProduct": 901, "name": "Surging Sparks Booster Box", "categoryName": "Pokémon Display", "idExpansion": 5773},
    {"idProduct": 902, "name": "Surging Sparks Elite Trainer Box", "categoryName": "Pokémon Elite Trainer Boxes", "idExpansion": 5773},
    {"idProduct": 903, "name": "Surging Sparks Booster Bundle", "categoryName": "Pokémon Display", "idExpansion": 5773},
    {"idProduct": 904, "name": "Surging Sparks Booster", "categoryName": "Pokémon Booster", "idExpansion": 5773},
    {"idProduct": 905, "name": "Surging Sparks Sleeved Booster", "categoryName": "Pokémon Booster", "idExpansion": 5773},
    {"idProduct": 906, "name": "Surging Sparks: Zapdos 3-Pack Blister", "categoryName": "Pokémon Blisters", "idExpansion": 5773},
    {"idProduct": 907, "name": "Surging Sparks: Wooper 1-Pack Blister", "categoryName": "Pokémon Blisters", "idExpansion": 5773},
    {"idProduct": 908, "name": "Surging Sparks: Alakazam Premium Checklane Blister", "categoryName": "Pokémon Blisters", "idExpansion": 5773},
    {"idProduct": 909, "name": "Surging Sparks 6 Booster Box Case", "categoryName": "Pokémon Display", "idExpansion": 5773},
    {"idProduct": 910, "name": "Surging Sparks Booster Bundle Pokémon Center Version", "categoryName": "Pokémon Display", "idExpansion": 5773},
    # decoy from another set — must never match
    {"idProduct": 999, "name": "Stellar Crown Booster Box", "categoryName": "Pokémon Display", "idExpansion": 5651},
]


class NormalizerTest(unittest.TestCase):
    def test_set_tokens_strips_sv_prefix(self):
        self.assertEqual(set_name_tokens(GROUP), tokenize("Surging Sparks"))

    def test_plurals_and_qualifiers(self):
        # "3 Pack Blisters [Zapdos]" and ": Zapdos 3-Pack Blister" normalize identically
        self.assertEqual(tokenize("Surging Sparks 3 Pack Blisters [Zapdos]"),
                         tokenize("Surging Sparks: Zapdos 3-Pack Blister"))

    def test_single_pack_equals_1_pack(self):
        self.assertEqual(tokenize("Single Pack Blister [Wooper]"),
                         tokenize("Wooper 1-Pack Blister"))

    def test_lgs_qualifier_dropped(self):
        self.assertEqual(tokenize("Booster Bundle (LGS)"), tokenize("Booster Bundle"))

    def test_sleeved_booster_pack_variants(self):
        self.assertEqual(tokenize("Sleeved Booster Pack"), tokenize("Sleeved Booster"))

    def test_curly_apostrophe_equals_ascii_apostrophe(self):
        # CM writes U+2019 ("Champion’s"), TP writes ASCII ("Champion's") —
        # the full-run 2026-07-16 bug: ascii-fold-before-punct deleted ’
        self.assertEqual(tokenize("Champion’s Path Elite Trainer Box"),
                         tokenize("Champion's Path Elite Trainer Box"))

    def test_and_vs_ampersand(self):
        self.assertEqual(tokenize("Black and White Booster Box"),
                         tokenize("Black & White Booster Box"))

    def test_era_dash_prefix_stripped(self):
        self.assertEqual(set_name_tokens("SM - Burning Shadows"), tokenize("Burning Shadows"))

    def test_set_alias_xy_base(self):
        self.assertEqual(set_name_tokens("XY Base Set"), tokenize("XY"))

    def test_set_of_n_bundle_never_matches_single(self):
        # TP bundle SKUs ("Set of 2") vs CM singles: different quantity, block
        tp = [{"productId": 1, "name": "Evolving Skies Elite Trainer Box [Set of 2]",
               "product_type": "etb"}]
        cm = [{"idProduct": 2, "name": "Evolving Skies Elite Trainer Box",
               "categoryName": "Pokémon Elite Trainer Boxes", "idExpansion": 4444}]
        rep = match_group("SWSH07: Evolving Skies", tp, cm)
        self.assertNotIn(1, rep.matched)

    def test_case_count_on_cm_side_still_matches(self):
        # narrow guard: CM spelling out the standard case size must NOT block
        tp = [{"productId": 1, "name": "Surging Sparks Booster Box Case",
               "product_type": "booster_box"}]
        cm = [{"idProduct": 2, "name": "Surging Sparks 6 Booster Box Case",
               "categoryName": "Pokémon Display", "idExpansion": 5773}]
        rep = match_group("SV08: Surging Sparks", tp, cm)
        self.assertEqual(rep.matched.get(1, (None,))[0], 2)

    def test_pc_edition_never_matches_regular_product(self):
        # Pokemon Center editions are premium-priced distinct products
        tp = [{"productId": 1, "name": "Pitch Black Pokemon Center Elite Trainer Box (Exclusive)",
               "product_type": "etb"}]
        cm = [{"idProduct": 2, "name": "Pitch Black Elite Trainer Box",
               "categoryName": "Pokémon Elite Trainer Boxes", "idExpansion": 88}]
        rep = match_group("Pitch Black", tp, cm)
        self.assertNotIn(1, rep.matched)

    def test_pc_edition_matches_pc_edition(self):
        tp = [{"productId": 1, "name": "Pitch Black Pokemon Center Elite Trainer Box (Exclusive)",
               "product_type": "etb"}]
        cm = [{"idProduct": 2, "name": "Pitch Black Pokémon Center Elite Trainer Box",
               "categoryName": "Pokémon Elite Trainer Boxes", "idExpansion": 88}]
        rep = match_group("Pitch Black", tp, cm)
        self.assertEqual(rep.matched.get(1, (None,))[0], 2)

    def test_vintage_unlimited_matches_cm_plain_listing(self):
        # policy: CM's plain vintage product IS the unlimited print
        tp = [{"productId": 1, "name": "Gym Heroes Booster Box [Unlimited Edition]",
               "product_type": "booster_box"}]
        cm = [{"idProduct": 2, "name": "Gym Heroes Booster Box",
               "categoryName": "Pokémon Display", "idExpansion": 77}]
        rep = match_group("Gym Heroes", tp, cm)
        self.assertEqual(rep.matched.get(1, (None,))[0], 2)

    def test_vintage_first_edition_never_matches_plain(self):
        # a 1st-ed box priced as the plain listing would be wrong by 5-20x
        tp = [{"productId": 1, "name": "Gym Heroes Booster Box [1st Edition]",
               "product_type": "booster_box"}]
        cm = [{"idProduct": 2, "name": "Gym Heroes Booster Box",
               "categoryName": "Pokémon Display", "idExpansion": 77}]
        rep = match_group("Gym Heroes", tp, cm)
        self.assertNotIn(1, rep.matched)

    def test_set_alias_sv_151(self):
        tp = [{"productId": 1, "name": "151 Elite Trainer Box", "product_type": "etb"}]
        cm = [{"idProduct": 2, "name": "151 Elite Trainer Box",
               "categoryName": "Pokémon Elite Trainer Boxes", "idExpansion": 5062}]
        rep = match_group("SV: Scarlet & Violet 151", tp, cm)
        self.assertEqual(rep.matched.get(1, (None,))[0], 2)

    def test_unscoped_misc_group_matches_whole_catalog(self):
        # Miscellaneous group: set-scoping bypassed, full-name similarity only
        tp = [{"productId": 1, "name": "Kanto Power Mini Tin [Charizard]", "product_type": "tin"}]
        cm = [{"idProduct": 2, "name": "Kanto Power: Charizard Mini Tin",
               "categoryName": "Pokémon Tins", "idExpansion": 4141}]
        rep = match_group("Miscellaneous Cards & Products", tp, cm)
        self.assertEqual(rep.matched.get(1, (None,))[0], 2)


class MatchGroupTest(unittest.TestCase):
    def setUp(self):
        self.rep = match_group(GROUP, TP, CM)

    def matched_cm(self, tp_id):
        self.assertIn(tp_id, self.rep.matched, f"{tp_id} unmatched: {self.rep.unmatched}")
        return self.rep.matched[tp_id][0]

    def test_flagships(self):
        self.assertEqual(self.matched_cm(565606), 901)  # booster box
        self.assertEqual(self.matched_cm(565630), 902)  # ETB

    def test_bundle_ignores_lgs_and_prefers_plain_over_pokemon_center(self):
        self.assertEqual(self.matched_cm(565640), 903)

    def test_packs(self):
        self.assertEqual(self.matched_cm(565610), 904)
        self.assertEqual(self.matched_cm(565611), 905)

    def test_blisters_with_divergent_qualifier_style(self):
        self.assertEqual(self.matched_cm(565650), 906)
        self.assertEqual(self.matched_cm(565651), 907)
        self.assertEqual(self.matched_cm(565652), 908)

    def test_case_matches_case_not_plain_box(self):
        # greedy 1:1: plain box takes 901, so the case product must NOT steal it
        self.assertNotEqual(self.rep.matched.get(565660, (None,))[0], 901)

    def test_case_never_takes_plain_even_when_plain_product_absent(self):
        # asymmetry guard: without the plain TP booster box competing, the TP
        # case product still must not match CM's plain booster box
        tp_case_only = [p for p in TP if p["productId"] == 565660]
        cm_no_case = [c for c in CM if c["idProduct"] != 909]
        rep = match_group(GROUP, tp_case_only, cm_no_case)
        self.assertNotIn(565660, rep.matched)

    def test_decoy_other_set_never_used(self):
        used = {cm for cm, _ in self.rep.matched.values()}
        self.assertNotIn(999, used)

    def test_one_to_one(self):
        used = [cm for cm, _ in self.rep.matched.values()]
        self.assertEqual(len(used), len(set(used)))


class OverridesTest(unittest.TestCase):
    def test_forced_match_wins(self):
        rep = match_group(GROUP, TP, CM, {"matches": {565606: 909}})
        self.assertEqual(rep.matched[565606], (909, 1.0))
        self.assertIn(565606, rep.overridden)
        # 909 is consumed; nothing else may take it
        used = [cm for cm, _ in rep.matched.values()]
        self.assertEqual(used.count(909), 1)

    def test_never_blocks(self):
        rep = match_group(GROUP, TP, CM, {"never": [565606]})
        self.assertNotIn(565606, rep.matched)
        self.assertNotIn(565606, [u[0] for u in rep.unmatched])


if __name__ == "__main__":
    unittest.main()
