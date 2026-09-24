"""Brand, gender, kind and audience: what the shops did not tell us."""
from __future__ import annotations

from typing import ClassVar

import pytest

from pi import db as dbm
from pi import taxonomy


def add_product(conn, store_id, external_id, title, brand=None, category=None, sizes=()):
    product_id = dbm.upsert_product(
        conn,
        store_id=store_id,
        external_id=external_id,
        title=title,
        url=f"https://example.com/{external_id}",
        brand=brand,
        category=category,
    )
    for index, size in enumerate(sizes):
        dbm.upsert_variant(
            conn,
            product_id=product_id,
            external_id=f"{external_id}-{index}",
            sku=None,
            size=size,
            size_norm=size,
            color=None,
        )
    return product_id


@pytest.fixture
def shops(conn):
    return [dbm.upsert_store(conn, f"shop{n}.com") for n in range(5)]


class TestBrand:
    """A vendor field is a free-text box, and it shows."""

    def test_spellings_of_one_brand_fold_together(self):
        assert taxonomy.fold("C.P. Company") == taxonomy.fold("CP COMPANY")
        assert taxonomy.fold("Nike") == taxonomy.fold("NIKE_")

    def test_a_vendor_several_shops_use_is_a_brand(self, conn, shops):
        for shop in shops[:3]:
            add_product(conn, shop, f"p{shop}", "Air Force 1", brand="Nike")
        index = taxonomy.load_brand_index(conn)
        assert taxonomy.canonical_brand("NIKE", index)[0] == "Nike"

    def test_a_vendor_only_one_shop_uses_is_that_shop_naming_itself(self, conn, shops):
        """CommonGround12 and CCS are shops, and they are why this rule exists."""
        for n in range(4):
            add_product(conn, shops[0], f"p{n}", "Tee", brand="CommonGround12")
        index = taxonomy.load_brand_index(conn)
        assert taxonomy.canonical_brand("CommonGround12", index) == (None, None)

    def test_the_canonical_spelling_is_the_one_most_shops_use(self, conn, shops):
        """Not the one most products use: one loud shop must not outvote three."""
        for n in range(50):
            add_product(conn, shops[0], f"loud{n}", "Air Force 1", brand="NIKE")
        for shop in shops[1:4]:
            add_product(conn, shop, f"quiet{shop}", "Air Force 1", brand="Nike")
        index = taxonomy.load_brand_index(conn)
        assert taxonomy.canonical_brand("nike", index)[0] == "Nike"

    def test_a_sub_brand_keeps_its_name_and_gains_a_family(self, conn, shops):
        for shop in shops[:3]:
            add_product(conn, shop, f"j{shop}", "Air Jordan 1", brand="Jordan")
        index = taxonomy.load_brand_index(conn)
        assert taxonomy.canonical_brand("Jordan", index) == ("Jordan", "Nike")


class TestGender:
    def test_women_is_read_before_men_because_the_word_contains_it(self):
        assert taxonomy.gender("Nike Air Force 1 Women's") == "women"
        assert taxonomy.gender("Wmns Air Max 90") == "women"

    def test_men_is_read_in_the_languages_the_shops_use(self):
        assert taxonomy.gender("Herren Jacke") == "men"
        assert taxonomy.gender("Chaussures Homme") == "men"

    def test_saying_nothing_is_an_answer(self):
        """87% of the catalogue lands here, and a guess would be indistinguishable."""
        assert taxonomy.gender("Air Force 1 '07") is None

    def test_a_brand_called_boys_or_girls_is_not_a_gender(self):
        """Measured: 3,917 products match one of the two, and most are names.

        Billionaire Boys Club was filed as menswear and the Powerpuff Girls
        collaboration as womenswear, on the strength of a collection's name.
        """
        assert taxonomy.gender("Billionaire Boys Club Curve Logo SS Tee") is None
        assert taxonomy.gender("Nike SB Dunk Low The Powerpuff Girls Bubbles") is None
        assert taxonomy.gender("Travis Scott JACKBOYS Vehicle Hoodie Black") is None

    def test_a_childs_department_is_not_an_adult_gender(self):
        """A boys' grade school shoe is a child's, and `audience` is where that lives."""
        assert taxonomy.gender("Saucony Omni 9 - Boys' Grade School") is None
        assert taxonomy.gender("Jordan Flowy Shorts Set - Girls' Infant") is None


class TestAudience:
    """Every case here was found in the live catalogue, not invented."""

    def test_the_words_that_really_mean_a_child(self):
        assert taxonomy.audience("Nike Dunk Low (GS)") == "kids"
        assert taxonomy.audience("Jordan True Flight Toddler") == "kids"
        assert taxonomy.audience("Saucony Omni 9 - Boys' Grade School") == "kids"
        assert taxonomy.audience("Kith Kids Nelson Sweatpant - Rogue") == "kids"
        assert taxonomy.audience("Air Jordan 1 Retro High OG GS Big Kid's") == "kids"
        assert taxonomy.audience("Jordan 1 Low SE Dune Red (GS) Sz 7Y") == "kids"
        assert taxonomy.audience("Vans Sk8-Hi Mid Pop Check - Youth Sneakers") == "kids"

    def test_a_collection_named_after_children_is_not_for_them(self):
        """The reason bare "boys", "girls" and "baby" are not signals.

        1,543 products match "baby" and the common ones are a women's Baby Tee
        and a BAPE Baby Milo; 3,917 match "boys" or "girls" and most of those
        are Billionaire Boys Club and the Powerpuff Girls.
        """
        assert taxonomy.audience("Billionaire Boys Club Curve Logo SS Tee") is None
        assert taxonomy.audience("Powerpuff Girls Rainbow Shark Backpack") is None
        assert taxonomy.audience("Womens Crystal Soft Serve Baby Tee") is None
        assert taxonomy.audience("A Bathing Ape X OVO Baby Milo Tee - White") is None
        assert taxonomy.audience("Travis Scott JACKBOYS Vehicle Hoodie") is None
        assert taxonomy.audience("World Industries Youth Classic Hoodie") is None

    def test_a_closing_quote_is_not_a_possessive(self):
        """'Concrete Boys' ends in an apostrophe and is a Lil Yachty release."""
        assert taxonomy.audience("Lil Yachty US Force 1 'Concrete Boys'") is None
        assert taxonomy.audience("Jordan Air Nfh 'Bayou Boys'") is None

    def test_a_colourway_called_child_is_not_a_child(self):
        assert taxonomy.audience("Polar Big Boy Jeans - Lemon Black / Demon Child") is None
        assert taxonomy.audience("Bape Star Wars The Child Milo Tee") is None

    def test_a_small_adult_size_is_not_a_childs_size(self):
        """US5 women's shoes exist; 1,491 offers carry a US size below 6."""
        assert taxonomy.audience("Nike Dunk Low Women's US5") is None
        assert taxonomy.audience("Air Force 1 '07") is None

    def test_the_category_may_say_plainly_what_a_title_may_not(self):
        """A category is a taxonomy path, so the plain words are safe there."""
        assert taxonomy.audience("Nelson Sweatpant", "kids/boys-clothing/shirts-tops") == "kids"
        assert taxonomy.audience("Stan Smith", "Toddler/Preschool") == "kids"
        assert taxonomy.audience("Some Shoe", "Sneakers") is None


class TestKind:
    def test_a_size_says_what_a_missing_category_does_not(self):
        assert taxonomy.kind("Air Force 1", None, ["US10", "US10.5"]) == "shoes"
        assert taxonomy.kind("Logo Print", None, ["L", "XL"]) == "clothing"
        assert taxonomy.kind("Cap", None, ["OS"]) == "accessories"

    def test_a_garment_sized_the_italian_way_is_not_a_shoe(self, conn):
        """EU48 is a jacket here and a shoe elsewhere; the word settles it."""
        assert taxonomy.kind("Stone Island Jacket", None, ["EU48"]) == "clothing"

    def test_the_category_helps_when_the_title_says_nothing(self):
        assert taxonomy.kind("Chuck 70", "Low Top Sneakers", []) == "shoes"


class TestClassify:
    def test_it_writes_what_it_derives(self, conn, shops):
        for shop in shops[:3]:
            add_product(
                conn, shop, f"p{shop}", "Wmns Air Force 1",
                brand="Nike", category="Sneakers", sizes=["US7"],
            )
        stats = taxonomy.classify(conn)
        row = conn.execute(
            "SELECT brand_norm, brand_family, gender, kind, audience FROM products LIMIT 1"
        ).fetchone()
        assert (row["brand_norm"], row["brand_family"]) == ("Nike", "Nike")
        assert row["gender"] == "women"
        assert row["kind"] == "shoes"
        assert row["audience"] is None
        assert stats["brand"] == 3

    def test_it_writes_the_audience_too(self, conn, shops):
        add_product(conn, shops[0], "grown", "Air Force 1 '07", sizes=["US10"])
        add_product(conn, shops[0], "small", "Air Force 1 (GS)", sizes=["US5"])
        stats = taxonomy.classify(conn)
        found = dict(conn.execute("SELECT external_id, audience FROM products"))
        assert found == {"grown": None, "small": "kids"}
        assert stats["kids"] == 1

    def test_reclassifying_can_take_a_misreading_back(self, conn, shops):
        """The point of storing it: a better rule costs a rerun, not a crawl."""
        product = add_product(conn, shops[0], "x", "Air Force 1 '07", sizes=["US10"])
        conn.execute("UPDATE products SET audience = 'kids' WHERE id = ?", (product,))
        taxonomy.classify(conn)
        assert conn.execute("SELECT audience FROM products").fetchone()[0] is None

    def test_a_gender_travels_along_the_article_number(self, conn, shops):
        """One shop spells out Wmns, the next does not, and it is the same shoe."""
        named = add_product(conn, shops[0], "a", "Wmns Air Force 1 CW2288-111")
        silent = add_product(conn, shops[1], "b", "Air Force 1 CW2288-111")
        for product_id in (named, silent):
            dbm.set_product_keys(conn, product_id, [("style", "CW2288-111")])
        taxonomy.classify(conn)
        rows = dict(conn.execute("SELECT id, gender FROM products"))
        assert rows[named] == "women"
        assert rows[silent] == "women"

    def test_classifying_a_slice_borrows_from_the_rest_of_the_catalogue(self, conn, shops):
        """A run collects one shop; the shop that named the gender is another one."""
        named = add_product(conn, shops[0], "a", "Wmns Air Force 1 CW2288-111")
        dbm.set_product_keys(conn, named, [("style", "CW2288-111")])
        taxonomy.classify(conn)

        silent = add_product(conn, shops[1], "b", "Air Force 1 CW2288-111")
        dbm.set_product_keys(conn, silent, [("style", "CW2288-111")])
        taxonomy.classify(conn, [silent])
        gender = conn.execute(
            "SELECT gender FROM products WHERE id = ?", (silent,)
        ).fetchone()[0]
        assert gender == "women"

    def test_a_product_is_not_its_own_witness(self, conn, shops):
        """The shop that named the gender corrects itself, and the borrower
        follows the correction rather than being blanked by its own stale vote."""
        named = add_product(conn, shops[0], "a", "Wmns Air Force 1 CW2288-111")
        dbm.set_product_keys(conn, named, [("style", "CW2288-111")])
        silent = add_product(conn, shops[1], "b", "Air Force 1 CW2288-111")
        dbm.set_product_keys(conn, silent, [("style", "CW2288-111")])
        taxonomy.classify(conn)
        assert conn.execute(
            "SELECT gender FROM products WHERE id = ?", (silent,)
        ).fetchone()[0] == "women"

        conn.execute("UPDATE products SET title = ? WHERE id = ?",
                     ("Mens Air Force 1 CW2288-111", named))
        taxonomy.classify(conn, [named])
        taxonomy.classify(conn, [silent])
        assert conn.execute(
            "SELECT gender FROM products WHERE id = ?", (silent,)
        ).fetchone()[0] == "men", "the correction reaches it, rather than blanking it"


class TestItalianSizing:
    """Where the two size systems collide: 46, 48, 50 is a jacket, 44, 45, 46 is a shoe."""

    def test_even_numbers_two_apart_are_a_garment(self):
        assert taxonomy.kind("Bermuda Cargo", None, ["EU46", "EU48", "EU50"]) == "clothing"

    def test_consecutive_numbers_are_a_shoe(self):
        sizes = ["EU42", "EU43", "EU44", "EU45"]
        assert taxonomy.kind("Premiata MASE25", None, sizes) == "shoes"

    def test_two_sizes_are_not_enough_to_call_it(self):
        """Two even numbers two apart are also two shoe sizes with one sold out."""
        assert taxonomy.kind("Runner", None, ["EU44", "EU46"]) == "shoes"

    def test_half_sizes_rule_it_out(self):
        sizes = ["EU44", "EU44.5", "EU46", "EU48"]
        assert taxonomy.kind("Runner", None, sizes) == "shoes"


class TestTheBrandTheShopWroteInTheTitle:
    """`canonical_brand` reads the vendor field and nothing else. 166,948
    products name no brand there, 10,151 of them standing on the shelf — dropped
    by `require_brand` before they can be a find, and invisible to the rule that
    refuses to call two products the same article when their brands differ."""

    INDEX: ClassVar[dict[str, str]] = {
        "nike": "Nike", "adidas": "adidas", "newbalance": "New Balance",
        "thenorthface": "The North Face", "balmain": "Balmain",
        "vintage": "Vintage",
    }

    def test_the_brand_at_the_front_is_taken(self):
        assert taxonomy.brand_from_title("Nike Air Max 90", self.INDEX)[0] == "Nike"

    def test_the_longest_name_wins(self):
        """"New" is a brand nowhere; "New Balance" is one."""
        assert (
            taxonomy.brand_from_title("New Balance 991v2 Made in UK", self.INDEX)[0]
            == "New Balance"
        )

    def test_a_word_that_merely_starts_like_a_brand_is_not_one(self):
        """Folding drops spaces, so a folded-prefix test would read this as Nike."""
        assert taxonomy.brand_from_title("Nikelodeon Slime Tee", self.INDEX) == (None, None)

    def test_a_condition_is_not_a_maker(self):
        """Enough shops write "Vintage" in the vendor field that the index
        promotes it, and then a Balmain blazer becomes a Vintage."""
        brand, _ = taxonomy.brand_from_title(
            "Vintage Balmain Paris Wool Blazer", self.INDEX
        )
        assert brand == "Balmain"
        assert taxonomy.canonical_brand("Vintage", self.INDEX) == (None, None)

    def test_the_words_shops_put_before_a_brand_are_stepped_over(self):
        brand, _ = taxonomy.brand_from_title(
            "PRE OWNED adidas Yeezy Boost 700", self.INDEX
        )
        assert brand == "adidas"

    def test_a_brand_named_only_in_the_middle_is_not_taken(self):
        """7,260 of these titles name two brands — a collaboration, where the
        second name is not the maker. Only the front is read."""
        assert taxonomy.brand_from_title(
            "Limited Edt GEL-Kayano 14 x Nike", self.INDEX
        ) == (None, None)

    def test_nothing_is_a_real_answer(self):
        assert taxonomy.brand_from_title("Some Unknown Thing", self.INDEX) == (None, None)
        assert taxonomy.brand_from_title(None, self.INDEX) == (None, None)


class TestTheWaysAShopSaysWomensWithoutTheWord:
    """Measured against the 18,083 shelf products the word-based rules leave
    unlabelled: a trailing W reaches 90 of them and a named garment 318. Small —
    about 2% — and clean, which is the only reason they are worth having."""

    def test_the_trailing_w_is_the_womens_cut_of_a_model(self):
        """adidas, Asics, On and Nike all write it this way."""
        assert taxonomy.gender("Superstar II W") == "women"
        assert taxonomy.gender("Gazelle Bold W") == "women"
        assert taxonomy.gender("Gel-1090 W") == "women"

    def test_a_lone_w_in_the_middle_is_not_read(self):
        """There it is an initial or a size, not a cut."""
        assert taxonomy.gender("W. Simmons Tee") is None
        assert taxonomy.gender("Samba OG") is None

    def test_a_named_garment_counts(self):
        assert taxonomy.gender("Monogram Spaghetti Mini Dress") == "women"
        assert taxonomy.gender("SANDALI ELEVATED ELEFTHERIA") == "women"
        assert taxonomy.gender("BORSA FLECA NOBUCK") == "women"

    def test_dress_as_a_colour_is_not_a_dress(self):
        """The false class here was not "dress shirt", which does not occur in
        these titles at all, but "Dress Blues" — a colour, on a DC sweatshirt."""
        assert taxonomy.gender("DC Cooper 1/4 Zip Sweat - Dress Blues") is None
        assert taxonomy.gender("Vans DNA Branding Sweatshirt - Dress Blues") is None

    def test_dress_as_an_adjective_is_not_one_either(self):
        assert taxonomy.gender("Brooks Brothers Dress Shirt") is None
        assert taxonomy.gender("Leather Dress Shoes") is None

    def test_the_word_still_wins_when_it_is_there(self):
        assert taxonomy.gender("Nike Women's Air Footscape") == "women"
        assert taxonomy.gender("Herren Jacke") == "men"
