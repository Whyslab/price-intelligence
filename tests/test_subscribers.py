"""More than one reader.

Everything here was correct while the bot wrote to one chat and quietly wrong
the moment it wrote to two. The tests are mostly about the second reader,
because the first one never noticed.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from pi import db as dbm
from pi import deals as dealm
from pi import personal, pipeline
from pi.config import Config, Filters

from .conftest import ts

OWNER_CHAT = "42"


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "pi.db",
        sites_file=tmp_path / "sites.txt",
        bot_token="123:AA",
        chat_id=OWNER_CHAT,
        concurrency=4,
        shopify_rate=10_000.0,
        shopify_host_rate=10_000.0,
        max_shopify_stores=0,
        log_level="WARNING",
        # Selling is off by default (Config.subscription); these tests are
        # about what happens when it is on, so they turn it on.
        subscription=True,
        filters=Filters(min_discount_pct=30.0, min_saving_usd=40.0, min_score=50),
    )


def a_deal(product_id: int = 1, price: float = 100.0) -> dealm.Deal:
    return dealm.Deal(
        variant_id=product_id, product_id=product_id, price_usd=price,
        reference_usd=200.0, reference_source="market", discount_pct=50.0,
        saving_usd=100.0, score=80, all_time_low=False, fake_sale=False,
        dropped_hours_ago=1.0, history_points=2,
    )


def a_product(conn) -> int:
    store = dbm.upsert_store(conn, "shop.example", platform="shopify", currency="USD")
    product = dbm.upsert_product(conn, store, "p1", "Shoe", "https://shop.example/p1")
    dbm.upsert_variant(conn, product, "v1")
    return product


class TestWhoTheRunWritesTo:
    def test_the_owner_alone_is_a_reader_without_ever_having_used_the_bot(self, conn):
        """A fresh install has a chat id in .env and no subscriber list at all."""
        readers = personal.subscribers(conn, OWNER_CHAT, Filters(sizes=("EU44",)))

        assert len(readers) == 1
        assert readers[0].chat_id == OWNER_CHAT
        assert readers[0].user_id == 0
        assert readers[0].reader.sizes == frozenset({"EU44"}), "filters.toml still counts"

    def test_a_second_paying_person_is_a_second_reader(self, conn):
        dbm.upsert_bot_user(conn, 7, OWNER_CHAT, "owner", sizes="EU44")
        dbm.upsert_bot_user(conn, 9, "99", "someone", sizes="EU40")
        dbm.grant(conn, 9, days=30)

        readers = personal.subscribers(conn, OWNER_CHAT, Filters())

        assert [r.chat_id for r in readers] == [OWNER_CHAT, "99"], "the owner goes first"
        assert [r.user_id for r in readers] == [7, 9]
        assert readers[1].reader.sizes == frozenset({"EU40"}), "their sizes, not the owner's"

    def test_the_owner_is_not_counted_twice_once_they_have_a_profile(self, conn):
        dbm.upsert_bot_user(conn, 7, OWNER_CHAT, "owner", sizes="EU44")

        readers = personal.subscribers(conn, OWNER_CHAT, Filters(sizes=("EU99",)))

        assert len(readers) == 1
        assert readers[0].user_id == 7, "the profile wins over the chat id"
        assert readers[0].reader.sizes == frozenset({"EU44"})

    def test_skipping_the_wizard_means_everything_not_nothing(self, conn):
        """Pressing /start and answering none of the questions is not unsubscribing."""
        dbm.upsert_bot_user(conn, 9, "99", "quiet")
        dbm.grant(conn, 9, days=30)

        readers = personal.subscribers(conn, OWNER_CHAT, Filters())

        assert "99" in [r.chat_id for r in readers]
        assert not readers[-1].reader.has_opinions

    def test_a_reader_who_blocked_the_bot_is_left_out(self, conn):
        dbm.upsert_bot_user(conn, 9, "99", "gone")
        dbm.grant(conn, 9, days=30)
        personal.deactivate(conn, "99")

        assert "99" not in [r.chat_id for r in personal.subscribers(conn, OWNER_CHAT, Filters())]

    def test_talking_to_the_bot_again_brings_them_back(self, conn):
        """The only evidence a chat reopened is the person turning up in it."""
        dbm.upsert_bot_user(conn, 9, "99", "gone")
        dbm.grant(conn, 9, days=30)
        personal.deactivate(conn, "99")

        dbm.upsert_bot_user(conn, 9, "99", "back")

        assert "99" in [r.chat_id for r in personal.subscribers(conn, OWNER_CHAT, Filters())]


class TestNewsIsNewsToEachPerson:
    """The bug that made every extra subscriber cost the others their notifications."""

    def test_two_readers_both_hear_about_the_same_find(self, conn):
        a_product(conn)
        deal = a_deal()

        assert dealm.record_alert(conn, deal, dbm.utcnow(), user_id=7)
        assert dealm.record_alert(conn, deal, dbm.utcnow(), user_id=9), (
            "the second reader was robbed by the first"
        )

    def test_the_same_reader_is_not_told_twice(self, conn):
        a_product(conn)
        deal = a_deal()

        assert dealm.record_alert(conn, deal, dbm.utcnow(), user_id=7)
        assert not dealm.record_alert(conn, deal, dbm.utcnow(), user_id=7)

    def test_being_told_does_not_silence_anyone_else(self, conn):
        a_product(conn)
        deal = a_deal()
        dealm.record_alert(conn, deal, dbm.utcnow(), user_id=7)

        assert dealm.already_alerted(conn, deal, user_id=7)
        assert not dealm.already_alerted(conn, deal, user_id=9)

    def test_a_seeded_discount_is_old_news_to_everybody(self, conn):
        """What was already on sale before the bot existed is not news to a
        subscriber who joins tomorrow either."""
        a_product(conn)
        deal = a_deal()
        dealm.record_alert(conn, deal, dbm.utcnow(), sent=False)  # user 0: everyone

        assert dealm.already_alerted(conn, deal, user_id=7)
        assert dealm.already_alerted(conn, deal, user_id=9)

    def test_a_further_drop_reaches_a_reader_who_already_heard_the_first(self, conn):
        a_product(conn)
        dealm.record_alert(conn, a_deal(price=100.0), dbm.utcnow(), user_id=7)

        assert not dealm.already_alerted(conn, a_deal(price=80.0), user_id=7)
        assert dealm.already_alerted(conn, a_deal(price=99.0), user_id=7)


class TestArrangingOneReadersList:
    """The cheap half: same scored deals, different list per person."""

    @staticmethod
    def _scored(conn) -> list:
        product = a_product(conn)
        row = conn.execute(
            """
            SELECT v.id AS variant_id, v.product_id, s.domain, p.audience
              FROM variants v
              JOIN products p ON p.id = v.product_id
              JOIN stores   s ON s.id = p.store_id
             WHERE v.product_id = ?
            """,
            (product,),
        ).fetchone()
        return [(a_deal(product_id=product), row)]

    def test_a_find_already_sent_to_one_reader_still_reaches_another(self, config, conn):
        scored = self._scored(conn)
        deal = scored[0][0]
        dealm.record_alert(conn, deal, dbm.utcnow(), user_id=7)

        for_seven = pipeline.arrange_for(
            conn, scored, config, market=None, user_id=7, fold_duplicates=False
        )
        for_nine = pipeline.arrange_for(
            conn, scored, config, market=None, user_id=9, fold_duplicates=False
        )

        assert for_seven == []
        assert len(for_nine) == 1

    def _a_childs_offer(self, conn) -> list:
        self._scored(conn)
        conn.execute("UPDATE products SET audience = 'kids'")
        return self._scored(conn)   # re-read, now carrying the audience

    def test_a_childs_shoe_is_not_worth_interrupting_anyone_with(self, config, conn):
        scored = self._a_childs_offer(conn)

        assert pipeline.arrange_for(
            conn, scored, config, market=None, fold_duplicates=False
        ) == []

    def test_the_shelf_is_written_with_it_anyway(self, config, conn):
        """Scoring feeds the browsable shelf too, and that one can be asked."""
        scored = self._a_childs_offer(conn)

        assert len(pipeline.arrange_for(
            conn, scored, config, market=None, fold_duplicates=False, kids=True
        )) == 1

    def test_the_shelf_keeps_every_discounted_size_and_a_message_one(self, config, conn):
        """The shelf is filtered by size against the variant on offer.

        Holding only a product's best-scoring size made a shoe discounted in
        EU44 invisible to somebody filtering for EU44 whenever EU42 had scored
        a point higher. A message is different: one hoodie in six sizes is one
        thing to hear about.
        """
        (deal, row), = self._scored(conn)
        other_size = replace(deal, variant_id=deal.variant_id + 1000, score=deal.score - 1)
        scored = [(deal, row), (other_size, row)]

        shelf = pipeline.arrange_for(
            conn, scored, config, market=None, cap_per_store=False,
            fold_duplicates=False, skip_alerted=False, kids=True, per_product=False,
        )
        message = pipeline.arrange_for(
            conn, scored, config, market=None, fold_duplicates=False
        )

        assert {d.variant_id for d, _ in shelf} == {deal.variant_id, other_size.variant_id}
        assert [d.variant_id for d, _ in message] == [deal.variant_id]

    def test_an_article_asked_for_by_name_still_arrives(self, config, conn):
        """Naming an article is a clearer statement than a reading of a title."""
        scored = self._a_childs_offer(conn)
        watched = {scored[0][0].product_id}

        assert len(pipeline.arrange_for(
            conn, scored, config, market=None, fold_duplicates=False, watched=watched
        )) == 1


class TestMigratingToPerReaderAlerts:
    @staticmethod
    def _a_version_9_database(path, product_id: int = 1):
        """A database as version 9 left it, with one already-sent alert in it."""
        conn = dbm.connect(path)
        conn.executescript(
            """
            DROP TABLE alerts;
            CREATE TABLE alerts (
                id           INTEGER PRIMARY KEY,
                product_id   INTEGER NOT NULL,
                variant_id   INTEGER NOT NULL,
                ts           TEXT    NOT NULL,
                price_usd    REAL    NOT NULL,
                price_bucket INTEGER NOT NULL,
                discount_pct REAL    NOT NULL,
                score        INTEGER NOT NULL,
                sent         INTEGER NOT NULL DEFAULT 1,
                UNIQUE (product_id, price_bucket)
            );
            """
        )
        conn.execute(
            "INSERT INTO alerts (product_id, variant_id, ts, price_usd, price_bucket,"
            " discount_pct, score, sent)"
            " VALUES (?, ?, '2026-08-01T00:00:00+00:00', 100.0, 5, 50.0, 80, 1)",
            (product_id, product_id),
        )
        conn.execute("PRAGMA user_version = 9")
        conn.close()

    def test_an_old_database_is_rebuilt_and_its_history_kept(self, tmp_path):
        """Rows written before there was a second reader mean "told everybody"."""
        path = tmp_path / "old.db"
        conn = dbm.connect(path)
        product = a_product(conn)
        conn.close()
        self._a_version_9_database(path, product_id=product)
        conn = dbm.connect(path)
        try:
            row = conn.execute("SELECT * FROM alerts").fetchone()
            assert row["user_id"] == 0, "already announced, to whoever there was"
            assert row["price_usd"] == 100.0, "the history survived the rebuild"
            unique = {
                tuple(c["name"] for c in conn.execute(f"PRAGMA index_info({i['name']})"))
                for i in conn.execute("PRAGMA index_list(alerts)")
                if i["unique"]
            }
            assert ("user_id", "product_id", "price_bucket") in unique
            assert ("product_id", "price_bucket") not in unique
        finally:
            conn.close()

    def test_an_orphaned_row_does_not_stop_the_collector_starting(self, tmp_path):
        """The rebuild re-checks every foreign key it copies.

        One alert whose product had been deleted would abort the migration —
        and the migration runs inside `connect`, so it would not fail a query,
        it would stop the service from starting at all.
        """
        path = tmp_path / "orphan.db"
        self._a_version_9_database(path, product_id=999)  # no such product

        conn = dbm.connect(path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, (
                "and foreign keys are back on afterwards"
            )
        finally:
            conn.close()

    def test_the_rebuild_does_not_run_twice(self, tmp_path):
        """migrate() is called on every connect; it must be a no-op after the first."""
        path = tmp_path / "fresh.db"
        for _ in range(3):
            conn = dbm.connect(path)
            conn.close()
        conn = dbm.connect(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == dbm.SCHEMA_VERSION
        finally:
            conn.close()


class TestFollowingAProduct:
    """A star on the shelf is the reader saying it, and it outranks every guess.

    The expensive half is shared, so scoring is told the union of everybody's
    lists; who actually hears is decided in the cheap half. These are the tests
    of that seam, because getting it wrong either tells everyone about one
    person's shoe or tells nobody about their own.
    """

    @staticmethod
    def _scored(conn, score: int = 20, price: float = 97.0, gender: str = "men") -> list:
        """One deal nobody would be interrupted with: 20 points, well under the bar.

        `gender` defaults to what this shop stocks. Only the starred-thing test
        wants women's, and it says so — a women's product is not carried at all
        (pi.db.offers_for, pipeline.arrange_for), so leaving it as the fixture
        default made every other test here depend on a rule none of them is about.
        """
        product = a_product(conn)
        conn.execute(
            "UPDATE products SET gender = ?, kind = 'shoes', brand_family = 'Salomon'"
            " WHERE id = ?", (gender, product),
        )
        row = conn.execute(
            """
            SELECT v.id AS variant_id, v.product_id, v.size_norm, s.domain, s.country,
                   p.audience, p.gender, p.kind, p.brand_family, p.brand_norm
              FROM variants v
              JOIN products p ON p.id = v.product_id
              JOIN stores   s ON s.id = p.store_id
             WHERE v.product_id = ?
            """,
            (product,),
        ).fetchone()
        deal = a_deal(product_id=product, price=price)
        deal.score = score
        return [(deal, row)]

    def _ranker(self, config, following=frozenset()):
        return personal.ranker(
            personal.Reader(genders=frozenset({"men"})),
            config.filters.min_score,
            following=following,
        )

    def test_a_starred_thing_arrives_below_every_bar(self, config, conn):
        """20 points against a bar of 50, and a gender this shop does not stock."""
        scored = self._scored(conn, score=20, gender="women")
        product = scored[0][0].product_id

        arrived = pipeline.arrange_for(
            conn, scored, config, market=None, user_id=7,
            rank=self._ranker(config, {product}), fold_duplicates=False,
            watched={product},
        )

        assert len(arrived) == 1

    def test_the_same_thing_does_not_reach_somebody_who_did_not_star_it(self, config, conn):
        scored = self._scored(conn, score=20, gender="women")

        assert pipeline.arrange_for(
            conn, scored, config, market=None, user_id=9,
            rank=self._ranker(config), fold_duplicates=False,
        ) == []

    def test_the_notification_says_it_is_theirs_and_not_the_other_readers(
        self, config, conn
    ):
        """`watched` is set at scoring for everybody at once, so the flag on the
        deal has to be rewritten per reader or it tells the wrong person."""
        scored = self._scored(conn, score=90)
        product = scored[0][0].product_id

        mine = pipeline.arrange_for(
            conn, scored, config, market=None, user_id=7, fold_duplicates=False,
            watched={product},
        )
        theirs = pipeline.arrange_for(
            conn, scored, config, market=None, user_id=9, fold_duplicates=False,
        )

        assert mine[0][0].watched is True
        assert theirs[0][0].watched is False

    def test_a_second_word_about_a_starred_thing_needs_only_two_percent(
        self, config, conn
    ):
        scored = self._scored(conn, score=90, price=97.0)
        product = scored[0][0].product_id
        dealm.record_alert(conn, a_deal(product_id=product, price=100.0), dbm.utcnow(), user_id=7)

        starred = pipeline.arrange_for(
            conn, scored, config, market=None, user_id=7, fold_duplicates=False,
            watched={product},
        )
        ordinary = pipeline.arrange_for(
            conn, scored, config, market=None, user_id=7, fold_duplicates=False,
        )

        assert len(starred) == 1, "3% below the last word, and they asked for this one"
        assert ordinary == [], "the same 3% is not worth interrupting anyone else with"

    def test_two_percent_is_a_bar_and_not_an_absence_of_one(self, config, conn):
        scored = self._scored(conn, score=90, price=99.0)
        product = scored[0][0].product_id
        dealm.record_alert(conn, a_deal(product_id=product, price=100.0), dbm.utcnow(), user_id=7)

        assert pipeline.arrange_for(
            conn, scored, config, market=None, user_id=7, fold_duplicates=False,
            watched={product},
        ) == [], "1% is not news even about a thing somebody is waiting for"

    def test_a_find_only_a_star_let_through_is_nobody_elses(self, config, conn):
        """Review 24.09: one reader's star put a −3% price on the shelf everybody
        reads. Scoring skips the bars for a followed product; the shelf and the
        other readers' lists must not inherit that."""
        scored = self._scored(conn, score=20)
        scored[0][0].on_merit = False
        product = scored[0][0].product_id

        shelf = pipeline.arrange_for(
            conn, scored, config, market=None, cap_per_store=False,
            fold_duplicates=False, skip_alerted=False, kids=True, per_product=False,
        )
        mine = pipeline.arrange_for(
            conn, scored, config, market=None, user_id=7, fold_duplicates=False,
            watched={product},
        )
        theirs = pipeline.arrange_for(
            conn, scored, config, market=None, user_id=9, fold_duplicates=False,
            watched={12345},
        )

        assert shelf == [], "not on the shared shelf"
        assert len(mine) == 1, "still the follower's"
        assert theirs == []

    def test_a_real_find_that_somebody_follows_is_still_everybodys(self, config, conn):
        scored = self._scored(conn, score=90)
        assert scored[0][0].on_merit

        shelf = pipeline.arrange_for(
            conn, scored, config, market=None, cap_per_store=False,
            fold_duplicates=False, skip_alerted=False, kids=True, per_product=False,
        )
        assert len(shelf) == 1

    def test_somebody_who_blocked_the_bot_follows_nothing(self, config, conn):
        product = a_product(conn)
        dbm.upsert_bot_user(conn, 7, "7", "reader")
        dbm.upsert_bot_user(conn, 9, "9", "left")
        dbm.add_favorite(conn, 7, product)
        dbm.add_favorite(conn, 9, product)
        personal.deactivate(conn, "9")

        assert dbm.following(conn) == {7: {product}}

    def test_nobody_following_anything_changes_nothing(self, config, conn):
        scored = self._scored(conn, score=90)

        assert dbm.following(conn) == {}
        assert len(pipeline.arrange_for(
            conn, scored, config, market=None, user_id=7, fold_duplicates=False,
        )) == 1


class TestTheFeedIsWhatIsSold:
    """A reader who has not paid hears from the digest, not from the run.

    Every call here passes `subscription=True`: selling is off by default
    (Config.subscription), and with it off there is no paid/free line to test.
    """

    def test_a_free_reader_with_a_full_profile_is_not_written_to(self, conn):
        dbm.upsert_bot_user(conn, 9, "99", "browsing", sizes="EU40", brands="Nike")

        readers = personal.subscribers(conn, OWNER_CHAT, Filters(), subscription=True)

        assert "99" not in [r.chat_id for r in readers], (
            "the feed is the thing being sold; a filled-in profile does not buy it"
        )

    def test_paying_puts_them_back(self, conn):
        dbm.upsert_bot_user(conn, 9, "99", "paying", sizes="EU40")
        dbm.grant(conn, 9, days=30)

        assert "99" in [r.chat_id for r in personal.subscribers(conn, OWNER_CHAT, Filters(), subscription=True)]

    def test_grace_keeps_the_feed_running(self, conn):
        dbm.upsert_bot_user(conn, 9, "99", "lapsing", sizes="EU40")
        dbm.grant(conn, 9, days=30)
        conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = 9", (ts(1),))

        assert "99" in [r.chat_id for r in personal.subscribers(conn, OWNER_CHAT, Filters(), subscription=True)], (
            "a failed renewal should cost a reminder, not the product"
        )

    def test_the_owner_never_has_to_pay(self, conn):
        dbm.upsert_bot_user(conn, 7, OWNER_CHAT, "owner", sizes="EU44")

        readers = personal.subscribers(conn, OWNER_CHAT, Filters(), subscription=True)

        assert [r.chat_id for r in readers] == [OWNER_CHAT]
