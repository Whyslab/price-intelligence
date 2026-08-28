"""The second scale: how much *this reader* should care."""
from __future__ import annotations

import sqlite3

import pytest

from pi import personal
from pi.config import Filters
from pi.deals import Deal


def row(size="EU44", family="Nike", kind="shoes", gender=None) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE r (size_norm TEXT, brand_family TEXT, brand_norm TEXT,"
        " kind TEXT, gender TEXT)"
    )
    conn.execute("INSERT INTO r VALUES (?, ?, ?, ?, ?)", (size, family, family, kind, gender))
    return conn.execute("SELECT * FROM r").fetchone()


def deal(score=70) -> Deal:
    return Deal(
        variant_id=1, product_id=1, price_usd=100.0, reference_usd=200.0,
        reference_source="history", discount_pct=50.0, saving_usd=100.0,
        score=score, all_time_low=False, fake_sale=False,
        dropped_hours_ago=None, history_points=2,
    )


@pytest.fixture
def me() -> personal.Reader:
    return personal.Reader(
        sizes=frozenset({"EU44", "XL"}),
        brands=frozenset({"nike"}),
        kinds=frozenset({"shoes"}),
    )


class TestTheBarMoves:
    """A size you do not take raises the bar; it does not disqualify."""

    def test_your_size_keeps_the_normal_bar(self, me):
        assert personal.bar_for(row(size="EU44", family="Puma"), me, base=55) == 55

    def test_somebody_else_s_size_has_to_be_properly_cheap(self, me):
        assert personal.bar_for(row(size="EU38", family="Puma"), me, base=55) == 75

    def test_a_brand_you_named_lowers_it(self, me):
        assert personal.bar_for(row(size="EU44", family="Nike"), me, base=55) == 45

    def test_with_no_sizes_named_nothing_moves(self):
        nobody = personal.Reader()
        assert personal.bar_for(row(size="EU38"), nobody, base=55) == 55

    def test_a_named_brand_in_the_wrong_size_lands_in_between(self, me):
        """+20 for the size, −10 for the brand: still above the normal bar."""
        assert personal.bar_for(row(size="EU38", family="Nike"), me, base=55) == 65


class TestTheQueue:
    def test_your_size_comes_first(self, me):
        mine = personal.priority(deal(), row(size="EU44", family="Puma"), me)
        theirs = personal.priority(deal(), row(size="EU38", family="Puma"), me)
        assert mine > theirs

    def test_a_smaller_discount_in_your_size_outranks_a_bigger_one_that_is_not(self, me):
        """The case the single-number score got wrong, and the reason for two."""
        mine = personal.priority(deal(score=60), row(size="EU44", family="Puma"), me)
        theirs = personal.priority(deal(score=80), row(size="EU38", family="Puma"), me)
        assert mine > theirs

    def test_a_named_brand_lifts_it_further(self, me):
        named = personal.priority(deal(), row(size="EU44", family="Nike"), me)
        other = personal.priority(deal(), row(size="EU44", family="Puma"), me)
        assert named > other


class TestTheRanker:
    def test_a_find_below_the_bar_is_dropped(self, me):
        rank = personal.ranker(me, min_score=55)
        assert rank(deal(score=60), row(size="EU38")) is None, "someone else's size, weak deal"
        assert rank(deal(score=80), row(size="EU38")) is not None, "someone else's size, strong"

    def test_asking_for_women_is_the_one_filter_that_really_filters(self):
        """Being sent men's things is not a near miss, it is the wrong answer."""
        reader = personal.Reader(genders=frozenset({"women"}))
        rank = personal.ranker(reader, min_score=55)
        assert rank(deal(), row(gender="men")) is None
        assert rank(deal(), row(gender=None)) is None
        assert rank(deal(), row(gender="women")) is not None

    def test_asking_for_men_lets_the_unstated_through(self):
        reader = personal.Reader(genders=frozenset({"men"}))
        rank = personal.ranker(reader, min_score=55)
        assert rank(deal(), row(gender=None)) is not None


class TestWhoTheReaderIs:
    def test_without_a_profile_it_falls_back_to_the_file(self):
        """Upgrading must not silently widen every notification."""
        filters = Filters(sizes=("EU44",), brands_allow=("nike",))
        reader = personal.Reader.from_filters(filters)
        assert reader.sizes == frozenset({"EU44"})
        assert reader.brands == frozenset({"nike"})

    def test_the_profile_wins_once_the_wizard_has_been_run(self, conn):
        from pi import db as dbm

        dbm.upsert_bot_user(conn, 7, "42", "u", sizes="EU45", onboarded=1)
        reader = personal.reader_for(conn, "42", Filters(sizes=("EU44",)))
        assert reader.sizes == frozenset({"EU45"})

    def test_an_unfinished_wizard_does_not_count(self, conn):
        from pi import db as dbm

        dbm.upsert_bot_user(conn, 7, "42", "u", sizes="EU45", onboarded=0)
        reader = personal.reader_for(conn, "42", Filters(sizes=("EU44",)))
        assert reader.sizes == frozenset({"EU44"})
