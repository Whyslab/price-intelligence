"""Reference prices: the shop's own floor, and what other shops say."""
from __future__ import annotations

import pytest

from pi import db as dbm
from pi import reference
from pi.domains import same_host, same_shop

from .conftest import make_history, ts


class TestKeys:
    """Matching a product across shops that name it nothing alike."""

    def test_the_manufacturers_article_number_is_found_in_a_sku(self):
        keys = reference.keys_for("Nike", "Air Force 1", ["CW2288-111"])
        assert ("style", "CW2288-111") in keys

    def test_it_is_found_in_a_title_too(self):
        """Plenty of shops put the code in the name and nowhere else."""
        keys = reference.keys_for("Nike", "Air Force 1 Low White CW2288-111", [None])
        assert ("style", "CW2288-111") in keys

    def test_a_code_buried_in_a_decorated_sku_still_counts(self):
        """Live SKUs read like "(Gold) FV5029-003"."""
        keys = reference.keys_for("Jordan", "4 Retro Rare Air", ["(Gold) FV5029-003"])
        assert ("style", "FV5029-003") in keys

    def test_two_shops_naming_the_same_shoe_differently_still_meet(self):
        one = reference.keys_for(
            "Nike", "Nike Kobe 4 Protro Draft Day Pack (Opened Packaging) Sz 9", ["IV6585-900"]
        )
        other = reference.keys_for("Nike", "Kobe 4 Protro Draft Day Pack", ["IV6585-900"])
        assert one & other

    def test_a_short_or_missing_sku_contributes_nothing(self):
        keys = reference.keys_for("Acme", "Sock", ["12"])
        assert not any(key_type == "sku" for key_type, _ in keys)

    @pytest.mark.parametrize(
        "brand,code",
        [
            ("New Balance", "M2002RDB"),   # in 13 shops
            ("New Balance", "U204LMMA"),   # in 18
            ("New Balance", "CT302OE"),
            ("ASICS", "1201A019-021"),     # Gel-Kayano 14, in 14 shops
            ("Puma", "635235-01"),
            ("Converse", "162050C"),       # Chuck 70, in 19 shops
            ("adidas", "IF4396"),
            ("Nike", "414571-102"),
        ],
    )
    def test_the_article_numbers_the_other_brands_use(self, brand, code):
        """Nike and adidas were the only shapes recognised, so everyone else was
        matched on their title or not at all.

        Which shapes to add was measured on the whole catalogue by how many codes
        end up shared by three shops or more — the thing a market price needs —
        rather than by how many products gain a code.
        """
        assert ("style", code) in reference.keys_for(brand, f"{brand} Shoe {code}", [None])

    @pytest.mark.parametrize(
        "sku",
        [
            "197595459334",   # a barcode
            "19389900015",    # a shop's own stock number
            "1234567C",       # too long to be the Converse form
        ],
    )
    def test_numbers_that_are_not_article_numbers_are_left_alone(self, sku):
        """A false match claims two different shoes are the same one, and the
        market price it then computes is arithmetic on unrelated products.
        """
        assert not reference.style_codes(sku)


class TestMsrpMode:
    """A real recommended price is copied by everyone; an invented one by nobody."""

    def test_the_crowd_wins_over_the_outlier(self):
        assert reference.mode_price([200.0, 200.0, 205.0, 199.0, 300.0]) == pytest.approx(200.0, abs=3)

    def test_a_single_claim_is_returned_as_itself(self):
        assert reference.mode_price([300.0]) == 300.0

    def test_nothing_to_go_on(self):
        assert reference.mode_price([]) is None


class TestPriorFloor:
    """The lowest price the shop really charged before the current one."""

    def test_a_price_raised_and_dropped_back_has_not_dropped(self):
        """The whole point. 200 for two months, 300 for a week, 200 again."""
        history = make_history([(200.0, None, 60), (300.0, None, 7), (200.0, None, 0)])
        floor = reference.prior_floor(history, window_days=30)
        assert floor is not None
        assert floor.lowest_native == 200.0, "the week at 300 does not raise the floor"

    def test_a_genuine_drop_is_below_the_floor(self):
        history = make_history([(200.0, None, 60), (200.0, None, 20), (140.0, None, 0)])
        floor = reference.prior_floor(history, window_days=30)
        assert floor is not None
        assert floor.lowest_native == 200.0
        assert floor.covered_days == pytest.approx(30.0, abs=0.1)

    def test_a_price_that_never_moved_has_no_floor_behind_it(self):
        history = make_history([(200.0, None, 60), (200.0, None, 0)])
        assert reference.prior_floor(history, window_days=30) is None

    def test_prices_that_left_the_window_are_not_counted(self):
        """A cheaper price from six months ago is not what it was before the drop."""
        history = make_history([(90.0, None, 200), (200.0, None, 60), (140.0, None, 0)])
        floor = reference.prior_floor(history, window_days=30)
        assert floor is not None
        assert floor.lowest_native == 200.0

    def test_one_observation_says_nothing(self):
        assert reference.prior_floor(make_history([(200.0, None, 0)]), window_days=30) is None

    def test_a_shop_that_changed_currency_is_not_compared_to_itself(self):
        """120 GBP and 120 EUR are not the same price, and reading them as one
        invents a discount out of nothing."""
        history = make_history(
            [(220.0, None, 40, "GBP"), (200.0, None, 20, "GBP"), (150.0, None, 0, "EUR")]
        )
        assert reference.prior_floor(history, window_days=30) is None


class TestDomains:
    def test_www_is_decoration(self):
        assert same_host("www.smoothitalia.com") == same_host("smoothitalia.com")

    def test_a_storefront_subdomain_is_the_same_merchant(self):
        assert same_shop("shop.bdgastore.com") == same_shop("bdgastore.com")

    def test_different_shops_stay_different(self):
        assert same_shop("kith.com") != same_shop("feature.com")

    def test_a_chain_in_four_countries_is_one_opinion_not_four(self):
        """Foot Locker's own pricing must not corroborate Foot Locker."""
        theirs = {same_shop(d) for d in (
            "www.footlocker.com", "www.footlocker.de",
            "www.footlocker.fr", "www.footlocker.it",
        )}
        assert len(theirs) == 1

    def test_a_chain_trading_under_another_name_abroad_is_still_one_shop(self):
        assert same_shop("www.snipesusa.com") == same_shop("www.snipes.com")

    def test_a_two_label_country_suffix_is_not_mistaken_for_the_name(self):
        """size.co.uk is "size", not "size.co" — otherwise nothing would merge."""
        assert same_shop("www.size.co.uk") == "size"
        assert same_shop("m.size.co.uk") == same_shop("www.size.co.uk")
        assert same_shop("www.jdsports.co.uk") == same_shop("www.jdsports.com")

    def test_unrelated_shops_are_still_told_apart(self):
        """Dropping the country must not start merging everyone."""
        distinct = [
            "kith.com", "feature.com", "www.ssense.com", "sneakerpolitics.com",
            "extrabutterny.com", "www.slamjam.com", "shop.ccs.com",
        ]
        assert len({same_shop(d) for d in distinct}) == len(distinct)


class TestMarketIndex:
    """What everybody else is charging, read out of the database."""

    def _stock(self, conn, domain, sku, price, compare=None, in_stock=True):
        store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
        product = dbm.upsert_product(conn, store, sku, f"Shoe {sku}", f"https://{domain}/p")
        dbm.set_product_keys(conn, product, reference.keys_for("Nike", f"Shoe {sku}", [sku]))
        variant = dbm.upsert_variant(conn, product, "v1", sku=sku)
        dbm.record_price(
            conn, variant, price, compare, in_stock, "USD", price, 1.0,
            ts=ts(0), compare_at_native=compare,
        )
        return product

    def test_a_shop_is_not_counted_as_its_own_witness(self, conn):
        mine = self._stock(conn, "mine.example", "CW2288-111", 200.0)
        self._stock(conn, "www.mine.example", "CW2288-111", 200.0)
        index = reference.build_market_index(conn)
        assert index.look_up(mine, same_shop("mine.example")).shops == 0

    def test_prices_from_other_shops_become_a_median_and_a_low(self, conn):
        mine = self._stock(conn, "mine.example", "CW2288-111", 200.0)
        for n, price in enumerate((150.0, 170.0, 190.0)):
            self._stock(conn, f"other{n}.example", "CW2288-111", price)

        market = reference.build_market_index(conn).look_up(mine, "mine.example")
        assert market.shops == 3
        assert market.median_usd == 170.0
        assert market.low_usd == 150.0

    def test_out_of_stock_prices_are_not_the_market(self, conn):
        mine = self._stock(conn, "mine.example", "CW2288-111", 200.0)
        self._stock(conn, "other.example", "CW2288-111", 90.0, in_stock=False)
        assert reference.build_market_index(conn).look_up(mine, "mine.example").shops == 0

    def test_the_recommended_price_is_what_most_shops_strike_through(self, conn):
        mine = self._stock(conn, "mine.example", "CW2288-111", 200.0, compare=300.0)
        for n in range(4):
            self._stock(conn, f"other{n}.example", "CW2288-111", 180.0, compare=200.0)

        market = reference.build_market_index(conn).look_up(mine, "mine.example")
        assert market.msrp_shops == 4
        assert market.msrp_usd == pytest.approx(200.0, abs=1)


class TestStoreTrust:
    """Telling a shop that remembers former prices from one that computes them."""

    def _catalogue(self, conn, domain, count, discounts):
        """Stock `count` items, cycling through `discounts` percent off."""
        store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
        for n in range(count):
            product = dbm.upsert_product(conn, store, f"p{n}", f"T{n}", "https://u")
            variant = dbm.upsert_variant(conn, product, f"v{n}")
            price = 100.0
            compare = round(price / (1 - discounts[n % len(discounts)] / 100), 2)
            dbm.record_price(
                conn, variant, price, compare, True, "USD", price, 1.0,
                ts=ts(0), compare_at_native=compare,
            )
        return store

    def test_one_percentage_across_the_catalogue_is_a_promotion(self, conn):
        store = self._catalogue(conn, "blanket.example", 200, [40])
        trust = reference.store_trust(conn)[store]
        assert trust.blanket_pct == 40.0
        assert trust.rule_priced(0.9, 0.3)

    def test_a_ladder_of_round_steps_is_a_promotion_too(self, conn):
        """The live case no single-bucket test catches: 40/45/30/50/60/35 across
        6,763 tagged products, the largest step only 27% of them."""
        store = self._catalogue(conn, "ladder.example", 300, [40, 45, 30, 50, 60, 35])
        trust = reference.store_trust(conn)[store]
        assert trust.round_share == 1.0
        assert trust.blanket_share < 0.3, "no one percentage dominates"
        assert trust.rule_priced(0.9, 0.3)

    def test_a_shop_pricing_products_individually_is_not(self, conn):
        """Former prices that were really charged land wherever the arithmetic
        puts them."""
        awkward = [17.3, 22.8, 36.4, 41.1, 12.7, 28.9, 33.2, 19.6]
        store = self._catalogue(conn, "honest.example", 200, awkward)
        trust = reference.store_trust(conn)[store]
        assert trust.round_share < 0.5
        assert not trust.rule_priced(0.9, 0.3)

    def test_a_handful_of_products_is_not_a_policy(self, conn):
        """Three things on sale together is a small shop, not a pricing rule."""
        store = self._catalogue(conn, "tiny.example", 3, [40])
        assert not reference.store_trust(conn)[store].rule_priced(0.9, 0.3)

    def test_a_shop_that_barely_discounts_anything_is_not_judged(self, conn):
        """A handful of round markdowns in a large catalogue says nothing."""
        store = dbm.upsert_store(conn, "quiet.example", platform="shopify", currency="USD")
        for n in range(400):
            product = dbm.upsert_product(conn, store, f"p{n}", f"T{n}", "https://u")
            variant = dbm.upsert_variant(conn, product, f"v{n}")
            compare = 200.0 if n < 60 else None
            dbm.record_price(
                conn, variant, 100.0, compare, True, "USD", 100.0, 1.0,
                ts=ts(0), compare_at_native=compare,
            )
        trust = reference.store_trust(conn)[store]
        assert trust.round_share == 1.0, "the few there are, are round"
        assert not trust.rule_priced(0.9, 0.3), "but they are 15% of the catalogue"

    def test_a_struck_through_price_equal_to_the_asking_price_is_not_a_discount(self, conn):
        """One shop carries 20,185 of these, which would read as a catalogue
        almost entirely on sale."""
        store = self._catalogue(conn, "noise.example", 200, [0.4])
        assert reference.store_trust(conn)[store].tagged == 0
