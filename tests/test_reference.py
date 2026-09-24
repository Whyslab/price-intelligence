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

    def test_a_ninety_nine_percent_fall_is_a_data_error_not_a_sale(self):
        """Live: nine t-shirts recorded at 333,085,723 became a $151 shirt's floor.

        The shelf showed them at −100%, seven of them in the first screen. The
        ingestion ceiling stops that class of figure arriving, but a placeholder
        like topshelfslc.com's 99,999 against a median of 190 is not absurd
        enough to be caught there and still cannot be believed here.
        """
        history = make_history([(99_999.0, None, 20), (190.0, None, 0)])
        assert reference.prior_floor(history, window_days=30) is None

    def test_a_deep_but_believable_cut_still_counts(self):
        """−90% is a clearance, not a corrupt row, and must survive the guard."""
        history = make_history([(200.0, None, 20), (20.0, None, 0)])
        floor = reference.prior_floor(history, window_days=30)
        assert floor is not None and floor.lowest_native == 200.0

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


class TestWitnessesThatDisagree:
    """Article matching is a claim, and a wrong claim is invisible until it prices something."""

    def test_two_shops_far_apart_are_not_corroborating_each_other(self):
        """sneakers123.com asked $3,824 for a Vans another shop sold at $60.99.

        The median of two is their average, so the shelf showed a real $46 shoe
        as 98% off $1,942 — at the very top, sorted there by the size of the lie.
        """
        assert reference.agreeing_prices([60.99, 3824.0]) == []

    def test_two_shops_merely_one_on_sale_still_count(self):
        """The same shoe really is $90 in a sale and $220 at full price."""
        assert reference.agreeing_prices([90.0, 220.0]) == [90.0, 220.0]

    def test_a_third_shop_makes_the_middle_knowable_and_the_outlier_droppable(self):
        assert reference.agreeing_prices([60.0, 70.0, 3824.0]) == [60.0, 70.0]

    def test_a_lone_shop_is_left_alone(self):
        assert reference.agreeing_prices([100.0]) == [100.0]

    def test_a_free_listing_cannot_become_the_market(self):
        """A zero divides, and a shop that lists something at nothing is wrong."""
        assert reference.agreeing_prices([0.0, 120.0]) == []

    @staticmethod
    def _stock(conn, domain, sku, price):
        store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
        product = dbm.upsert_product(conn, store, sku, f"Shoe {sku}", f"https://{domain}/p")
        dbm.set_product_keys(conn, product, reference.keys_for("Nike", f"Shoe {sku}", [sku]))
        variant = dbm.upsert_variant(conn, product, "v1", sku=sku)
        dbm.record_price(
            conn, variant, price, None, True, "USD", price, 1.0, ts=dbm.utcnow()
        )
        return product

    def test_the_market_disappears_rather_than_being_invented(self, conn):
        """End to end: the shelf must have no reference at all here."""
        mine = self._stock(conn, "mine.example", "CW2288-111", 46.0)
        self._stock(conn, "junk.example", "CW2288-111", 3824.0)
        self._stock(conn, "sane.example", "CW2288-111", 61.0)

        market = reference.build_market_index(conn).look_up(mine, "mine.example")
        assert market.shops == 0
        assert market.median_usd is None

    def test_a_shop_agreeing_with_another_still_prices_the_thing(self, conn):
        mine = self._stock(conn, "mine.example", "CW2288-111", 46.0)
        self._stock(conn, "one.example", "CW2288-111", 120.0)
        self._stock(conn, "two.example", "CW2288-111", 100.0)

        market = reference.build_market_index(conn).look_up(mine, "mine.example")
        assert market.shops == 2
        assert market.median_usd == 110.0


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


class TestAShopDroppedForItsPriceIsDroppedForItsTag:
    """A shop rejected as not holding this article is rejected once, not twice.

    `agreeing_prices` drops a shop asking $3,824 where others ask $60 because it
    is describing something else. Its struck-through price is then a claim about
    that something else, and letting it vote on the recommended price reads the
    same rejected evidence a second time — which matters because the MSRP is
    what disqualifies an inflated tag, and 90% of the shelf is priced off tags.
    """

    _stock = TestMarketIndex._stock

    def test_a_rejected_shop_does_not_vote_on_the_recommended_price(self, conn):
        mine = self._stock(conn, "mine.example", "CW2288-111", 140.0, compare=400.0)
        for n, price in enumerate((100.0, 105.0, 110.0)):
            self._stock(conn, f"real{n}.example", "CW2288-111", price, compare=200.0 + n * 5)
        for n, price in enumerate((3800.0, 3850.0)):
            self._stock(conn, f"junk{n}.example", "CW2288-111", price, compare=450.0 + n * 5)

        market = reference.build_market_index(conn).look_up(mine, "mine.example")
        assert market.shops == 3, "the two disagreeing shops are not the market"
        assert market.msrp_shops == 3, "and they do not carry the recommended price either"
        assert market.msrp_usd == pytest.approx(205.0, abs=5), "the real shops' tags, not $450"

    def test_the_count_and_the_price_agree_on_who_is_in(self, conn):
        """Whatever else is true, no more shops may speak about the tag than
        about the price."""
        mine = self._stock(conn, "mine.example", "CW2288-111", 140.0)
        for n, price in enumerate((100.0, 105.0, 110.0, 3800.0, 3850.0, 3900.0)):
            self._stock(conn, f"other{n}.example", "CW2288-111", price, compare=price * 1.4)

        market = reference.build_market_index(conn).look_up(mine, "mine.example")
        assert market.msrp_shops <= market.shops

    def test_shops_that_all_agree_are_all_still_heard(self, conn):
        """The guard must not silence the signal it is guarding."""
        mine = self._stock(conn, "mine.example", "CW2288-111", 140.0)
        for n in range(4):
            self._stock(conn, f"other{n}.example", "CW2288-111", 180.0, compare=200.0)

        market = reference.build_market_index(conn).look_up(mine, "mine.example")
        assert market.shops == 4 and market.msrp_shops == 4
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


class TestCarharttWritesItsArticleInTwoFields:
    """`I036262 3AN0J` — the garment, then the colour, joined however the shop
    pleases. Neither half alone is the article: the code without a colour would
    merge every colourway of the same jacket, and comparing prices between those
    is comparing different things."""

    def test_both_halves_make_one_code(self):
        assert reference.style_codes("I036262 3AN0J PALISANDER") == {"I0362623AN0J"}

    def test_the_separator_does_not_matter(self):
        """Shops write a space, a dot or a dash for the same article."""
        assert (
            reference.style_codes("I031454.1ONXX")
            == reference.style_codes("I031454 1ONXX")
            == reference.style_codes("I031454-1ONXX")
        )

    def test_a_bare_style_without_a_colour_is_not_a_code(self):
        assert reference.style_codes("I036262") == set()

    def test_the_shapes_already_recognised_are_untouched(self):
        assert reference.style_codes("CW2288-111") == {"CW2288-111"}
        assert reference.style_codes("IF4396") == {"IF4396"}
        assert reference.style_codes("M990GL6") == {"M990GL6"}


class TestAKeyThatNamesTwoBrandsIsNotAnArticleNumber:
    """`\\d{6}-\\d{2}` was taken for Puma and also matches a shop's own id with a
    European size on the end, so `103134-40` claimed a Hey Dude and a Nike are
    the same shoe. Measured on the catalogue, 733 of the 18,617 style keys that
    link two shops join products whose brands are both known and different."""

    def _stock(self, conn, domain, brand, sku, price):
        store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
        product = dbm.upsert_product(
            conn, store, sku, f"{brand} thing", f"https://{domain}/p", brand=brand
        )
        conn.execute(
            "UPDATE products SET brand_family = ? WHERE id = ?", (brand, product)
        )
        dbm.set_product_keys(conn, product, {(reference.STYLE, "103134-40")})
        variant = dbm.upsert_variant(conn, product, "v1", sku=sku)
        dbm.record_price(
            conn, variant, price, None, True, "USD", price, 1.0, ts=ts(0)
        )
        return product

    def test_the_market_price_ignores_the_whole_key(self, conn):
        """Not just the odd row: a $30 cap among $200 sneakers moves the median
        every other offer in the group is judged against."""
        mine = self._stock(conn, "mine.example", "adidas", "a1", 200.0)
        self._stock(conn, "other.example", "Nike", "n1", 30.0)

        market = reference.build_market_index(conn).look_up(mine, "mine.example")
        assert market.shops == 0, "a cap is not the cheaper version of a sneaker"

    def test_brands_that_agree_still_count(self, conn):
        mine = self._stock(conn, "mine.example", "adidas", "a1", 200.0)
        self._stock(conn, "other.example", "adidas", "a2", 150.0)

        market = reference.build_market_index(conn).look_up(mine, "mine.example")
        assert market.shops == 1

    def test_silence_is_not_disagreement(self, conn):
        """78% of the catalogue names no brand, and a missing name contradicts
        nothing."""
        mine = self._stock(conn, "mine.example", "adidas", "a1", 200.0)
        nameless = self._stock(conn, "other.example", "adidas", "a2", 150.0)
        conn.execute(
            "UPDATE products SET brand_family = NULL, brand_norm = NULL, brand = NULL"
            " WHERE id = ?", (nameless,),
        )

        market = reference.build_market_index(conn).look_up(mine, "mine.example")
        assert market.shops == 1

    def test_the_product_card_does_not_offer_it_either(self, conn):
        """`same_article` is what the card's "cheaper elsewhere" reads."""
        mine = self._stock(conn, "mine.example", "adidas", "a1", 200.0)
        self._stock(conn, "other.example", "Nike", "n1", 30.0)

        assert dbm.same_article(conn, mine) == []
