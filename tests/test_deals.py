"""What counts as a deal, and what must not."""
from __future__ import annotations

from dataclasses import replace

import pytest

from pi import deals
from pi.config import Filters

from .conftest import make_history, ts


def test_tagged_discount_alone_is_enough_on_a_fresh_database(filters):
    """First ever sighting: only the shop's struck-through price exists."""
    history = make_history([(120.0, 220.0, 0)])
    deal = deals.evaluate(1, 1, 120.0, 220.0, True, history, filters)

    assert deal is not None
    assert deal.reference_source == "tag"
    assert deal.discount_pct == pytest.approx(45.5, abs=0.1)
    assert deal.saving_usd == pytest.approx(100.0)
    assert not deal.all_time_low


def test_recorded_history_outranks_the_shops_claim(filters):
    """With enough history of our own, the median is the reference, not the tag."""
    history = make_history(
        [(210.0, 400.0, 40), (200.0, 400.0, 30), (190.0, 400.0, 20),
         (200.0, 400.0, 10), (120.0, 400.0, 0)]
    )
    deal = deals.evaluate(1, 1, 120.0, 400.0, True, history, filters)

    assert deal is not None
    assert deal.reference_source == "history"
    # 200 is the median of the four earlier prices, not the 400 the shop claims
    assert deal.reference_usd == pytest.approx(200.0)
    assert deal.discount_pct == pytest.approx(40.0, abs=0.1)
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
