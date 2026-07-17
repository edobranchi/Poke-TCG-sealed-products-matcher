"""Match Cardmarket products to TCGplayer products by name.

The two marketplaces use completely unrelated product ids, so the only way to
link them is comparing names. Names are messy though: "Surging Sparks 3 Pack
Blisters [Zapdos]" on one side is "Surging Sparks: Zapdos 3-Pack Blister" on
the other. The approach here:

  1. normalize both names into token bags (fix plurals, unify phrasings)
  2. for each TCGplayer set, collect Cardmarket candidates that mention the set
  3. score pairs with Jaccard similarity on the leftover tokens
  4. greedily assign best pairs 1:1, category must be compatible
  5. manual overrides (cm_overrides.yaml) always win

Anything ambiguous stays unmatched - a missing price is better than a wrong one.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

MATCH_THRESHOLD = 0.60
REVIEW_THRESHOLD = 0.85  # accepted, but worth a second look by a human

# Which Cardmarket categories can pair with which of our product types.
CATEGORY_COMPAT = {
    "booster_box": {"Pokémon Display"},
    "booster_bundle": {"Pokémon Display", "Pokémon Box Set"},
    "booster_pack": {"Pokémon Booster"},
    "etb": {"Pokémon Elite Trainer Boxes", "Pokémon Box Set"},
    "tin": {"Pokémon Tins"},
    "deck": {"Pokémon Theme Deck", "Pokémon Trainer Kits", "Pokémon Box Set"},
    "blister": {"Pokémon Blisters"},
    "collection": {"Pokémon Box Set", "Pokémon Tins", "Pokémon Display"},
}

# Tokens that mean "this is physically a different product". If one side has
# the token and the other doesn't, never match the pair - a "Booster Bundle
# Case" priced as a single bundle would be wrong by 6x, and a 1st edition
# vintage box priced as unlimited would be wrong by 20x.
CONFIG_TOKENS = {"case", "half", "1stedition", "shadowless", "pokemoncenter"}

# Phrasing differences between the two sites, unified before tokenizing.
PHRASE_FIXES = [
    (re.compile(r"elite trainer box(es)?|\betb\b"), " etb "),
    (re.compile(r"booster display( box)?"), " booster box "),
    (re.compile(r"sleeved booster( pack)?s?"), " sleevedbooster "),
    (re.compile(r"booster packs?"), " booster "),
    (re.compile(r"single pack blister|1[\s-]*pack blister"), " 1pack blister "),
    (re.compile(r"(\d+)[\s-]*pack blisters?"), r" \g<1>pack blister "),
    (re.compile(r"premium checklane blister"), " premium checklane blister "),
    (re.compile(r"pok[eé]mon center"), " pokemoncenter "),
    (re.compile(r"\(exclusive\)|exclusive"), " pokemoncenter "),
    (re.compile(r"\blgs\b"), " "),
    # Cardmarket's plain listing for vintage IS the unlimited print
    (re.compile(r"unlimited edition|\bunlimited\b"), " "),
    (re.compile(r"(1st|first) edition"), " 1stedition "),
    # TCGplayer bundle listings like "[Set of 2]" that Cardmarket doesn't sell
    (re.compile(r"set of (\d+)"), r" setof\1 "),
]

STOPWORDS = {"pokemon", "tcg", "the", "version", "of", "set", "and", "vs"}

SINGULARS = {
    "boosters": "booster", "blisters": "blister", "tins": "tin",
    "decks": "deck", "boxes": "box", "bundles": "bundle", "cases": "case",
    "packs": "pack", "championships": "championship",
}

# Set names TCGplayer and Cardmarket phrase differently.
SET_ALIASES = {
    "xy base": "xy",
    "sword shield base": "sword shield",
    "scarlet violet base": "scarlet violet",
    "sm base": "sun moon",
    "scarlet violet 151": "151",  # Cardmarket just calls it "151"
}

# TCGplayer groups that aren't real sets - match against the whole catalog.
UNSCOPED_GROUPS = {"miscellaneous cards products"}


def strip_accents(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def tokenize_list(name):
    # Replace punctuation BEFORE stripping accents: the curly apostrophe in
    # Cardmarket's "Champion's Path" isn't ascii-foldable and would just get
    # deleted, gluing "Champion's" into "champions".
    cleaned = re.sub(r"[\[\]():,&/+.'’!´`–—-]", " ", name)
    cleaned = strip_accents(cleaned).lower()
    for pattern, replacement in PHRASE_FIXES:
        cleaned = pattern.sub(replacement, cleaned)
    tokens = []
    for word in cleaned.split():
        word = SINGULARS.get(word, word)
        if word not in STOPWORDS:
            tokens.append(word)
    return tokens


def tokenize(name):
    """Token bag (multiset) for a product name."""
    return Counter(tokenize_list(name))


ERA_PREFIX = re.compile(r"^(sm|swsh|sv|xy|bw|dp|hs|hgss|ex|me|col|pop)\s*[-–—:]\s*", re.I)


def set_name_tokens(group_name):
    """Tokens identifying a set, from group names like 'SV08: Surging Sparks'
    or 'SM - Burning Shadows'."""
    if " ".join(tokenize_list(group_name)) in UNSCOPED_GROUPS:
        return Counter()  # empty = no set scoping, search everything
    name = strip_accents(group_name).lower().strip()
    name = re.sub(r"^[a-z0-9.&\s]{1,10}:", "", name).strip()
    name = ERA_PREFIX.sub("", name).strip()
    normalized = " ".join(tokenize_list(name))
    normalized = SET_ALIASES.get(normalized, normalized)
    return Counter(normalized.split())


def counter_minus(a, b):
    result = a.copy()
    result.subtract(b)
    return Counter({k: v for k, v in result.items() if v > 0})


def contains_all(haystack, needle):
    return all(haystack.get(k, 0) >= v for k, v in needle.items())


def jaccard(a, b):
    if not a and not b:
        return 1.0
    union = sum((a | b).values())
    return sum((a & b).values()) / union if union else 0.0


def different_config(a, b):
    """True when the two token bags disagree on a product-config token."""
    for token in CONFIG_TOKENS:
        if (token in a) != (token in b):
            return True
    if {t for t in a if t.startswith("setof")} != {t for t in b if t.startswith("setof")}:
        return True
    # both sides state counts and they differ (a 4-box case vs a 6-box case);
    # a count on one side only is fine, that's just Cardmarket spelling out
    # the standard size
    a_numbers = {t for t in a if t.isdigit()}
    b_numbers = {t for t in b if t.isdigit()}
    return bool(a_numbers) and bool(b_numbers) and a_numbers != b_numbers


@dataclass
class MatchReport:
    matched: dict = field(default_factory=dict)     # tp_id -> (cm_id, score)
    review: list = field(default_factory=list)      # low-score matches to eyeball
    unmatched: list = field(default_factory=list)   # (tp_id, name, top candidates)
    overridden: list = field(default_factory=list)  # matched via overrides
    never: list = field(default_factory=list)       # blocked via overrides
    candidates: dict = field(default_factory=dict)  # tp_id -> top 5 scored candidates


def match_group(group_name, tp_products, cm_products, overrides=None):
    """Match one TCGplayer group's products against the Cardmarket catalog.

    tp_products: [{productId, name, product_type}]
    cm_products: [{idProduct, name, categoryName, idExpansion}]
    overrides:   {"matches": {tp_id: cm_id}, "never": [tp_id, ...]}
    """
    overrides = overrides or {}
    forced = {int(k): int(v) for k, v in (overrides.get("matches") or {}).items()}
    never = {int(x) for x in (overrides.get("never") or [])}
    report = MatchReport()

    set_toks = set_name_tokens(group_name)
    cm_tokens = {p["idProduct"]: tokenize(p["name"]) for p in cm_products}

    # candidates = CM products whose name contains the set name
    pool = [p for p in cm_products if contains_all(cm_tokens[p["idProduct"]], set_toks)]

    # If we can pin down the set's booster box or ETB exactly, pull in every
    # CM product from that same expansion too. Covers CM names that phrase
    # the set differently.
    anchor_expansions = set()
    for candidate in pool:
        leftover = counter_minus(cm_tokens[candidate["idProduct"]], set_toks)
        if list(leftover.elements()) in (["booster", "box"], ["etb"]) and candidate["idExpansion"]:
            anchor_expansions.add(candidate["idExpansion"])
    if anchor_expansions:
        pool_ids = {p["idProduct"] for p in pool}
        for p in cm_products:
            if p["idExpansion"] in anchor_expansions and p["idProduct"] not in pool_ids:
                pool.append(p)

    # score every compatible pair
    scored_pairs = []
    all_candidates = defaultdict(list)
    for tp in tp_products:
        if tp["productId"] in forced or tp["productId"] in never:
            continue
        tp_leftover = counter_minus(tokenize(tp["name"]), set_toks)
        compatible = CATEGORY_COMPAT.get(tp["product_type"], set())
        for cm in pool:
            if cm["categoryName"] not in compatible:
                continue
            cm_leftover = counter_minus(cm_tokens[cm["idProduct"]], set_toks)
            if different_config(tp_leftover, cm_leftover):
                continue
            score = jaccard(tp_leftover, cm_leftover)
            all_candidates[tp["productId"]].append((score, cm["idProduct"], cm["name"]))
            if score >= MATCH_THRESHOLD:
                scored_pairs.append((score, tp["productId"], cm["idProduct"],
                                     tp["name"], cm["name"]))

    # overrides first (only for products actually in this group - stale
    # entries for since-removed products must stay inert)
    group_ids = {tp["productId"] for tp in tp_products}
    used_cm_ids = set()
    for tp_id, cm_id in forced.items():
        if tp_id not in group_ids:
            continue
        report.matched[tp_id] = (cm_id, 1.0)
        report.overridden.append(tp_id)
        used_cm_ids.add(cm_id)
    report.never.extend(sorted(never & group_ids))

    # then greedy 1:1 by best score
    for score, tp_id, cm_id, tp_name, cm_name in sorted(scored_pairs, reverse=True):
        if tp_id in report.matched or cm_id in used_cm_ids:
            continue
        report.matched[tp_id] = (cm_id, score)
        used_cm_ids.add(cm_id)
        if score < REVIEW_THRESHOLD:
            report.review.append((tp_id, tp_name, cm_id, cm_name, round(score, 3)))

    for tp in tp_products:
        tp_id = tp["productId"]
        top5 = [(round(s, 3), cid, n)
                for s, cid, n in sorted(all_candidates.get(tp_id, []), reverse=True)[:5]]
        report.candidates[tp_id] = top5
        if tp_id not in report.matched and tp_id not in never:
            report.unmatched.append((tp_id, tp["name"], top5))

    return report
