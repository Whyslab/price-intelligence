"""The bot: what one person is shown out of what is on offer."""
from __future__ import annotations

import pytest

from pi import bot
from pi import db as dbm

from .conftest import ts


def make_offer(
    conn, *, domain="shop.example", title="Air Force 1", brand_norm="Nike",
    brand_family="Nike", gender=None, kind="shoes", size="US10", score=80,
    discount=45.0, price=100.0, in_stock=True,
):
    store_id = dbm.upsert_store(conn, domain, platform="shopify", status="ok", name="Shop")
    product_id = dbm.upsert_product(
        conn, store_id, f"p-{title}-{size}-{domain}", title,
        f"https://{domain}/p", brand=brand_norm,
    )
    conn.execute(
        "UPDATE products SET brand_norm = ?, brand_family = ?, gender = ?, kind = ? WHERE id = ?",
        (brand_norm, brand_family, gender, kind, product_id),
    )
    variant_id = dbm.upsert_variant(
        conn, product_id, f"v-{size}", sku=None, size=size, size_norm=size, color=None
    )
    dbm.record_price(
        conn, variant_id, price, price * 2, in_stock, "USD", price, 1.0, ts=ts()
    )
    conn.execute(
        """
        INSERT INTO offers (variant_id, product_id, found_at, checked_at, price_usd,
                            reference_usd, reference_source, discount_pct, saving_usd,
                            score, all_time_low)
        VALUES (?, ?, ?, ?, ?, ?, 'history', ?, ?, ?, 0)
        """,
        (variant_id, product_id, ts(), ts(), price, price * 2, discount, price, score),
    )
    return variant_id


@pytest.fixture
def user(conn):
    return dbm.upsert_bot_user(conn, 42, "42", "tester")


class TestProfile:
    """Absent and empty are different answers, and the difference matters."""

    def test_a_skipped_question_means_everything(self, conn, user):
        assert bot.profile_of(user)["sizes"] is None

    def test_an_answered_question_is_a_list(self, conn, user):
        saved = dbm.upsert_bot_user(conn, 42, "42", sizes="EU44,XL")
        assert bot.profile_of(saved)["sizes"] == ["EU44", "XL"]

    def test_typed_sizes_are_normalised_the_way_the_catalogue_is(self):
        """"EU 44" must become "EU44" or the filter silently matches nothing."""
        assert bot.Bot._clean("sizes", "EU 44, us 10.5, Large") == "EU44,L,US10.5"

    def test_typed_brands_are_folded_to_compare(self):
        assert bot.Bot._clean("brands", "Nike, Stone Island") == "nike,stone island"

    def test_nothing_typed_is_no_opinion(self):
        assert bot.Bot._clean("sizes", "   ") is None


class TestShelf:
    def test_with_no_profile_everything_is_shown(self, conn):
        make_offer(conn, size="US10")
        make_offer(conn, size="EU38", title="Other")
        _, total = dbm.offers_for(conn)
        assert total == 2

    def test_a_size_filter_matches_the_variant_on_offer(self, conn):
        """A shoe discounted in EU38 is not discounted in EU44."""
        make_offer(conn, size="EU44")
        make_offer(conn, size="EU38", title="Other")
        rows, total = dbm.offers_for(conn, sizes=["EU44"])
        assert total == 1
        assert rows[0]["size_norm"] == "EU44"

    def test_asking_for_women_excludes_the_unknown(self, conn):
        make_offer(conn, gender="women", title="Wmns")
        make_offer(conn, gender=None, title="Unsaid")
        _, total = dbm.offers_for(conn, genders=["women"])
        assert total == 1

    def test_asking_for_men_lets_the_unknown_through(self, conn):
        """87% of the catalogue never says, so excluding it would hide the shop."""
        make_offer(conn, gender="men", title="Mens")
        make_offer(conn, gender=None, title="Unsaid")
        make_offer(conn, gender="women", title="Wmns")
        _, total = dbm.offers_for(conn, genders=["men"])
        assert total == 2

    def test_a_brand_filter_matches_the_family(self, conn):
        """Asking for Nike finds a Jordan, which is the point of the family."""
        make_offer(conn, brand_norm="Jordan", brand_family="Nike", title="AJ1")
        make_offer(conn, brand_norm="Puma", brand_family="Puma", title="Suede")
        rows, total = dbm.offers_for(conn, brands=["nike"])
        assert total == 1
        assert rows[0]["brand_norm"] == "Jordan"

    def test_the_best_offer_comes_first(self, conn):
        make_offer(conn, score=60, title="Weak", size="US9")
        make_offer(conn, score=95, title="Strong", size="US11")
        rows, _ = dbm.offers_for(conn)
        assert rows[0]["title"] == "Strong"

    def test_paging_walks_the_whole_shelf(self, conn):
        for n in range(25):
            make_offer(conn, title=f"Shoe {n}", size=f"US{n}", score=n)
        first, total = dbm.offers_for(conn, limit=10, offset=0)
        third, _ = dbm.offers_for(conn, limit=10, offset=20)
        assert total == 25
        assert len(first) == 10
        assert len(third) == 5
        assert {row["variant_id"] for row in first} & {row["variant_id"] for row in third} == set()


class TestSizesOnTheCard:
    def test_it_says_which_sizes_are_left(self, conn):
        variant_id = make_offer(conn, size="EU44", in_stock=True)
        product_id = conn.execute(
            "SELECT product_id FROM offers WHERE variant_id = ?", (variant_id,)
        ).fetchone()[0]
        gone = dbm.upsert_variant(
            conn, product_id, "v-EU45", sku=None, size="EU45", size_norm="EU45", color=None
        )
        dbm.record_price(conn, gone, 100.0, 200.0, False, "USD", 100.0, 1.0, ts=ts())

        sizes = dict(dbm.sizes_in_stock(conn, product_id))
        assert sizes == {"EU44": True, "EU45": False}


class TestFormatting:
    def test_an_empty_shelf_says_so_and_offers_a_way_out(self):
        text = bot.format_list([], page=0, total=0, now=ts())
        assert "ничего нет" in text
        assert "/settings" in text

    def test_the_list_carries_price_discount_shop_and_size(self, conn):
        make_offer(conn, size="EU44", discount=45.0, price=120.0)
        rows, total = dbm.offers_for(conn)
        text = bot.format_list(rows, page=0, total=total, now=ts())
        assert "−45%" in text
        assert "$120" in text
        assert "EU44" in text
        assert "Shop" in text

    def test_the_numbers_on_the_keyboard_open_the_entries_beside_them(self, conn):
        for n in range(3):
            make_offer(conn, title=f"Shoe {n}", size=f"US{n}")
        rows, total = dbm.offers_for(conn)
        keyboard = bot.list_keyboard(rows, page=0, total=total)
        opens = [
            button["callback_data"]
            for line in keyboard["inline_keyboard"] for button in line
            if button["callback_data"].startswith("o:")
        ]
        assert opens == [f"o:{row['variant_id']}:0" for row in rows]

    def test_every_callback_fits_telegram_s_limit(self, conn):
        make_offer(conn)
        rows, total = dbm.offers_for(conn)
        buttons = [
            button
            for keyboard in (bot.list_keyboard(rows, 0, total), bot.menu_keyboard(None),
                             bot.gender_keyboard(), bot.kinds_keyboard(["shoes"]))
            for line in keyboard["inline_keyboard"] for button in line
        ]
        for button in buttons:
            data = button.get("callback_data", "")
            assert len(data.encode()) <= bot.CALLBACK_LIMIT, button

    def test_paging_arrows_appear_only_where_there_is_somewhere_to_go(self, conn):
        make_offer(conn)
        rows, _ = dbm.offers_for(conn)
        first = bot.list_keyboard(rows, page=0, total=25)
        last = bot.list_keyboard(rows, page=2, total=25)
        first_nav = [b["text"] for b in first["inline_keyboard"][-1]]
        last_nav = [b["text"] for b in last["inline_keyboard"][-1]]
        assert "⬅️" not in first_nav and "➡️" in first_nav
        assert "⬅️" in last_nav and "➡️" not in last_nav


class TestShelfIsNotOnePersonsShelf:
    """The collector must not pre-filter the shelf by one reader's sizes."""

    def test_the_shelf_config_drops_the_personal_filters(self):
        from pi.config import Config, Filters
        from pi.pipeline import shelf_config

        config = Config(
            db_path="x", sites_file="y", bot_token=None, chat_id=None, concurrency=1,
            shopify_rate=1.0, shopify_host_rate=1.0, max_shopify_stores=1,
            log_level="INFO",
            filters=Filters(sizes=("EU44",), brands_allow=("nike",), min_score=55),
        )
        shelf = shelf_config(config)
        assert shelf.filters.sizes == ()
        assert shelf.filters.brands_allow == ()
        # The thresholds that define a discount are untouched.
        assert shelf.filters.min_score == config.filters.min_score


class TestWhenThePriceDropped:
    """A month-old sale must not be presented as a fresh find."""

    def test_a_price_with_no_history_dates_from_when_it_was_first_seen(self, conn):
        """Most variants have been seen once, so this is the usual case."""
        variant_id = make_offer(conn)
        conn.execute("DELETE FROM offers")
        conn.execute(
            "UPDATE price_points SET ts = ? WHERE variant_id = ?", (ts(30), variant_id)
        )
        row = conn.execute(
            "SELECT * FROM offers WHERE variant_id = ?", (variant_id,)
        ).fetchone()
        assert row is None

        deal = _deal(variant_id, conn, dropped_hours_ago=None)
        dbm.record_offers(conn, [variant_id], [deal], dbm.utcnow())
        found_at = conn.execute(
            "SELECT found_at FROM offers WHERE variant_id = ?", (variant_id,)
        ).fetchone()[0]
        assert found_at.startswith(ts(30)[:10]), "should date from the first sighting"

    def test_a_known_drop_dates_from_the_drop(self, conn):
        variant_id = make_offer(conn)
        conn.execute("DELETE FROM offers")
        deal = _deal(variant_id, conn, dropped_hours_ago=5.0)
        now = dbm.utcnow()
        dbm.record_offers(conn, [variant_id], [deal], now)
        found_at = conn.execute(
            "SELECT found_at FROM offers WHERE variant_id = ?", (variant_id,)
        ).fetchone()[0]
        from datetime import datetime
        hours = (datetime.fromisoformat(now) - datetime.fromisoformat(found_at)).total_seconds() / 3600
        assert 4.9 < hours < 5.1


def _deal(variant_id: int, conn, dropped_hours_ago: float | None):
    from pi.deals import Deal

    product_id = conn.execute(
        "SELECT product_id FROM variants WHERE id = ?", (variant_id,)
    ).fetchone()[0]
    return Deal(
        variant_id=variant_id, product_id=product_id, price_usd=100.0,
        reference_usd=200.0, reference_source="history", discount_pct=50.0,
        saving_usd=100.0, score=80, all_time_low=False, fake_sale=False,
        dropped_hours_ago=dropped_hours_ago, history_points=2,
    )


class TestRouting:
    """Pressing a button must reach the right screen — formatters alone cannot say."""

    @staticmethod
    def _config(tmp_path):
        from pi.config import Config, Filters

        return Config(
            db_path=tmp_path / "x.db", sites_file=tmp_path / "s.txt",
            bot_token="123:AA", chat_id="42", concurrency=1,
            shopify_rate=1.0, shopify_host_rate=1.0, max_shopify_stores=1,
            log_level="INFO", filters=Filters(),
        )

    @pytest.fixture
    def calls(self):
        """Every Bot API call the bot makes, captured instead of sent."""
        return []

    @pytest.fixture
    def robot(self, conn, tmp_path, calls, monkeypatch):
        instance = bot.Bot(self._config(tmp_path), conn)

        async def record(method, payload):
            calls.append((method, payload))
            return {"username": "test"}

        monkeypatch.setattr(instance, "_call", record)
        return instance

    @staticmethod
    def _message(text: str) -> dict:
        return {"message": {"chat": {"id": 42}, "from": {"id": 7, "username": "u"}, "text": text}}

    @staticmethod
    def _press(data: str) -> dict:
        return {"callback_query": {
            "id": "1", "data": data, "from": {"id": 7, "username": "u"},
            "message": {"chat": {"id": 42}, "message_id": 5},
        }}

    @pytest.mark.asyncio
    async def test_start_offers_both_the_wizard_and_the_whole_shelf(self, robot, calls):
        await robot.handle(self._message("/start"))
        buttons = calls[0][1]["reply_markup"]["inline_keyboard"][0]
        assert [b["callback_data"] for b in buttons] == ["wizard", "p:0"]

    @pytest.mark.asyncio
    async def test_the_list_button_shows_the_list(self, robot, calls, conn):
        make_offer(conn)
        await robot.handle(self._press("p:0"))
        methods = [method for method, _ in calls]
        assert "answerCallbackQuery" in methods, "the button must stop spinning"
        assert "editMessageText" in methods

    @pytest.mark.asyncio
    async def test_opening_an_entry_that_has_since_sold_out_says_so(self, robot, calls):
        await robot.handle(self._press("o:9999:0"))
        texts = [payload.get("text", "") for method, payload in calls if method == "sendMessage"]
        assert any("закончилось" in text for text in texts)

    @pytest.mark.asyncio
    async def test_the_wizard_walks_all_four_questions_and_can_be_skipped(
        self, robot, calls, conn
    ):
        await robot.handle(self._press("wizard"))
        assert "Чьи вещи" in calls[-1][1]["text"]

        await robot.handle(self._press("set:genders:women"))
        assert "Что интересует" in calls[-1][1]["text"]

        await robot.handle(self._press("skip:kinds"))
        assert "размеры" in calls[-1][1]["text"].lower()

        await robot.handle(self._press("skip:sizes"))
        assert "марки" in calls[-1][1]["text"].lower()

        await robot.handle(self._press("skip:brands"))
        user = dbm.get_bot_user(conn, 7)
        assert user["onboarded"] == 1
        assert user["wizard_step"] is None
        assert user["genders"] == "women"

    @pytest.mark.asyncio
    async def test_typed_sizes_are_stored_normalised(self, robot, conn):
        await robot.handle(self._press("wizard"))
        await robot.handle(self._press("set:genders:men"))
        await robot.handle(self._press("skip:kinds"))
        await robot.handle(self._message("EU 44, us 10.5"))
        assert dbm.get_bot_user(conn, 7)["sizes"] == "EU44,US10.5"

    @pytest.mark.asyncio
    async def test_text_outside_the_wizard_is_not_swallowed(self, robot, calls, conn):
        dbm.upsert_bot_user(conn, 7, "42", "u", onboarded=1)
        await robot.handle(self._message("привет"))
        assert "/deals" in calls[-1][1]["text"]

    @pytest.mark.asyncio
    async def test_a_broken_update_does_not_take_the_bot_down(self, robot):
        await robot.handle({"message": {"chat": {"id": 42}}})  # no sender at all
        await robot.handle({"callback_query": {"id": "1"}})    # no chat, no sender
        # Reaching here at all is the assertion: handle() swallowed both.
