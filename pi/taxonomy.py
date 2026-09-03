"""Who made it, who it is for, and what kind of thing it is.

Four questions the shops do not answer in any usable form, and all four are
needed before anything can be filtered.

**Brand.** Shopify's `vendor` is not a brand field, it is a free-text box. It
holds real brands most of the time, the shop's own name some of the time
(`CommonGround12`), and five spellings of the same brand always (`Nike`, `NIKE`,
`nike`, `NIke`, `NIKE_`). Two mechanisms sort it out. Case and punctuation folding
does most of the work by itself: it merges those five into one. What remains is
told apart by corroboration — a vendor carried by several independent shops is a
brand, a vendor carried by exactly one is that shop naming itself. The same
principle the reference price already uses for market prices and RRPs, and it
needs no external list to maintain: 4,485 of the 6,068 distinct vendors appear in
a single shop and cover only 77,106 products, while 314 appear in six or more and
cover 281,726.

**Gender.** Not in the data at all. An explicit word appears on 56,713 of 449,110
products, so 87% of the catalogue says nothing. Guessing the rest was rejected on
purpose: a guessed gender is indistinguishable from a known one at the point of
use, and the whole reason to have the field is buying for somebody else. So the
answer is allowed to be "unknown", and a filter for men shows men and unknown.

**Kind.** `category` is free text: 2,583 distinct values, 57,707 products with
none. `size_norm` is the better classifier because it is already normalised and
present. `US10.5` is a shoe, `XL` is a garment, `OS` is an accessory. The one
place this misfires is Italian clothing sizing, where a jacket is a 44 the same
way a shoe is — so words win over sizes when the words are unambiguous.

**Audience.** Whether it is for a child, which is not a third gender but a
different question — a boys' shoe and a girls' shoe are both a child's. Read
strictly from the title and loosely from the category, because the two fields
lie in different ways; the long note above `_KIDS_TITLE` says which words did
not survive measurement and why. 25,122 products of 660,470 are read as a
child's, and the reading is stored rather than acted on at collection, so
improving it costs a `pi reclassify` instead of a fresh crawl.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Collection, Iterable
from itertools import pairwise

# A vendor string needs this many distinct shops behind it before it is read as
# a brand rather than as a shop's own name. Three, matching msrp_min_shops: two
# shops can both be a chain, and the whole point is independent witnesses.
MIN_BRAND_SHOPS = 3


def fold(value: str | None) -> str:
    """Squash a brand string to its comparable core: 'C.P. Company' -> 'cpcompany'."""
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


# The hand-written layer, and the only part of brand handling that is not derived
# from the data. Two jobs. Aliases merge spellings that folding cannot: an
# ampersand written as "and", a name abbreviated in one shop and spelled out in
# another. Families answer "I want Nike" with Jordan and Nike SB as well, without
# claiming a Jordan is branded Nike — the canonical name stays Jordan, the family
# is what filters match.
_ALIAS: dict[str, str] = {
    "cpcompany": "cpcompany",
    "ccompany": "cpcompany",       # shops that lose the periods and the P
    "thenorthface": "thenorthface",
    "tnf": "thenorthface",
    "arcteryx": "arcteryx",
    "arcteryxveilance": "arcteryx",
    "stoneislandshadowproject": "stoneisland",
    "yeezy": "yeezy",
}

_FAMILY: dict[str, str] = {
    "nike": "Nike",
    "nikesb": "Nike",
    "nikeacg": "Nike",
    "nikelab": "Nike",
    "jordan": "Nike",
    "airjordan": "Nike",
    "jordanbrand": "Nike",
    "adidas": "adidas",
    "adidasoriginals": "adidas",
    "adidasperformance": "adidas",
    "adidasconsortium": "adidas",
    "adidasterrex": "adidas",
    "yeezy": "adidas",
    "newbalance": "New Balance",
    "newbalancenumeric": "New Balance",
    "stoneisland": "Stone Island",
    "stoneislandshadowproject": "Stone Island",
    "cpcompany": "C.P. Company",
    "carharttwip": "Carhartt WIP",
    "carhartt": "Carhartt WIP",
    "thenorthface": "The North Face",
    "asics": "Asics",
    "asicssportstyle": "Asics",
    "vans": "Vans",
    "vansvault": "Vans",
    "converse": "Converse",
    "puma": "Puma",
    "arcteryx": "Arc'teryx",
    "salomon": "Salomon",
}


def load_brand_index(conn: sqlite3.Connection) -> dict[str, str]:
    """Build folded-vendor -> canonical-name from the catalogue itself.

    The canonical name is the spelling the most shops use, not the one the most
    products use: one large shop shouting NIKE at 11,152 products should not
    outvote eighty-seven shops writing Nike.
    """
    rows = conn.execute(
        """
        SELECT brand, store_id, count(*) AS n
          FROM products
         WHERE brand IS NOT NULL AND trim(brand) <> ''
         GROUP BY brand, store_id
        """
    ).fetchall()

    shops: dict[str, set[int]] = {}
    spellings: dict[str, dict[str, int]] = {}
    for row in rows:
        key = _ALIAS.get(fold(row["brand"]), fold(row["brand"]))
        if not key:
            continue
        shops.setdefault(key, set()).add(row["store_id"])
        spellings.setdefault(key, {})
        spellings[key][row["brand"].strip()] = spellings[key].get(row["brand"].strip(), 0) + 1

    index: dict[str, str] = {}
    for key, stores in shops.items():
        if len(stores) < MIN_BRAND_SHOPS:
            continue
        index[key] = max(spellings[key].items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
    return index


def canonical_brand(
    vendor: str | None, index: dict[str, str]
) -> tuple[str | None, str | None]:
    """(canonical brand, family) for a vendor string, or (None, None) if unknown.

    Unknown is a real answer here. A vendor no other shop has heard of is the
    shop's own name far more often than it is a brand, and writing it into the
    brand column is how the field got useless in the first place.
    """
    key = _ALIAS.get(fold(vendor), fold(vendor))
    if not key or key not in index:
        return None, None
    canonical = index[key]
    return canonical, _FAMILY.get(key, canonical)


# --- gender -----------------------------------------------------------------
# Ordered: the women's patterns run first because "women" contains "men", and
# "Nike Air Max Women's" would otherwise be read as men's by the shorter word.
#
# "boys" and "girls" are deliberately absent, though both once sat here. They
# are not adult words at all — a boys' t-shirt is a child's — and as gender
# signals they were wrong twice over. Measured on the live catalogue: 3,917
# products match one of the two as a whole word, and most of them are neither
# menswear nor childrenswear but a name. Billionaire Boys Club is 386 products
# of adult streetwear, the Powerpuff Girls are 61 SB Dunks and backpacks, and
# JACKBOYS, Concrete Boys, Bayou Boys and Bronx Girls Skate are collections.
# Every one of them was being filed as men's or women's on the strength of a
# brand name. What a boys' department really looks like is a possessive, and
# that reading lives in `audience` below.
_WOMEN = re.compile(
    r"\b(w(?:o)?m(?:e|a)ns?|wmns|womens?|damen|femme|feminin|mujer|donna|dames|"
    r"ladies|female)\b|\bw\.?\s?nsw\b",
    re.I,
)
_MEN = re.compile(
    r"\b(mens?|herren|homme|hombre|uomo|heren|male)\b",
    re.I,
)


def gender(title: str | None, category: str | None = None) -> str | None:
    """'women', 'men', or None. None means the catalogue did not say."""
    haystack = f"{title or ''} {category or ''}"
    if _WOMEN.search(haystack):
        return "women"
    if _MEN.search(haystack):
        return "men"
    return None


# --- audience ---------------------------------------------------------------
# Whether a thing is for a child. Asked separately from gender because it is a
# separate question, and answered from the title and the category separately
# because the two fields lie in different ways.
#
# A title is prose written by a marketing department, so the reading has to be
# strict. Measured across the 660,470 products in the live catalogue, these are
# the words that turned out not to survive:
#
#   "baby"  — 1,543 matches, and the common ones are a women's "Baby Tee", a
#             BAPE "Baby Milo" and a "Baby Blue" colourway. Dropped entirely.
#             Real infant clothing almost always says infant, toddler or
#             newborn as well, or carries a children's category.
#   "youth" — 1,749 matches, split between a size class ("Uno Gen1 - Youth")
#             and a brand ("World Industries Youth Classic Hoodie"). Kept only
#             where it trails as a qualifier, which is how a size class reads.
#   "boys"  — see the note on gender above. Kept only as a possessive followed
#   "girls"   by a word, which is how a department reads and how a collection's
#             name does not: "- Boys' Grade School" against "'Concrete Boys'".
#   "child" — matches Star Wars' "The Child" and Polar's "Angel Child"
#             colourway. Kept only as the possessive or the plural.
#
# A category is a taxonomy path rather than prose — "kids/girls-clothing/
# dresses", "Toddler/Preschool" — so the plain words are safe there. Checked:
# of 100 distinct categories carrying one of these words, not one is a brand
# name.
_KIDS_TITLE = re.compile(
    r"\bkids?\b|\bkid[’']s\b|\bchildren[’']?s?\b|\bchild[’']s\b"
    r"|\btoddlers?\b|\binfants?\b|\bnewborns?\b|\bjuniors?\b|\bpreschool(?:ers?)?\b"
    r"|\b(?:grade|pre)[-\s]?school\b"
    # Nike and Jordan size classes: grade school, pre-school, toddler, and the
    # rest of the family. Written in brackets by every shop that uses them.
    r"|\((?:GS|PS|TD|BP|BT|BG|GG|PT)\)|\b(?:GS|TD|PS)/"
    r"|\b(?:boys|girls)[’'](?=\s+\w)|[-–]\s*(?:boys|girls)[’']\s*$"
    r"|\b(?:big|little)\s+kids?[’']?\b"
    r"|[-–(]\s*youth\b|\byouth\s+sizes?\b"
    # A youth shoe size, as the shops write it: 5Y, 6.5Y.
    r"|\b\d+(?:\.5)?Y\b",
    re.I,
)
_KIDS_CATEGORY = re.compile(
    r"\b(kids?|child|children|childrens|junior|juniors|toddler|toddlers|infant|"
    r"infants|baby|babies|newborn|boys?|girls?|youth|nursery|"
    r"pre[-\s]?school|grade[-\s]?school|gradeschool)\b",
    re.I,
)

# Bare "GS" is deliberately not read as grade school. It would add 1,886
# products, and it is a model code as often as a size class: "Nike Dunk Low GS"
# is a child's shoe and "GS Air Paris Pocket T-Shirt" is not. Every shop that
# means the size class also writes it in brackets or spells out "Grade School",
# both of which are read above, so the cost of leaving it out is small and the
# cost of guessing wrong is a find that never arrives.


def audience(title: str | None, category: str | None = None) -> str | None:
    """'kids', or None meaning nothing here says it is for a child.

    None is not "adult". Most of the catalogue says nothing either way, exactly
    as with gender, and the filters treat silence as "show it".
    """
    if title and _KIDS_TITLE.search(title):
        return "kids"
    if category and _KIDS_CATEGORY.search(category):
        return "kids"
    return None


# --- kind -------------------------------------------------------------------
_SHOE_SIZE = re.compile(r"^(US|EU|UK)\d")
_CLOTHING_SIZE = frozenset(
    {"XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL", "4XL", "L/XL", "S/M"}
)

# Words that settle it regardless of the size, because Italian clothing sizing
# collides head-on with shoe sizing: a Stone Island jacket is a 48 exactly the
# way a shoe is a 44, and both arrive as "EU48".
_CLOTHING_WORDS = re.compile(
    r"\b(jacket|coat|parka|hoodie|hoody|sweat|sweater|crewneck|jumper|knit|"
    # "top" on its own is not a garment word: "Low Top Sneakers" and "High Top"
    # are shoes, and there are 6,735 of the former in the catalogue. The forms
    # that really are garments are spelled out instead.
    r"cardigan|shirt|tee|t-shirt|crop\s?top|tank\s?top|polo|vest|gilet|pants?|trousers?|jeans|"
    r"bermuda|chinos?|joggers?|leggings?|sweatpants|tracksuit|"
    r"shorts?|skirt|dress|overshirt|blouson|anorak|fleece|track\s?suit|"
    r"jacke|hose|pullover|chaqueta|pantalon)\b",
    re.I,
)
_SHOE_WORDS = re.compile(
    r"\b(sneakers?|shoes?|trainers?|boots?|runners?|footwear|sandals?|slides?|"
    r"clogs?|mules?|loafers?|calzado|schuhe|chaussures?|scarpe)\b",
    re.I,
)
_ACCESSORY_WORDS = re.compile(
    r"\b(cap|hat|beanie|bucket|bag|backpack|tote|pouch|wallet|belt|socks?|"
    r"gloves?|scarf|sunglasses|glasses|watch|keychain|lanyard|towel|"
    r"accessor(?:y|ies)|m(?:ü|u)tze|tasche)\b",
    re.I,
)

_SIZE_NUMBER = re.compile(r"^(?:US|EU|UK)(\d+(?:\.5)?)$")


def _sized_like_italian_clothing(sizes: set[str]) -> bool:
    """Do these numbers step by two, the way jacket sizes do?

    Italian clothing sizing collides with shoe sizing on the numbers themselves —
    a C.P. Company bermuda is a 46, 48, 50 and a Premiata sneaker is a 43, 44,
    45 — but not on their shape. Garments come in even numbers two apart;
    shoes come in consecutive numbers and half sizes. That is the whole rule,
    and it needs no vocabulary to maintain.

    Three distinct sizes are demanded before it fires, because two numbers two
    apart are also just two shoe sizes with one sold out.
    """
    numbers = sorted(
        {float(m.group(1)) for s in sizes if (m := _SIZE_NUMBER.match(s))}
    )
    if len(numbers) < 3:
        return False
    if any(n != int(n) or int(n) % 2 for n in numbers):
        return False
    return all(b - a == 2 for a, b in pairwise(numbers))


def kind(
    title: str | None,
    category: str | None,
    size_norms: Iterable[str | None] = (),
) -> str | None:
    """'shoes', 'clothing', 'accessories', or None.

    Words first where they are unambiguous, sizes second. The order matters only
    for numeric sizes, and only because two size systems share the same numbers.
    """
    haystack = f"{title or ''} {category or ''}"
    shoe_word = bool(_SHOE_WORDS.search(haystack))
    clothing_word = bool(_CLOTHING_WORDS.search(haystack))
    if shoe_word and not clothing_word:
        return "shoes"
    if clothing_word and not shoe_word:
        return "clothing"
    if _ACCESSORY_WORDS.search(haystack) and not (shoe_word or clothing_word):
        return "accessories"

    sizes = {s for s in size_norms if s}
    if _sized_like_italian_clothing(sizes):
        return "clothing"
    if any(_SHOE_SIZE.match(s) for s in sizes):
        return "shoes"
    if sizes & _CLOTHING_SIZE:
        return "clothing"
    if "OS" in sizes:
        return "accessories"
    return None


# --- writing it into the database -------------------------------------------


def _borrow_gender_by_article(
    conn: sqlite3.Connection, known: dict[int, str]
) -> dict[int, str]:
    """Spread a known gender across shops along the manufacturer's article number.

    One shop writes "Wmns Air Force 1", the next sells the same article as "Air
    Force 1". The article number is the same in both, so the second can borrow
    what the first said. Measured on the live catalogue this adds 16,874
    products to 68,718 — coverage goes from 15.3% to 19.1%. Modest, because the
    ceiling is that most of the catalogue never states a gender at all.

    Only sku and style keys are used. A title key is the title, so borrowing
    along it would restate the reading we already did. Articles whose members
    disagree are dropped rather than voted on: 83 of 2.26 million, and a
    disagreement means one of the two readings is wrong.
    """
    by_key: dict[str, list[int]] = {}
    for product_id, key in conn.execute(
        "SELECT product_id, key FROM product_keys WHERE key_type IN ('sku', 'style')"
    ):
        by_key.setdefault(key, []).append(product_id)

    borrowed: dict[int, str] = {}
    contested: set[int] = set()
    for members in by_key.values():
        votes = {known[p] for p in members if p in known}
        if len(votes) != 1:
            continue
        value = votes.pop()
        for product_id in members:
            if product_id in known or product_id in contested:
                continue
            if borrowed.setdefault(product_id, value) != value:
                del borrowed[product_id]
                contested.add(product_id)
    return borrowed


def _borrow_gender_from_stored(
    conn: sqlite3.Connection, product_ids: list[int]
) -> dict[int, str]:
    """The same borrowing, asked of the database instead of held in memory.

    Used when only a slice is being classified: scanning 2.26 million keys to
    settle a few thousand products is the wrong way round.

    A product is not among its own witnesses. It reached this function precisely
    because its own title says nothing, so the only gender it could contribute
    is one it borrowed on an earlier run — and counting that is the same shop
    corroborating itself, which every other reading here refuses. It also gets
    the answer wrong in the one case that matters: when the shop that named the
    gender corrects itself, the stale value disagrees with the new one, the
    article is dropped as contested, and a product that should have followed the
    correction loses its gender instead.
    """
    borrowed: dict[int, str] = {}
    for start in range(0, len(product_ids), 900):
        chunk = product_ids[start : start + 900]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT mine.product_id AS id, theirs.gender AS gender
              FROM product_keys mine
              JOIN product_keys sibling
                ON sibling.key = mine.key AND sibling.key_type = mine.key_type
               AND sibling.product_id <> mine.product_id
              JOIN products theirs ON theirs.id = sibling.product_id
             WHERE mine.product_id IN ({placeholders})
               AND mine.key_type IN ('sku', 'style')
               AND theirs.gender IS NOT NULL
             GROUP BY mine.product_id
            HAVING COUNT(DISTINCT theirs.gender) = 1
            """,
            chunk,
        )
        for row in rows:
            borrowed[row["id"]] = row["gender"]
    return borrowed


def classify(
    conn: sqlite3.Connection, product_ids: Collection[int] | None = None
) -> dict[str, int]:
    """Fill brand_norm, brand_family, gender, kind and audience.

    With no ids this rebuilds the whole catalogue, which is what `pi reclassify`
    does; a run passes the products it just collected, because reclassifying
    449,110 rows to record what changed about nine thousand is twenty seconds
    and a large write for no gain.

    The classification is always rebuilt rather than patched, even for a subset.
    The brand index is derived from the catalogue, so a shop added last week can
    turn a vendor only one shop used into a corroborated brand — and then every
    product carrying it changes answer at once, including ones nobody touched.
    That is what the full rebuild is for, and why it stays a command you can run.
    """
    index = load_brand_index(conn)
    if product_ids is None:
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.brand, p.category,
                   group_concat(v.size_norm, '|') AS sizes
              FROM products p
              LEFT JOIN variants v ON v.product_id = p.id
             GROUP BY p.id
            """
        ).fetchall()
    else:
        ids = list(product_ids)
        if not ids:
            return {"products": 0, "brands": len(index), "brand": 0, "kind": 0,
                    "kids": 0, "gender_stated": 0, "gender_borrowed": 0}
        rows = []
        # Chunked because SQLite caps a statement at 999 parameters by default.
        for start in range(0, len(ids), 900):
            chunk = ids[start : start + 900]
            rows.extend(
                conn.execute(
                    f"""
                    SELECT p.id, p.title, p.brand, p.category,
                           group_concat(v.size_norm, '|') AS sizes
                      FROM products p
                      LEFT JOIN variants v ON v.product_id = p.id
                     WHERE p.id IN ({",".join("?" * len(chunk))})
                     GROUP BY p.id
                    """,
                    chunk,
                ).fetchall()
            )

    stated: dict[int, str] = {}
    updates: list[tuple] = []
    stats = {"products": len(rows), "brands": len(index), "brand": 0, "kind": 0,
             "kids": 0}
    for row in rows:
        brand, family = canonical_brand(row["brand"], index)
        who = gender(row["title"], row["category"])
        what = kind(row["title"], row["category"], (row["sizes"] or "").split("|"))
        for_whom = audience(row["title"], row["category"])
        if brand:
            stats["brand"] += 1
        if what:
            stats["kind"] += 1
        if for_whom:
            stats["kids"] += 1
        if who:
            stated[row["id"]] = who
        updates.append((brand, family, who, what, for_whom, row["id"]))

    if product_ids is None:
        borrowed = _borrow_gender_by_article(conn, stated)
    else:
        # A run classifies a slice, but it should still borrow from the whole
        # catalogue: the shop that spells out "Wmns" is usually not the shop
        # being collected right now. So the siblings are read from the database
        # rather than from what this pass happened to look at.
        borrowed = _borrow_gender_from_stored(
            conn, [r["id"] for r in rows if r["id"] not in stated]
        )
    if borrowed:
        updates = [
            (b, f, g or borrowed.get(pid), k, a, pid) for b, f, g, k, a, pid in updates
        ]
    stats["gender_stated"] = len(stated)
    stats["gender_borrowed"] = len(borrowed)

    with conn:
        conn.executemany(
            """UPDATE products
                  SET brand_norm = ?, brand_family = ?, gender = ?, kind = ?,
                      audience = ?
                WHERE id = ?""",
            updates,
        )
    return stats
