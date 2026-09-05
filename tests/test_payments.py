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


def _paid(plan: str = "month", stars: int = 150, **extra) -> dict:
    payment = {
        "currency": "XTR",
        "total_amount": stars,
        "invoice_payload": f"sub:{plan}",
        "telegram_payment_charge_id": "chg_abc",
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
        await robot.handle(_paid(is_recurring=True))
        second = dbm.get_bot_user(conn, 7)["paid_until"]
        assert second > first
        assert dbm.get_bot_user(conn, 7)["stars_paid"] == 300

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
