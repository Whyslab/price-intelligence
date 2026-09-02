"""Brand, gender and kind: what the shops did not tell us."""
from __future__ import annotations

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
            "SELECT brand_norm, brand_family, gender, kind FROM products LIMIT 1"
        ).fetchone()
        assert (row["brand_norm"], row["brand_family"]) == ("Nike", "Nike")
        assert row["gender"] == "women"
        assert row["kind"] == "shoes"
        assert stats["brand"] == 3

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
