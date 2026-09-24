"""What counts as a deal, and what must not."""
from __future__ import annotations

from dataclasses import replace

import pytest

from pi import deals
from pi.config import Filters
from pi.reference import Market, Trust

from .conftest import make_history, ts


def test_tagged_discount_alone_is_the_last_resort_not_the_first(filters):
    """First ever sighting from a shop nobody else stocks: only the tag exists.

    It still counts — otherwise a new product could never be reported — but it
    earns no corroboration bonus, so it has to be a large discount on its own.
    """
    history = make_history([(120.0, 220.0, 0)])
    deal = deals.evaluate(1, 1, 120.0, 220.0, True, history, filters)

    assert deal is not None
    assert deal.reference_source == "tag"
    assert deal.discount_pct == pytest.approx(45.5, abs=0.1)
    assert deal.saving_usd == pytest.approx(100.0)
    assert not deal.all_time_low


def test_a_modest_tagged_discount_no_longer_clears_the_bar(filters):
    """Every one of the 520 notifications ever sent rested on the tag alone.

    An uncorroborated tag earns no bonus, so at the default threshold it now has
    to be a larger discount to be worth sending. The very same 30% backed by
    other shops still goes out — the bar moved for the evidence, not the price.
    """
    from pi.reference import Market

    history = make_history([(210.0, 300.0, 0)])
    assert deals.evaluate(1, 1, 210.0, 300.0, True, history, filters) is None

    corroborated = deals.evaluate(
        1, 1, 210.0, 300.0, True, history, filters,
        market=Market(median_usd=300.0, low_usd=290.0, shops=5),
    )
    assert corroborated is not None
    assert corroborated.reference_source == "market"


def test_recorded_history_outranks_the_shops_claim(filters):
    """With history of our own, the shop's own floor is the reference, not the tag.

    The lowest price actually charged in the window, not the median of the
    recorded points: points are change events, so a price that stood for six
    months is one row and a week of jitter is five, and a median reads the week
    as the norm.
    """
    history = make_history(
        [(210.0, 400.0, 40), (200.0, 400.0, 30), (190.0, 400.0, 20),
         (200.0, 400.0, 10), (120.0, 400.0, 0)]
    )
    deal = deals.evaluate(1, 1, 120.0, 400.0, True, history, filters)

    assert deal is not None
    assert deal.reference_source == "history"
    # 190 was really charged inside the window; 400 is what the shop says.
    assert deal.reference_usd == pytest.approx(190.0)
    assert deal.discount_pct == pytest.approx(36.8, abs=0.1)
    assert deal.all_time_low


def test_permanent_sale_is_detected_and_scored_down(filters):
    """A 'was 220, now 120' that has read that way for two months is the real price."""
    unchanged = [(120.0, 220.0, day) for day in (60, 45, 30, 15, 0)]
    deal = deals.evaluate(1, 1, 120.0, 220.0, True, make_history(unchanged), filters)

    # It becomes a history-referenced comparison against our own median of 120,
    # which is no discount at all, so nothing is sent.
    assert deal is None


def test_permanent_sale_flag_is_set_when_a_deal_still_qualifies():
    """The fake-sale penalty applies rather than being silently ignored."""
    lenient = Filters(min_discount_pct=5.0, min_saving_usd=5.0, min_score=0)
    history = make_history(
        [(200.0, 300.0, 40), (200.0, 300.0, 30), (200.0, 300.0, 20), (150.0, 300.0, 0)]
    )
    with_fake = deals.evaluate(1, 1, 150.0, 300.0, True, history, lenient)
    assert with_fake is not None
    assert with_fake.fake_sale is True

    # Same numbers, but the shop only just introduced the struck-through price.
    fresh = make_history(
        [(200.0, None, 40), (200.0, None, 30), (200.0, None, 20), (150.0, 300.0, 0)]
    )
    without_fake = deals.evaluate(1, 1, 150.0, 300.0, True, fresh, lenient)
    assert without_fake is not None
    assert without_fake.fake_sale is False
    assert without_fake.score > with_fake.score


def test_big_percentage_on_a_cheap_item_is_not_a_deal(filters):
    """70% off a $30 shirt saves $21 — below min_saving_usd, so no notification."""
    history = make_history([(9.0, 30.0, 0)])
    assert deals.evaluate(1, 1, 9.0, 30.0, True, history, filters) is None


def test_small_percentage_on_an_expensive_item_is_not_a_deal(filters):
    """$60 off a $600 jacket clears min_saving_usd but is only 10%."""
    history = make_history([(540.0, 600.0, 0)])
    assert deals.evaluate(1, 1, 540.0, 600.0, True, history, filters) is None


def test_out_of_stock_is_never_a_deal(filters):
    history = make_history([(120.0, 300.0, 0)])
    assert deals.evaluate(1, 1, 120.0, 300.0, False, history, filters) is None


def test_no_reference_price_means_no_deal(filters):
    """A cheap price with nothing to compare it against proves nothing."""
    history = make_history([(120.0, None, 0)])
    assert deals.evaluate(1, 1, 120.0, None, True, history, filters) is None


def test_price_outside_the_configured_range_is_skipped(filters):
    history = make_history([(5000.0, 12000.0, 0)])
    assert deals.evaluate(1, 1, 5000.0, 12000.0, True, history, filters) is None


def test_thin_history_falls_back_to_the_tag(filters):
    """Two points over two days is not enough history to be the reference."""
    history = make_history([(220.0, 300.0, 1), (120.0, 300.0, 0)])
    deal = deals.evaluate(1, 1, 120.0, 300.0, True, history, filters)
    assert deal is not None
    assert deal.reference_source == "tag"


def test_dropped_hours_ago_measures_the_current_price_only(filters):
    history = make_history([(220.0, 300.0, 30), (120.0, 300.0, 0.25)])
    deal = deals.evaluate(1, 1, 120.0, 300.0, True, history, filters)
    assert deal is not None
    assert deal.dropped_hours_ago == pytest.approx(6.0, abs=0.2)


def test_brand_and_size_filters():
    f = Filters(brands_allow=("nike", "jordan"), brands_deny=("kids",), sizes=("EU44", "US10"))
    assert f.wants_brand("Nike Sportswear")
    assert f.wants_brand("JORDAN")
    assert not f.wants_brand("Adidas")
    assert not f.wants_brand("Nike Kids")
    assert f.wants_size("EU44")
    assert not f.wants_size("EU39")
    assert not f.wants_size(None)

    assert Filters().wants_brand("anything")
    assert Filters().wants_size("EU39")
    assert Filters().wants_size(None)


class TestDeduplication:
    """The same shoe at the same price must never be announced twice."""

    def test_buckets_are_five_percent_wide(self):
        assert deals.price_bucket(100.0) == deals.price_bucket(103.0)
        assert deals.price_bucket(100.0) > deals.price_bucket(94.0)

    def test_second_alert_at_the_same_price_is_suppressed(self, conn, filters):
        history = make_history([(120.0, 300.0, 0)])
        deal = deals.evaluate(1, 1, 120.0, 300.0, True, history, filters)
        assert deal is not None

        conn.execute("INSERT INTO stores (id, domain) VALUES (1, 'x.com')")
        conn.execute(
            "INSERT INTO products (id, store_id, external_id, title, url)"
            " VALUES (1, 1, 'p', 't', 'u')"
        )
        conn.execute("INSERT INTO variants (id, product_id, external_id) VALUES (1, 1, 'v')")

        assert deals.already_alerted(conn, deal) is False
        assert deals.record_alert(conn, deal, ts()) is True
        assert deals.already_alerted(conn, deal) is True
        assert deals.record_alert(conn, deal, ts()) is False

    def test_a_further_drop_is_announced_again(self, conn, filters):
        conn.execute("INSERT INTO stores (id, domain) VALUES (1, 'x.com')")
        conn.execute(
            "INSERT INTO products (id, store_id, external_id, title, url)"
            " VALUES (1, 1, 'p', 't', 'u')"
        )
        conn.execute("INSERT INTO variants (id, product_id, external_id) VALUES (1, 1, 'v')")

        first = deals.evaluate(1, 1, 120.0, 300.0, True, make_history([(120.0, 300.0, 0)]), filters)
        deals.record_alert(conn, first, ts())

        nudge = replace(first, price_usd=118.0)      # under 5% lower: still quiet
        assert deals.already_alerted(conn, nudge) is True

        real_drop = replace(first, price_usd=99.0)   # a genuine further cut
        assert deals.already_alerted(conn, real_drop) is False


def test_first_sighting_makes_no_claim_about_when_the_price_dropped(filters):
    """One observation says when we looked, not when the shop moved the price."""
    deal = deals.evaluate(1, 1, 120.0, 300.0, True, make_history([(120.0, 300.0, 0)]), filters)
    assert deal is not None
    assert deal.dropped_hours_ago is None


class TestAnInflatedReferencePrice:
    """The shop that raises a price in order to discount it back.

    Every mechanism here answers the same question a different way: what was this
    actually selling for before the sign went up?
    """

    def test_a_price_raised_and_dropped_back_is_not_a_discount(self, filters):
        """200 for two months, 300 for a week, 200 again — and the shop calls the
        last step a 33% saving. Its own floor says otherwise."""
        history = make_history(
            [(200.0, None, 60), (300.0, 300.0, 7), (200.0, 300.0, 0)]
        )
        assert deals.evaluate(1, 1, 200.0, 300.0, True, history, filters) is None

    def test_the_same_shop_dropping_below_its_floor_is_a_discount(self, filters):
        """The mechanism must not simply refuse everything after a price rise."""
        history = make_history(
            [(200.0, None, 60), (300.0, 300.0, 7), (130.0, 300.0, 0)]
        )
        deal = deals.evaluate(1, 1, 130.0, 300.0, True, history, filters)
        assert deal is not None
        assert deal.reference_source == "history"
        assert deal.reference_usd == pytest.approx(200.0), "not the 300 it invented"
        assert deal.discount_pct == pytest.approx(35.0, abs=0.1)

    def test_a_price_above_the_market_is_not_a_discount(self, filters):
        """Whatever it is marked down from, it is not cheap if five other shops
        are asking less for the same article number."""
        history = make_history([(200.0, 400.0, 0)])
        market = Market(median_usd=170.0, low_usd=150.0, shops=5)
        assert deals.evaluate(1, 1, 200.0, 400.0, True, history, filters, market=market) is None

    def test_the_market_becomes_the_reference_when_there_is_no_history(self, filters):
        """2.75 million of 2.76 million variants have been seen exactly once, so
        this is the signal that works on the database as it actually is."""
        history = make_history([(120.0, None, 0)])
        market = Market(median_usd=200.0, low_usd=180.0, shops=6)
        deal = deals.evaluate(1, 1, 120.0, None, True, history, filters, market=market)
        assert deal is not None
        assert deal.reference_source == "market"
        assert deal.reference_usd == pytest.approx(200.0)
        assert deal.beats_market, "cheaper than every shop we can see"

    def test_a_price_four_times_below_every_other_shop_is_not_the_same_thing(self, filters):
        """allikestore.com's "Sean Wotherspoon" at $55.98 against the $1,100
        resale shops ask topped the shelf at −95%. Prices that far apart are the
        rule agreeing_prices keeps between other shops: different things."""
        history = make_history([(55.98, None, 0)])
        market = Market(median_usd=1099.88, low_usd=700.0, shops=5)
        deal = deals.evaluate(1, 1, 55.98, None, True, history, filters, market=market)
        assert deal is None

    def test_their_recommended_price_goes_with_them(self, filters):
        """Review 24.09: the same shops' struck-through $180 made the $55.98 find
        −69% against msrp, score 100, an all-time low worth interrupting for."""
        # A day of history: too short for the shop's own floor to decide.
        history = make_history([(60.0, None, 1), (55.98, None, 0)])
        market = Market(median_usd=1099.88, low_usd=700.0, shops=5, msrp_usd=180.0, msrp_shops=3)
        assert deals.evaluate(1, 1, 55.98, None, True, history, filters, market=market) is None

    def test_a_find_measured_otherwise_does_not_show_their_view(self, filters):
        history = make_history([(200.0, None, 10), (55.98, None, 0)])
        market = Market(median_usd=1099.88, low_usd=700.0, shops=5, msrp_usd=180.0, msrp_shops=3)
        deal = deals.evaluate(1, 1, 55.98, None, True, history, filters, market=market)
        assert deal is not None and deal.reference_source == "history"
        assert deal.market_shops == 0 and deal.market_median_usd is None
        assert deal.msrp_usd is None

    def test_nor_does_the_shops_own_tag_speak_then(self, filters):
        """Dropping the msrp also switched off the inflated-tag test, which
        compares the tag with it — so the shop's "was $300" became the reference."""
        history = make_history([(60.0, 300.0, 1), (55.98, 300.0, 0)])
        market = Market(median_usd=1099.88, low_usd=700.0, shops=5, msrp_usd=180.0, msrp_shops=3)
        assert deals.evaluate(1, 1, 55.98, 300.0, True, history, filters, market=market) is None

    def test_a_deep_but_believable_cut_against_the_market_still_counts(self, filters):
        history = make_history([(80.0, None, 0)])
        market = Market(median_usd=300.0, low_usd=260.0, shops=5)
        deal = deals.evaluate(1, 1, 80.0, None, True, history, filters, market=market)
        assert deal is not None and deal.reference_source == "market"
        assert deal.discount_pct == pytest.approx(73.3, abs=0.1)

    def test_a_tag_far_above_the_recommended_price_is_not_used(self, filters):
        """Six shops strike through 200; this one strikes through 300. The 300 is
        this shop's invention, so it is not a reference for anything."""
        history = make_history([(190.0, 300.0, 0)])
        market = Market(msrp_usd=200.0, msrp_shops=6)
        deal = deals.evaluate(1, 1, 190.0, 300.0, True, history, filters, market=market)
        assert deal is None or deal.reference_source != "tag"

    def test_the_recommended_price_is_used_when_the_tag_is_not(self, filters):
        history = make_history([(120.0, 300.0, 0)])
        market = Market(msrp_usd=200.0, msrp_shops=6)
        deal = deals.evaluate(1, 1, 120.0, 300.0, True, history, filters, market=market)
        assert deal is not None
        assert deal.reference_source == "msrp"
        assert deal.reference_usd == pytest.approx(200.0)
        assert deal.inflated_tag

    def test_a_shop_that_computes_its_discounts_proves_nothing(self, filters):
        """The last twelve notifications sent were one shop, all at -40%, from a
        catalogue whose every discount lands on a round 5% step."""
        history = make_history([(98.0, 163.0, 0)])
        by_rule = Trust(
            tag_share=0.64, round_share=1.0, blanket_pct=40.0, blanket_share=0.27,
            sample=10552, tagged=6763,
        )
        assert deals.evaluate(1, 1, 98.0, 163.0, True, history, filters, trust=by_rule) is None

    def test_the_same_numbers_from_a_shop_that_remembers_former_prices(self, filters):
        history = make_history([(98.0, 163.0, 0)])
        honest = Trust(
            tag_share=0.38, round_share=0.21, blanket_pct=29.0, blanket_share=0.08,
            sample=4346, tagged=1650,
        )
        deal = deals.evaluate(1, 1, 98.0, 163.0, True, history, filters, trust=honest)
        assert deal is not None
        assert deal.reference_source == "tag"


class TestTheMarketIsBelievedAtOneStrength:
    """The market either counts or it does not, and the same threshold decides.

    `market_min_shops` exists because two shops agreeing is a coincidence and
    five is a price. Every use of the market has to respect it, including the
    +15 for undercutting the market — the largest single bonus in the score.
    Trusting a figure for the bonus after refusing it for the reference is
    reading the same weak evidence twice, and the second read is the one that
    sends the notification.
    """

    def test_a_market_too_small_to_price_is_too_small_to_credit(self, filters):
        """One other shop cannot make a tag-priced deal into a market-beating one.

        Its 30% comes entirely from the struck-through price, which is what the
        market was supposed to check rather than corroborate.
        """
        history = make_history([(140.0, 200.0, 0)])
        thin = Market(median_usd=145.0, low_usd=145.0, shops=1)
        assert deals.evaluate(1, 1, 140.0, 200.0, True, history, filters, market=thin) is None

    def test_more_shops_never_turn_a_silent_run_loud(self, filters):
        """Evidence may only cost a deal its notification, never buy one.

        With three shops the median is the reference and the price is 3.4% under
        it — not a discount. Any smaller market has to reach the same verdict or
        a quieter one, or the shops nobody found are worth more than the shops
        somebody did.
        """
        history = make_history([(140.0, 200.0, 0)])
        for shops in (0, 1, 2, 3, 5):
            market = (
                Market(median_usd=145.0, low_usd=145.0, shops=shops) if shops else Market()
            )
            deal = deals.evaluate(1, 1, 140.0, 200.0, True, history, filters, market=market)
            assert deal is None, f"{shops} other shop(s) should not produce an alert"

    def test_a_market_big_enough_still_earns_the_credit(self, filters):
        """The guard must not silence the signal it is guarding."""
        history = make_history([(120.0, None, 0)])
        market = Market(median_usd=200.0, low_usd=180.0, shops=6)
        deal = deals.evaluate(1, 1, 120.0, None, True, history, filters, market=market)
        assert deal is not None
        assert deal.beats_market, "under the lowest of six shops"


class TestAClaimNobodyCanMake:
    """A reference that says the price fell 95% is not describing a sale."""

    def test_a_tag_claiming_a_ninety_seven_percent_cut_is_refused(self):
        """Live: a $40 vintage fleece marked down from $1,420, top of the shelf.

        The shop does not price by rule and has no recommended price to be
        inflated against, so every existing test on the tag passes it.
        """
        deal = _evaluate(price=40.59, compare_at=1420.76)
        assert deal is None

    def test_an_ordinary_deep_cut_still_counts(self):
        deal = _evaluate(price=40.0, compare_at=200.0)
        assert deal is not None
        assert deal.discount_pct == pytest.approx(80.0)


def _evaluate(price, compare_at):
    history = make_history([(price, compare_at, 0)])
    return deals.evaluate(
        variant_id=1, product_id=1, price_usd=price, compare_at_usd=compare_at,
        in_stock=True, history=history,
        filters=Filters(min_discount_pct=30.0, min_saving_usd=10.0),
    )


class TestAFindOnlyAStarLetThrough:
    """A followed product skips the bars so its follower hears small moves; the
    deal says whether it would have cleared them anyway (review 24.09)."""

    def test_a_small_move_on_a_followed_product_is_not_on_merit(self, filters):
        history = make_history([(100.0, None, 10), (97.0, None, 0)])
        assert deals.evaluate(1, 1, 97.0, None, True, history, filters) is None
        deal = deals.evaluate(1, 1, 97.0, None, True, history, filters, watched=True)

        assert deal is not None and deal.watched
        assert not deal.on_merit

    def test_a_real_discount_is_on_merit_followed_or_not(self, filters):
        history = make_history([(200.0, None, 10), (100.0, None, 0)])
        plain = deals.evaluate(1, 1, 100.0, None, True, history, filters)
        followed = deals.evaluate(1, 1, 100.0, None, True, history, filters, watched=True)

        assert plain is not None and plain.on_merit
        assert followed is not None and followed.on_merit
