"""Paying, renewing, lapsing and being refunded."""
from __future__ import annotations

from pathlib import Path

import pytest

from pi import bot
from pi import db as dbm
from pi.config import Config, Filters

from .conftest import ts


def _config(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "test.db", sites_file=tmp_path / "sites.txt",
        bot_token="123:AA", chat_id="42", concurrency=1,
        shopify_rate=1.0, shopify_host_rate=1.0, max_shopify_stores=1,
        log_level="INFO", filters=Filters(),
    )


@pytest.fixture
def calls() -> list:
    return []


@pytest.fixture
def robot(conn, tmp_path, calls, monkeypatch):
    instance = bot.Bot(_config(tmp_path), conn)

    async def record(method, payload):
        calls.append((method, payload))
        if method == "createInvoiceLink":
            return "https://t.me/invoice/abc"
        return {"username": "test"}

    monkeypatch.setattr(instance, "_call", record)
    return instance


def only(calls: list, method: str) -> dict:
    """The payload of the one call to `method`.

    Every button press answers `answerCallbackQuery` first — otherwise the
    button spins on the sender's phone — so indexing calls by position tests
    that acknowledgement rather than the thing under test.
    """
    matching = [payload for name, payload in calls if name == method]
    assert len(matching) == 1, f"{method}: {len(matching)} calls, expected 1"
    return matching[0]


def _press(data: str) -> dict:
    return {"callback_query": {
        "id": "1", "data": data, "from": {"id": 7, "username": "u"},
        "message": {"chat": {"id": 42}, "message_id": 5},
    }}


def _message(text: str) -> dict:
    return {"message": {"chat": {"id": 42}, "from": {"id": 7, "username": "u"}, "text": text}}


def _paid(plan: str = "month", stars: int = 150, charge: str = "chg_abc", **extra) -> dict:
    payment = {
        "currency": "XTR",
        "total_amount": stars,
        "invoice_payload": f"sub:{plan}",
        "telegram_payment_charge_id": charge,
    }
    payment.update(extra)
    return {"message": {
        "chat": {"id": 42}, "from": {"id": 7, "username": "u"},
        "successful_payment": payment,
    }}


class TestBuying:
    @pytest.mark.asyncio
    async def test_the_monthly_plan_is_a_subscription(self, robot, calls):
        await robot.handle(_press("buy:month"))
        payload = only(calls, "createInvoiceLink")
        assert payload["currency"] == "XTR"
        assert payload["prices"][0]["amount"] == bot.MONTHLY_STARS
        # Telegram accepts exactly thirty days and refuses the invoice for
        # anything else.
        assert payload["subscription_period"] == 30 * 24 * 3600

    @pytest.mark.asyncio
    async def test_the_yearly_plan_renews_nothing(self, robot, calls):
        await robot.handle(_press("buy:year"))
        payload = only(calls, "createInvoiceLink")
        assert payload["prices"][0]["amount"] == bot.YEARLY_STARS
        assert "subscription_period" not in payload

    @pytest.mark.asyncio
    async def test_the_price_is_stars_not_hundredths_of_them(self, robot, calls):
        await robot.handle(_press("buy:month"))
        payload = only(calls, "createInvoiceLink")
        # XTR has no minor unit. Multiplying by 100 the way every other currency
        # wants would charge a reader fifteen thousand stars.
        assert payload["prices"][0]["amount"] == 150

    @pytest.mark.asyncio
    async def test_an_unknown_plan_buys_nothing(self, robot, calls):
        await robot.handle(_press("buy:decade"))
        # The press is still acknowledged; nothing is invoiced.
        assert [m for m, _ in calls] == ["answerCallbackQuery"]


class TestPaying:
    @pytest.mark.asyncio
    async def test_pre_checkout_is_answered(self, robot, calls):
        await robot.handle({"pre_checkout_query": {"id": "q1", "from": {"id": 7}}})
        method, payload = calls[0]
        # Ten seconds, and Telegram cancels the payment for us if we are late.
        assert (method, payload["ok"]) == ("answerPreCheckoutQuery", True)
        assert payload["pre_checkout_query_id"] == "q1"

    @pytest.mark.asyncio
    async def test_paying_opens_the_shelf(self, robot, conn):
        await robot.handle(_paid())
        assert dbm.is_subscribed(conn, 7) is True
        row = dbm.get_bot_user(conn, 7)
        assert row["charge_id"] == "chg_abc"
        assert row["stars_paid"] == 150

    @pytest.mark.asyncio
    async def test_a_year_buys_a_year(self, robot, conn):
        await robot.handle(_paid("year", stars=1500))
        row = dbm.get_bot_user(conn, 7)
        from datetime import UTC, datetime, timedelta
        left = datetime.fromisoformat(row["paid_until"]) - datetime.now(UTC)
        assert timedelta(days=364) < left <= timedelta(days=365)

    @pytest.mark.asyncio
    async def test_a_renewal_adds_a_month_rather_than_resetting_one(self, robot, conn):
        await robot.handle(_paid())
        first = dbm.get_bot_user(conn, 7)["paid_until"]
        # A real renewal is a new charge. Telegram gives it its own id.
        await robot.handle(_paid(charge="chg_second", is_recurring=True))
        second = dbm.get_bot_user(conn, 7)["paid_until"]
        assert second > first
        assert dbm.get_bot_user(conn, 7)["stars_paid"] == 300

    @pytest.mark.asyncio
    async def test_the_same_charge_twice_is_one_month(self, robot, conn):
        """A restart before the next getUpdates replays the payment."""
        await robot.handle(_paid())
        once = dbm.get_bot_user(conn, 7)["paid_until"]

        await robot.handle(_paid())

        row = dbm.get_bot_user(conn, 7)
        assert row["paid_until"] == once
        assert row["stars_paid"] == 150

    @pytest.mark.asyncio
    async def test_only_the_monthly_charge_is_the_one_cancel_stops(self, robot, conn):
        await robot.handle(_paid("month", charge="chg_month"))
        await robot.handle(_paid("year", stars=1500, charge="chg_year"))
        row = dbm.get_bot_user(conn, 7)
        # `charge_id` is the last payment, for refunds. `sub_charge_id` is the
        # one that renews itself, which is what /cancel has to name.
        assert row["charge_id"] == "chg_year"
        assert row["sub_charge_id"] == "chg_month"

    @pytest.mark.asyncio
    async def test_a_payment_is_not_read_as_an_article_number(self, robot, calls):
        """A payment arrives as a message with no text at all."""
        await robot.handle(_paid())
        methods = [m for m, _ in calls]
        assert "sendMessage" in methods
        assert all("не нашёл" not in str(p) for _, p in calls)


class TestKnowingWhereYouStand:
    @pytest.mark.asyncio
    async def test_a_free_reader_is_shown_the_pitch(self, robot, calls, conn):
        await robot.handle(_message("/subscription"))
        text = calls[0][1]["text"]
        assert "Что даёт подписка" in text

    @pytest.mark.asyncio
    async def test_a_paying_reader_is_told_until_when(self, robot, calls, conn):
        dbm.upsert_bot_user(conn, 7, "42")
        dbm.grant(conn, 7, days=30, stars=150)
        await robot.handle(_message("/subscription"))
        text = calls[0][1]["text"]
        assert "Подписка активна до" in text
        assert "150" in text

    @pytest.mark.asyncio
    async def test_a_lapsing_reader_is_told_what_is_still_working(
        self, robot, calls, conn
    ):
        dbm.upsert_bot_user(conn, 7, "42")
        dbm.grant(conn, 7, days=30)
        conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = 7", (ts(1),))
        await robot.handle(_message("/subscription"))
        text = calls[0][1]["text"]
        assert "Лента пока идёт, витрина уже закрыта" in text


class TestCancelling:
    @pytest.mark.asyncio
    async def test_cancelling_stops_the_renewal_and_keeps_the_month(self, robot, calls, conn):
        await robot.handle(_paid())
        paid_until = dbm.get_bot_user(conn, 7)["paid_until"]
        calls.clear()

        await robot.handle(_message("/cancel"))

        payload = only(calls, "editUserStarSubscription")
        assert payload["is_canceled"] is True
        assert payload["telegram_payment_charge_id"] == "chg_abc"
        # Cancelling is "not next month", not "give me back this one".
        assert dbm.get_bot_user(conn, 7)["paid_until"] == paid_until
        assert dbm.is_subscribed(conn, 7) is True

    @pytest.mark.asyncio
    async def test_cancelling_nothing_says_so(self, robot, calls, conn):
        await robot.handle(_message("/cancel"))
        assert calls[0][0] == "sendMessage"
        assert "Отменять нечего" in calls[0][1]["text"]


class TestTheDailyCheck:
    def test_a_reminder_goes_out_three_days_before(self, conn):
        from datetime import UTC, datetime, timedelta
        dbm.upsert_bot_user(conn, 7, "42")
        dbm.grant(conn, 7, days=30)
        soon = (datetime.now(UTC) + timedelta(days=2)).isoformat(timespec="seconds")
        conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = 7", (soon,))

        assert [r["id"] for r in dbm.expiring_soon(conn, within_days=3)] == [7]

    def test_grace_runs_out_and_the_profile_does_not(self, conn):
        dbm.upsert_bot_user(conn, 7, "42", sizes="EU44", brands="Nike")
        dbm.grant(conn, 7, days=30)
        conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = 7", (ts(90),))

        assert dbm.expire_due(conn) == 1
        row = dbm.get_bot_user(conn, 7)
        assert row["plan"] == "free"
        assert (row["sizes"], row["brands"]) == ("EU44", "Nike")


class TestWhatIsPromisedAndWhatIsKept:
    @pytest.mark.asyncio
    async def test_start_says_the_price_before_anybody_can_pay(self, robot, calls):
        await robot.handle(_message("/start"))
        text = calls[0][1]["text"]
        assert str(bot.MONTHLY_STARS) in text
        assert "/terms" in text
        assert "/delete_me" in text

    @pytest.mark.asyncio
    async def test_terms_name_the_refund_window_and_what_is_stored(self, robot, calls):
        await robot.handle(_message("/terms"))
        text = calls[0][1]["text"]
        assert "48 часов" in text
        assert "Что о вас хранится" in text

    @pytest.mark.asyncio
    async def test_delete_me_leaves_nothing_behind(self, robot, conn):
        dbm.upsert_bot_user(conn, 7, "42", sizes="EU44")
        product, variant = _a_product(conn, with_variant=True)
        dbm.add_favorite(conn, 7, product, None)
        conn.execute(
            "INSERT INTO alerts (product_id, variant_id, ts, price_usd, price_bucket,"
            " discount_pct, score, sent, user_id) VALUES (?, ?, ?, 10, 10, 50, 80, 1, 7)",
            (product, variant, ts()),
        )

        await robot.handle(_message("/delete_me"))

        assert dbm.get_bot_user(conn, 7) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE user_id = 7"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE user_id = 7"
        ).fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_delete_me_does_not_touch_anybody_else(self, robot, conn):
        dbm.upsert_bot_user(conn, 7, "42")
        dbm.upsert_bot_user(conn, 8, "43", sizes="EU40")
        dbm.add_favorite(conn, 8, _a_product(conn), None)

        await robot.handle(_message("/delete_me"))

        assert dbm.get_bot_user(conn, 8)["sizes"] == "EU40"
        assert conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE user_id = 8"
        ).fetchone()[0] == 1


def _a_product(conn, with_variant: bool = False):
    store = dbm.upsert_store(conn, "shop.example", platform="shopify", currency="USD")
    product = dbm.upsert_product(
        conn, store, "p1", "Air Max", "https://shop.example/p1"
    )
    if not with_variant:
        return product
    return product, dbm.upsert_variant(conn, product, "v1", sku="SKU1", size="US10")


class TestTheDailySummary:
    def test_the_summary_counts_who_pays(self, conn):
        from pi import pipeline

        for user_id in (1, 2, 3):
            dbm.upsert_bot_user(conn, user_id, str(user_id))
        dbm.grant(conn, 2, days=30, stars=150)
        dbm.grant(conn, 3, days=30, stars=150)
        conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = 3", (ts(1),))

        report = pipeline.health_report(conn)

        assert "Читателей: 3" in report
        assert "платят 1" in report
        assert "в отсрочке 1" in report
        assert "Получено звёзд всего: 300" in report

    def test_a_lapsed_plan_column_does_not_inflate_the_count(self, conn):
        from pi import pipeline

        dbm.upsert_bot_user(conn, 1, "1")
        dbm.grant(conn, 1, days=30, stars=150)
        # `pi subscriptions` corrects `plan` once a day, so between runs the
        # column says "paid" for somebody who is not. The summary must not.
        conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = 1", (ts(90),))

        assert "платят 0" in pipeline.health_report(conn)


def _dm(text: str) -> dict:
    """A real private chat: Telegram gives it the same id as the person."""
    return {"message": {"chat": {"id": 7}, "from": {"id": 7, "username": "u"}, "text": text}}


def _dm_press(data: str) -> dict:
    return {"callback_query": {
        "id": "1", "data": data, "from": {"id": 7, "username": "u"},
        "message": {"chat": {"id": 7}, "message_id": 5},
    }}


def _group(text: str, chat: int = -1001999888777) -> dict:
    return {"message": {"chat": {"id": chat}, "from": {"id": 7, "username": "u"}, "text": text}}


class TestTheOwnersPanel:
    """`/admin` opens for one person, in one chat, and elsewhere says nothing."""

    @pytest.fixture
    def owner(self, conn, tmp_path, calls, monkeypatch):
        """A bot whose .env owner is user 7 — the id the fixtures send from."""
        from dataclasses import replace

        instance = bot.Bot(replace(_config(tmp_path), chat_id="7"), conn)

        async def record(method, payload):
            calls.append((method, payload))
            return {"username": "test"}

        monkeypatch.setattr(instance, "_call", record)
        return instance

    @pytest.mark.asyncio
    async def test_a_stranger_is_not_told_the_command_exists(self, robot, calls):
        # robot's config.chat_id is "42"; the sender is user 7.
        await robot.handle(_message("/admin"))
        assert "Не понял" in calls[0][1]["text"]
        assert "дмин" not in calls[0][1]["text"]

    @pytest.mark.asyncio
    async def test_the_owner_gets_the_panel(self, owner, calls, conn):
        dbm.upsert_bot_user(conn, 8, "8")
        dbm.grant(conn, 8, days=30)

        await owner.handle(_dm("/admin"))

        text, keyboard = calls[0][1]["text"], calls[0][1]["reply_markup"]
        assert "Админ-панель" in text
        assert "платят 1" in text
        buttons = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
        assert {"adm:readers", "adm:grant", "adm:revoke"} <= set(buttons)

    @pytest.mark.asyncio
    async def test_granting_access_from_the_panel(self, owner, calls, conn):
        await owner.handle(_dm_press("adm:grant"))
        calls.clear()

        await owner.handle(_dm("999"))

        assert dbm.is_subscribed(conn, 999) is True
        # Told the owner, and told the new reader.
        recipients = [p["chat_id"] for m, p in calls if m == "sendMessage"]
        assert "999" in recipients

    @pytest.mark.asyncio
    async def test_a_grant_can_be_given_a_number_of_days(self, owner, conn):
        await owner.handle(_dm_press("adm:grant"))
        await owner.handle(_dm("999 45"))

        from datetime import UTC, datetime, timedelta
        left = datetime.fromisoformat(
            dbm.get_bot_user(conn, 999)["paid_until"]
        ) - datetime.now(UTC)
        assert timedelta(days=44) < left <= timedelta(days=45)

    @pytest.mark.asyncio
    async def test_granting_to_somebody_who_never_wrote_still_works(self, owner, conn):
        """The subscription waits for them; grant() alone would have raised."""
        await owner.handle(_dm_press("adm:grant"))
        await owner.handle(_dm("31337"))

        row = dbm.get_bot_user(conn, 31337)
        assert row is not None and row["chat_id"] == "31337"
        assert dbm.is_subscribed(conn, 31337) is True

    @pytest.mark.asyncio
    async def test_revoking_keeps_the_profile(self, owner, calls, conn):
        dbm.upsert_bot_user(conn, 999, "999", sizes="EU44")
        dbm.grant(conn, 999, days=30, stars=150)

        await owner.handle(_dm_press("adm:revoke"))
        await owner.handle(_dm("999"))

        row = dbm.get_bot_user(conn, 999)
        assert dbm.is_subscribed(conn, 999) is False
        assert row["sizes"] == "EU44"
        # Not a refund: what they paid is still on the record.
        assert row["stars_paid"] == 150

    @pytest.mark.asyncio
    async def test_a_stranger_pressing_the_button_gets_nothing(self, robot, calls):
        await robot.handle(_press("adm:readers"))
        # Only the callback acknowledgement; no panel, no reader list.
        assert [m for m, _ in calls] == ["answerCallbackQuery"]

    @pytest.mark.asyncio
    async def test_nonsense_instead_of_an_id_is_refused(self, owner, calls, conn):
        await owner.handle(_dm_press("adm:grant"))
        calls.clear()

        await owner.handle(_dm("вася"))

        assert "не похоже на id" in calls[0][1]["text"]
        assert dbm.readers(conn) == [] or all(
            r["id"] == 7 for r in dbm.readers(conn)
        )


class TestForgedButtonPayloads:
    """`callback_data` looks like the bot's own payload and is not.

    It is a client-supplied field: any MTProto client can send arbitrary bytes
    for any message carrying an inline keyboard, and /start hands one to
    everybody.
    """

    @pytest.mark.asyncio
    async def test_a_forged_field_cannot_buy_a_subscription(self, robot, conn):
        await robot.handle(_press("set:paid_until:2099-01-01T00:00:00+00:00"))

        # This alone was the whole paywall — no admin path, no owner, one button.
        assert dbm.is_subscribed(conn, 7) is False
        assert dbm.get_bot_user(conn, 7)["paid_until"] is None

    @pytest.mark.asyncio
    async def test_a_forged_field_cannot_reach_the_admin_step(self, robot, conn):
        await robot.handle(_press("set:wizard_step:admin_grant"))

        assert dbm.get_bot_user(conn, 7)["wizard_step"] is None

    @pytest.mark.asyncio
    async def test_the_real_buttons_still_work(self, robot, conn):
        await robot.handle(_press("set:genders:women"))

        assert dbm.get_bot_user(conn, 7)["genders"] == "women"

    @pytest.mark.asyncio
    async def test_save_refuses_a_column_the_bot_never_writes(self, robot):
        with pytest.raises(ValueError, match="refusing to write"):
            robot._save(7, stars_paid=999999)


class TestTheAdminStepIsNotJustAColumn:
    """Even with the step set, being the owner is asked again."""

    @pytest.mark.asyncio
    async def test_a_stranger_holding_the_step_grants_nothing(self, robot, conn):
        dbm.upsert_bot_user(conn, 7, "7")
        # Set the way the forged callback used to set it, bypassing the panel.
        conn.execute("UPDATE bot_users SET wizard_step = 'admin_grant' WHERE id = 7")

        await robot.handle(_message("7"))

        assert dbm.is_subscribed(conn, 7) is False
        assert dbm.get_bot_user(conn, 7)["wizard_step"] is None

    @pytest.mark.asyncio
    async def test_a_stranger_holding_the_step_revokes_nobody(self, robot, conn):
        dbm.upsert_bot_user(conn, 7, "7")
        dbm.upsert_bot_user(conn, 8, "8")
        dbm.grant(conn, 8, days=30)
        conn.execute("UPDATE bot_users SET wizard_step = 'admin_revoke' WHERE id = 7")

        await robot.handle(_message("8"))

        assert dbm.is_subscribed(conn, 8) is True


class TestTheOwnerInAGroup:
    @pytest.fixture
    def owner(self, conn, tmp_path, calls, monkeypatch):
        from dataclasses import replace

        instance = bot.Bot(replace(_config(tmp_path), chat_id="7"), conn)

        async def record(method, payload):
            calls.append((method, payload))
            return {"username": "test"}

        monkeypatch.setattr(instance, "_call", record)
        return instance

    @pytest.mark.asyncio
    async def test_the_panel_is_not_published_to_a_group(self, owner, calls, conn):
        dbm.upsert_bot_user(conn, 8, "8")
        dbm.grant(conn, 8, days=30)

        await owner.handle(_group("/admin"))

        text = calls[0][1]["text"]
        # The panel lists every reader's id, username, paid-to date and stars.
        assert "только в личном чате" in text
        assert "Читателей" not in text

    @pytest.mark.asyncio
    async def test_speaking_in_a_group_does_not_move_the_owners_feed(
        self, owner, conn
    ):
        await owner.handle(_dm("/start"))
        assert dbm.get_bot_user(conn, 7)["chat_id"] == "7"

        await owner.handle(_group("/start"))

        # It used to become the group's, and the hourly feed went there with it.
        assert dbm.get_bot_user(conn, 7)["chat_id"] == "7"


class TestGivingAccessToSomethingThatIsNotAPerson:
    def test_a_channel_id_is_refused(self, conn):
        with pytest.raises(ValueError, match="not a reader"):
            dbm.comp(conn, -1001234567890)

    @pytest.mark.asyncio
    async def test_the_panel_refuses_it_too(self, owner, calls, conn):
        await owner.handle(_dm_press("adm:grant"))
        calls.clear()

        await owner.handle(_dm("-1001234567890"))

        assert "отрицательные id" in calls[0][1]["text"]
        assert dbm.get_bot_user(conn, -1001234567890) is None

    @pytest.fixture
    def owner(self, conn, tmp_path, calls, monkeypatch):
        from dataclasses import replace

        instance = bot.Bot(replace(_config(tmp_path), chat_id="7"), conn)

        async def record(method, payload):
            calls.append((method, payload))
            return {"username": "test"}

        monkeypatch.setattr(instance, "_call", record)
        return instance


class TestTakingAccessFromSomebodyWhoIsStillBeingCharged:
    def test_revoke_refuses_while_the_charge_is_live(self, conn):
        dbm.upsert_bot_user(conn, 8, "8")
        dbm.grant(conn, 8, days=30, charge_id="ch1", stars=150, recurring=True)

        with pytest.raises(dbm.StillRecurring):
            dbm.revoke(conn, 8)

        # Otherwise it only appears to work: the next charge fires, on_paid
        # grants a month, and they have access again while still paying.
        assert dbm.is_subscribed(conn, 8) is True

    def test_a_comped_reader_is_revoked_normally(self, conn):
        dbm.comp(conn, 8, days=30)

        assert dbm.revoke(conn, 8) is True
        assert dbm.is_subscribed(conn, 8) is False

    def test_cancelling_first_makes_revoke_work(self, conn):
        dbm.upsert_bot_user(conn, 8, "8")
        dbm.grant(conn, 8, days=30, charge_id="ch1", recurring=True)
        conn.execute("UPDATE bot_users SET sub_charge_id = NULL WHERE id = 8")

        assert dbm.revoke(conn, 8) is True


class TestCompsAreNotCustomers:
    def test_the_summary_counts_them_apart(self, conn):
        from pi import pipeline

        dbm.upsert_bot_user(conn, 1, "1")
        dbm.grant(conn, 1, days=30, stars=150)
        dbm.comp(conn, 2)
        dbm.comp(conn, 3)

        report = pipeline.health_report(conn)

        # The number this line exists to give is "how many customers".
        assert "платят 1" in report
        assert "подарено 2" in report

    def test_a_comp_is_not_shown_as_a_date_in_2076(self, conn):
        dbm.comp(conn, 2)

        listed = bot.format_readers(conn, dbm.readers(conn))

        assert "бессрочно" in listed
        assert "2076" not in listed
