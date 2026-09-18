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
        image_url=f"https://{domain}/p.jpg",
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

    def test_women_are_not_stocked_at_all(self, conn):
        """Not a filter the reader turns off — this is a men's shop.

        The row stays written, because it is still evidence about the price of
        the same article elsewhere, and because a misreading nobody can see is a
        misreading nobody can report. It is simply never shown.
        """
        make_offer(conn, gender="women", title="Wmns")
        make_offer(conn, gender=None, title="Unsaid")
        _, total = dbm.offers_for(conn)
        assert total == 1
        _, asked = dbm.offers_for(conn, genders=["women"])
        assert asked == 0, "asking for women does not put them back"

    def test_the_owner_can_still_see_them(self, conn):
        """Otherwise a men's shoe read as women's is invisible and unfixable."""
        make_offer(conn, gender="women", title="Wmns")
        _, total = dbm.offers_for(conn, women=True)
        assert total == 1

    def test_asking_for_men_means_the_confirmed_ones(self, conn):
        """Once women are out of the shop, "men" stops being protection from
        them and starts being a preference: show me only what actually says so.

        57% of the shelf says nothing, so this narrows hard on purpose — it is
        the reader's choice, not the shop's boundary.
        """
        make_offer(conn, gender="men", title="Mens")
        make_offer(conn, gender=None, title="Unsaid")
        make_offer(conn, gender="women", title="Wmns")
        _, total = dbm.offers_for(conn, genders=["men"])
        assert total == 1
        _, everything = dbm.offers_for(conn)
        assert everything == 2, "by default the unsaid are shown too"

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
    def test_the_entry_carries_price_discount_shop_and_size(self, conn):
        make_offer(conn, size="EU44", discount=45.0, price=120.0)
        rows, _ = dbm.offers_for(conn)
        text = bot.format_entry(rows[0], ts())
        assert "−45%" in text
        assert "$120" in text
        assert "EU44" in text
        assert "Shop" in text

    def test_the_header_says_where_you_are(self):
        header = bot.format_page_header(page=2, total=48)
        assert "стр. 3" in header
        assert "48" in header

    def test_each_photo_carries_a_way_to_buy_and_a_way_to_read_more(self, conn):
        make_offer(conn)
        rows, _ = dbm.offers_for(conn)
        buttons = bot.entry_keyboard(rows[0], page=0)["inline_keyboard"][0]
        assert buttons[0]["url"].startswith("http")
        assert buttons[1]["callback_data"] == f"o:{rows[0]['variant_id']}:0"

    def test_every_callback_fits_telegram_s_limit(self, conn):
        make_offer(conn)
        rows, _ = dbm.offers_for(conn)
        buttons = [
            button
            for keyboard in (bot.entry_keyboard(rows[0], 0), bot.nav_keyboard(0, 25),
                             bot.menu_keyboard(None), bot.gender_keyboard(),
                             bot.kinds_keyboard(["shoes"]))
            for line in keyboard["inline_keyboard"] for button in line
        ]
        for button in buttons:
            data = button.get("callback_data", "")
            assert len(data.encode()) <= bot.CALLBACK_LIMIT, button

    def test_paging_arrows_appear_only_where_there_is_somewhere_to_go(self):
        first = [b["text"] for b in bot.nav_keyboard(0, 25)["inline_keyboard"][0]]
        last = [b["text"] for b in bot.nav_keyboard(4, 25)["inline_keyboard"][0]]
        assert not any("Назад" in t for t in first)
        assert any("Дальше" in t for t in first)
        assert any("Назад" in t for t in last)
        assert not any("Дальше" in t for t in last)


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

    @staticmethod
    def _shelf_buttons(calls) -> list[dict]:
        rows = calls[0][1]["reply_markup"]["inline_keyboard"]
        return [b for row in rows for b in row if "web_app" in b or "url" in b]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("private, sender, sees", [
        (False, 7, True),    # an open shelf is everybody's
        (True, 42, True),    # a private one is the owner's (TELEGRAM_CHAT_ID=42)
        (True, 7, False),    # and nobody else gets a button that opens nothing
    ])
    async def test_who_is_offered_the_shelf(self, conn, tmp_path, monkeypatch, private, sender, sees):
        from dataclasses import replace

        config = replace(
            self._config(tmp_path), web_url="https://shelf.example", web_private=private
        )
        robot = bot.Bot(config, conn)
        calls = []

        async def record(method, payload):
            calls.append((method, payload))
            return {"username": "test"}

        monkeypatch.setattr(robot, "_call", record)
        await robot.handle({"message": {
            "chat": {"id": sender}, "from": {"id": sender, "username": "u"}, "text": "/start",
        }})
        assert bool(self._shelf_buttons(calls)) is sees

    @pytest.mark.asyncio
    async def test_the_list_button_sends_a_photo_for_every_offer(self, robot, calls, conn):
        """Clothes are chosen by looking at them."""
        for n in range(3):
            make_offer(conn, title=f"Shoe {n}", size=f"US{n}")
        await robot.handle(self._press("p:0"))
        methods = [method for method, _ in calls]
        assert "answerCallbackQuery" in methods, "the button must stop spinning"
        assert methods.count("sendPhoto") == 3, "one photo per offer"
        # A header before them and a navigation footer after.
        assert methods.count("sendMessage") == 2

    @pytest.mark.asyncio
    async def test_an_offer_with_no_picture_still_appears(self, robot, calls, conn):
        variant_id = make_offer(conn)
        conn.execute(
            "UPDATE products SET image_url = NULL WHERE id = "
            "(SELECT product_id FROM offers WHERE variant_id = ?)", (variant_id,)
        )
        await robot.handle(self._press("p:0"))
        sent = [p for m, p in calls if m == "sendMessage" and "reply_markup" in p]
        assert any("−45%" in p.get("text", "") for p in sent)

    @pytest.mark.asyncio
    async def test_an_empty_shelf_says_so(self, robot, calls):
        await robot.handle(self._press("p:0"))
        texts = [p.get("text", "") for m, p in calls if m == "sendMessage"]
        assert any("ничего нет" in t for t in texts)

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
        """Nothing matches "привет", and the reply still says what to do next."""
        dbm.upsert_bot_user(conn, 7, "42", "u", onboarded=1)
        await robot.handle(self._message("привет"))
        assert "/deals" in calls[-1][1]["text"]

    @pytest.mark.asyncio
    async def test_an_article_number_is_priced_rather_than_shrugged_at(
        self, robot, calls, conn
    ):
        """The one question the shelf cannot answer: what does this cost anywhere."""
        dbm.upsert_bot_user(conn, 7, "42", "u", onboarded=1)
        for n, (domain, price) in enumerate(
            [("cheap.example", 90.0), ("dear.example", 150.0)]
        ):
            store = dbm.upsert_store(conn, domain, platform="shopify", currency="USD")
            product = dbm.upsert_product(
                conn, store, f"p{n}", "Nike Air Force 1 CW2288-111",
                f"https://{domain}/p",
            )
            dbm.set_product_keys(conn, product, [("style", "CW2288-111")])
            variant = dbm.upsert_variant(conn, product, f"v{n}")
            dbm.record_price(conn, variant, price, None, True, "USD", price, 1.0)

        await robot.handle(self._message("CW2288-111"))

        text = calls[-1][1]["text"]
        assert "CW2288-111" in text
        assert "cheap.example" in text and "$90" in text
        assert text.index("cheap.example") < text.index("dear.example"), "cheapest first"

    @pytest.mark.asyncio
    async def test_a_broken_update_does_not_take_the_bot_down(self, robot):
        await robot.handle({"message": {"chat": {"id": 42}}})  # no sender at all
        await robot.handle({"callback_query": {"id": "1"}})    # no chat, no sender
        # Reaching here at all is the assertion: handle() swallowed both.


class TestTheSizeList:
    """What is left in stock, read by a person."""

    def test_one_label_appears_once(self, conn):
        """A shop selling a jacket in two colourways has "L" twice."""
        variant_id = make_offer(conn, size="L")
        product_id = conn.execute(
            "SELECT product_id FROM offers WHERE variant_id = ?", (variant_id,)
        ).fetchone()[0]
        second = dbm.upsert_variant(
            conn, product_id, "v-L-black", sku=None, size="L", size_norm="L", color="black"
        )
        dbm.record_price(conn, second, 100.0, 200.0, False, "USD", 100.0, 1.0, ts=ts())
        assert dbm.sizes_in_stock(conn, product_id) == [("L", True)]

    def test_a_label_counts_as_available_if_any_variant_is(self, conn):
        variant_id = make_offer(conn, size="XL", in_stock=False)
        product_id = conn.execute(
            "SELECT product_id FROM offers WHERE variant_id = ?", (variant_id,)
        ).fetchone()[0]
        second = dbm.upsert_variant(
            conn, product_id, "v-XL-red", sku=None, size="XL", size_norm="XL", color="red"
        )
        dbm.record_price(conn, second, 100.0, 200.0, True, "USD", 100.0, 1.0, ts=ts())
        assert dbm.sizes_in_stock(conn, product_id) == [("XL", True)]

    def test_sizes_are_ordered_the_way_they_are_read(self):
        from pi.db import _size_order

        labels = ["US10", "US2", "XL", "XS", "EU44.5", "EU44", "L"]
        assert sorted(labels, key=_size_order) == [
            "XS", "L", "XL", "EU44", "EU44.5", "US2", "US10",
        ]


class TestLandedPrice:
    """What it costs delivered, which is what decides whether to buy."""

    @staticmethod
    def _rules():
        from pi import landed

        return landed.Rules(
            destinations={
                "NO": {"name": "Норвегия", "vat_pct": 25.0,
                       "duty_pct": {"clothing": 10.7, "shoes": 0.0, "unknown": 10.7},
                       "duty_free_usd": 0.0, "clearance_fee_usd": 15.0},
                "UA": {"name": "Украина", "vat_pct": 20.0,
                       "duty_pct": {"clothing": 10.0, "shoes": 10.0, "unknown": 10.0},
                       "duty_free_eur": 150.0, "clearance_fee_usd": 0.0},
            },
            shipping={"US": {"NO": 35.0, "UA": 35.0}, "EU": {"NO": 20.0, "UA": 18.0}},
            free_over={"EU": 200.0},
            per_shop={},
        )

    def test_norway_charges_vat_from_the_first_dollar(self):
        from pi import landed

        item = landed.landed_for(self._rules(), "NO", 100.0, "shoes", "s.com", "US")
        assert item.vat_usd == pytest.approx(25.0)
        assert item.duty_usd == 0.0, "no duty on shoes"
        assert item.total_usd == pytest.approx(100 + 35 + 25 + 15)

    def test_norway_charges_duty_on_clothing_but_not_shoes(self):
        from pi import landed

        shoes = landed.landed_for(self._rules(), "NO", 100.0, "shoes", "s.com", "US")
        coat = landed.landed_for(self._rules(), "NO", 100.0, "clothing", "s.com", "US")
        assert coat.duty_usd > shoes.duty_usd == 0.0

    def test_ukraine_charges_nothing_below_the_allowance(self):
        from pi import landed

        item = landed.landed_for(
            self._rules(), "UA", 100.0, "shoes", "s.com", "US", eur_usd=1.08
        )
        assert (item.duty_usd, item.vat_usd) == (0.0, 0.0)
        assert item.total_usd == pytest.approx(135.0), "only the postage"

    def test_ukraine_charges_only_on_the_excess(self):
        """The easy mistake overstates a €160 parcel sixteenfold."""
        from pi import landed

        item = landed.landed_for(
            self._rules(), "UA", 200.0, "shoes", "s.com", "US", eur_usd=1.0
        )
        assert item.duty_usd == pytest.approx(5.0), "10% of the 50 above 150"
        assert item.vat_usd == pytest.approx(11.0), "20% of excess plus duty"

    def test_free_shipping_over_a_threshold_is_honoured(self):
        from pi import landed

        cheap = landed.landed_for(self._rules(), "NO", 100.0, "shoes", "s.com", "DE")
        dear = landed.landed_for(self._rules(), "NO", 250.0, "shoes", "s.com", "DE")
        assert cheap.shipping_usd == 20.0
        assert dear.shipping_usd == 0.0

    def test_a_shop_you_have_ordered_from_overrides_the_guess(self):
        from pi import landed

        rules = self._rules()
        rules = landed.Rules(
            rules.destinations, rules.shipping, rules.free_over,
            per_shop={"s.com": {"NO": 5.0}},
        )
        item = landed.landed_for(rules, "NO", 100.0, "shoes", "S.com", "US")
        assert item.shipping_usd == 5.0, "matched case-insensitively"

    def test_delivery_can_move_an_offer_down_the_list_but_not_off_it(self):
        from pi import landed

        ruinous = [landed.Landed("NO", "Норвегия", 20.0, 90.0, 0.0, 0.0, 15.0)]
        assert landed.penalty(ruinous) == landed.MAX_PENALTY

    def test_the_cheaper_destination_is_the_one_judged(self):
        """You choose where to send it, so you are not punished for the worse route."""
        from pi import landed

        items = [
            landed.Landed("NO", "Норвегия", 100.0, 90.0, 0.0, 0.0, 15.0),
            landed.Landed("UA", "Украина", 100.0, 5.0, 0.0, 0.0, 0.0),
        ]
        assert landed.penalty(items) == landed.penalty([items[1]])

    def test_with_no_shipping_file_nothing_is_claimed(self, tmp_path):
        from pi import landed

        rules = landed.load_rules(tmp_path / "absent.toml")
        assert not rules.enabled
        assert landed.landed_all(rules, 100.0, "shoes", "s.com", "US") == []


class TestTheShelfButton:
    """The way out of the chat and into the whole shelf."""

    def test_no_address_means_no_button(self):
        """A link to a machine the reader is not sitting at is worse than none."""
        assert bot.shelf_button(None) is None
        assert bot.shelf_button("") is None

    def test_https_opens_inside_telegram(self):
        [button] = bot.shelf_button("https://shelf.example")
        assert button["web_app"] == {"url": "https://shelf.example"}

    def test_plain_http_becomes_an_ordinary_link(self):
        """Telegram will not run a page inside itself without TLS."""
        [button] = bot.shelf_button("http://127.0.0.1:8000")
        assert button["url"] == "http://127.0.0.1:8000"
        assert "web_app" not in button

    def test_the_menu_carries_it_only_when_there_is_one(self, conn):
        user = dbm.upsert_bot_user(conn, 1, "42", "someone")
        without = bot.menu_keyboard(user, None)["inline_keyboard"]
        with_it = bot.menu_keyboard(user, "https://shelf.example")["inline_keyboard"]
        assert len(with_it) == len(without) + 1
        assert any("web_app" in b for row in with_it for b in row)


class TestCountingInRussian:
    """"26 магазин(ов)" is a developer showing through the text."""

    def test_it_declines_the_way_the_language_does(self):
        def say(n):
            return bot.plural(n, "магазин", "магазина", "магазинов")

        assert say(1) == "1 магазин"
        assert say(2) == "2 магазина"
        assert say(5) == "5 магазинов"
        assert say(11) == "11 магазинов", "the teens are the exception"
        assert say(21) == "21 магазин"
        assert say(112) == "112 магазинов"
