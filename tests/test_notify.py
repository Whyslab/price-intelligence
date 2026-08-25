"""Message formatting and Telegram delivery."""
from __future__ import annotations

import json

import httpx
import respx

from pi import notify
from pi.deals import Deal

TOKEN, CHAT = "123:AA", "42"


def a_deal(**overrides) -> Deal:
    base = dict(
        variant_id=1, product_id=1, price_usd=121.0, reference_usd=220.0, reference_source="tag",
        discount_pct=45.0, saving_usd=99.0, score=88, all_time_low=True,
        fake_sale=False, dropped_hours_ago=4.0, history_points=3,
    )
    return Deal(**{**base, **overrides})


class TestCaption:
    def test_carries_everything_the_reader_needs(self):
        caption = notify.format_caption(
            a_deal(),
            title="Air Jordan 4 Retro", url="https://shop.example/aj4",
            brand="Nike", size="US 10", sku="308497-060",
            store="Overkill", country="DE", native_price=112.0, currency="EUR",
        )
        assert "−45%" in caption            # how big the discount is
        assert "$99" in caption             # how much is actually saved
        assert "Air Jordan 4 Retro" in caption
        assert "Nike" in caption
        assert "US 10" in caption
        assert "308497-060" in caption
        assert "$121" in caption
        assert "220" in caption
        assert "Минимум за всё время" in caption
        assert "Overkill (DE)" in caption
        assert "€112" in caption            # the price actually charged
        assert "https://shop.example/aj4" in caption

    def test_marks_a_permanent_sale(self):
        caption = notify.format_caption(
            a_deal(fake_sale=True), title="X", url="https://x"
        )
        assert "вечная распродажа" in caption

    def test_escapes_html_in_shop_supplied_text(self):
        """Product titles are not trusted input — parse_mode is HTML."""
        caption = notify.format_caption(
            a_deal(), title='Nike <b>"Bred"</b> & Co', url="https://x", brand="A&B"
        )
        assert "&lt;b&gt;" in caption
        assert "&amp;" in caption
        assert "<b>Nike" not in caption

    def test_fits_inside_the_telegram_caption_limit(self):
        caption = notify.format_caption(
            a_deal(), title="Very long product name " * 80, url="https://x" + "y" * 300,
            brand="B" * 200, sku="S" * 200, store="T" * 200,
        )
        assert len(caption) <= notify.CAPTION_LIMIT
        assert not caption.endswith(" ")     # trimmed at a line, not mid-word

    def test_trimming_keeps_whole_lines(self):
        assert notify._trim("aaa\nbbb\nccc", 8) == "aaa\nbbb"
        assert notify._trim("short", 100) == "short"


class TestDelivery:
    @respx.mock
    async def test_a_deal_is_sent_as_a_photo_with_a_caption(self):
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_deal("caption", "https://cdn.example/shoe.jpg") is True

        body = json.loads(route.calls[0].request.content)
        assert body["photo"] == "https://cdn.example/shoe.jpg"
        assert body["caption"] == "caption"
        assert body["parse_mode"] == "HTML"

    @respx.mock
    async def test_an_unfetchable_image_falls_back_to_text(self):
        """The picture failing must not cost us the notification."""
        respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
            return_value=httpx.Response(
                400, json={"ok": False, "description": "wrong file identifier"}
            )
        )
        text = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_deal("caption", "https://cdn.example/broken.jpg") is True
        assert text.called

    @respx.mock
    async def test_no_image_goes_straight_to_text(self):
        photo = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto")
        text = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_deal("caption", None) is True
        assert not photo.called
        assert text.called

    @respx.mock
    async def test_rate_limit_waits_the_requested_time_then_retries(self):
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            side_effect=[
                httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_text("hi") is True
        assert route.call_count == 2

    @respx.mock
    async def test_a_rejected_message_reports_failure_rather_than_pretending(self):
        respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(403, json={"ok": False, "description": "bot was blocked"})
        )
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_text("hi") is False

    @respx.mock
    async def test_network_failure_returns_false_and_does_not_raise(self, monkeypatch):
        async def instant(_seconds):
            return None

        monkeypatch.setattr(notify.asyncio, "sleep", instant)  # skip the real backoff
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            side_effect=httpx.ConnectError("offline")
        )
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_text("hi") is False
        assert route.call_count == notify.MAX_RETRIES
