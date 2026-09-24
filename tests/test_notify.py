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


class TestWhereTheWasPriceCameFrom:
    """"было 300" reads the same whether the 300 is the brand's recommendation,
    what five other shops charge, or what this shop typed on the label on Tuesday.
    The caption has to say which."""

    def test_the_shops_own_floor(self):
        caption = notify.format_caption(
            a_deal(reference_source="history", reference_usd=200.0),
            title="T", url="https://u",
        )
        assert "минимум за 30 дней" in caption

    def test_what_other_shops_charge(self):
        caption = notify.format_caption(
            a_deal(reference_source="market", reference_usd=200.0, market_shops=6),
            title="T", url="https://u",
        )
        assert "в других магазинах" in caption
        assert "6" in caption

    def test_the_recommended_price(self):
        caption = notify.format_caption(
            a_deal(reference_source="msrp", reference_usd=200.0, market_shops=5),
            title="T", url="https://u",
        )
        assert "рекомендованная" in caption

    def test_the_shops_own_label_is_named_as_such(self):
        caption = notify.format_caption(a_deal(), title="T", url="https://u")
        assert "зачёркнуто в магазине" in caption

    def test_an_inflated_label_is_called_out(self):
        caption = notify.format_caption(
            a_deal(reference_source="msrp", reference_usd=200.0, market_shops=6,
                   inflated_tag=True, msrp_usd=200.0),
            title="T", url="https://u",
        )
        assert "выше рекомендованной" in caption

    def test_a_shop_that_computes_its_discounts_is_called_out(self):
        caption = notify.format_caption(
            a_deal(rule_priced=True), title="T", url="https://u"
        )
        assert "по правилу" in caption

    def test_being_cheaper_than_everyone_is_worth_saying(self):
        caption = notify.format_caption(
            a_deal(reference_source="history", beats_market=True, market_shops=7),
            title="T", url="https://u",
        )
        assert "Дешевле, чем в других магазинах" in caption


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
        respx.get("https://cdn.example/broken.jpg").mock(return_value=httpx.Response(404))
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_deal("caption", "https://cdn.example/broken.jpg") is True
        assert text.called

    @respx.mock
    async def test_a_picture_telegram_would_not_fetch_is_uploaded_instead(self, monkeypatch):
        """Some hosts turn Telegram's fetcher away and not ours."""
        async def public(url):
            return True

        monkeypatch.setattr(notify, "_is_public", public)
        photo = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
            side_effect=[
                httpx.Response(
                    400, json={"ok": False, "description": "failed to get HTTP URL content"}
                ),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        text = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        respx.get("https://img.example/shoe.jpg").mock(
            return_value=httpx.Response(
                200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}
            )
        )
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_deal("caption", "https://img.example/shoe.jpg") is True
        assert photo.call_count == 2
        assert b"multipart/form-data" in photo.calls[1].request.headers["content-type"].encode()
        assert not text.called

    @respx.mock
    async def test_a_picture_on_a_private_address_is_never_fetched(self):
        """The address is a shop's to write. One pointing into this machine or
        this network would otherwise go to a reader's chat as a "picture"."""
        respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
            return_value=httpx.Response(
                400, json={"ok": False, "description": "failed to get HTTP URL content"}
            )
        )
        text = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        inside = respx.get("http://127.0.0.1:8000/api/offers")
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_deal("caption", "http://127.0.0.1:8000/api/offers") is True
        assert not inside.called
        assert text.called

    @staticmethod
    def _refused_by_telegram():
        """Telegram cannot fetch the address, and would take an upload."""
        photo = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
            side_effect=[
                httpx.Response(
                    400, json={"ok": False, "description": "failed to get HTTP URL content"}
                ),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        text = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        return photo, text

    @respx.mock
    async def test_a_name_that_resolves_inside_the_second_time_is_not_read(self, monkeypatch):
        """Review 24.09: the name is resolved once to check it and again to
        connect, so a host can pass the check and then answer from 127.0.0.1."""

        async def public(url):
            return True

        class Inside:
            def get_extra_info(self, name):
                return ("127.0.0.1", 80) if name == "server_addr" else None

        monkeypatch.setattr(notify, "_is_public", public)
        photo, text = self._refused_by_telegram()
        respx.get("https://img.example/shoe.jpg").mock(
            return_value=httpx.Response(
                200, content=b"secret", headers={"content-type": "image/jpeg"},
                extensions={"network_stream": Inside()},
            )
        )
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_deal("caption", "https://img.example/shoe.jpg") is True
        assert photo.call_count == 1, "nothing from inside was uploaded"
        assert text.called

    @respx.mock
    async def test_a_host_that_drips_its_picture_is_given_up_on(self, monkeypatch):
        """Review 24.09: a byte a second held the download for as long as the
        host liked — the per-read timeout never fired."""
        import asyncio

        async def public(url):
            return True

        class Drip(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    yield b"x"

        monkeypatch.setattr(notify, "_is_public", public)
        monkeypatch.setattr(notify, "DOWNLOAD_SECONDS", 0.3)
        photo, text = self._refused_by_telegram()
        respx.get("https://img.example/shoe.jpg").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "image/jpeg"}, stream=Drip()
            )
        )
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_deal("caption", "https://img.example/shoe.jpg") is True
        assert photo.call_count == 1 and text.called

    def test_an_address_is_public_only_if_it_really_is(self):
        assert notify._global("8.8.8.8")
        assert not notify._global("127.0.0.1")
        assert not notify._global("::ffff:127.0.0.1"), "IPv4 inside IPv6 is still IPv4"
        assert not notify._global("fe80::1%eth0")
        assert not notify._global("not an address")

    @respx.mock
    async def test_a_refusal_that_is_not_about_the_picture_uploads_nothing(self, monkeypatch):
        async def public(url):
            return True

        monkeypatch.setattr(notify, "_is_public", public)
        respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
            return_value=httpx.Response(
                400, json={"ok": False, "description": "can't parse entities"}
            )
        )
        respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        picture = respx.get("https://img.example/shoe.jpg")
        async with notify.Telegram(TOKEN, CHAT) as tg:
            await tg.send_deal("caption", "https://img.example/shoe.jpg")
        assert not picture.called

    @respx.mock
    async def test_a_page_that_is_not_a_picture_is_not_uploaded(self, monkeypatch):
        async def public(url):
            return True

        monkeypatch.setattr(notify, "_is_public", public)
        respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
            return_value=httpx.Response(
                400, json={"ok": False, "description": "failed to get HTTP URL content"}
            )
        )
        text = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        respx.get("https://img.example/shoe.jpg").mock(
            return_value=httpx.Response(200, text="<html>captcha</html>",
                                        headers={"content-type": "text/html"})
        )
        async with notify.Telegram(TOKEN, CHAT) as tg:
            assert await tg.send_deal("caption", "https://img.example/shoe.jpg") is True
        assert text.called


class TestTheAddressTelegramFetchesAPictureFrom:
    """Shopify's CDN serves the original upload unless asked for a size, and
    Telegram gave up on a 4284×5712, 2.3 MB one: 18 of 209 alerts in a week
    went out without their photo."""

    def test_a_shopify_picture_is_asked_for_at_a_sane_width(self):
        url = "https://cdn.shopify.com/s/files/1/0605/files/shoe.jpg?v=1790129347"
        assert notify.telegram_photo(url) == (
            "https://cdn.shopify.com/s/files/1/0605/files/shoe.jpg?v=1790129347&width=1000"
        )

    def test_a_shop_domain_cdn_path_counts_too(self):
        url = "https://shop.example/cdn/shop/files/shoe.jpg?v=1&width=4000"
        assert notify.telegram_photo(url) == (
            "https://shop.example/cdn/shop/files/shoe.jpg?v=1&width=1000"
        )

    def test_any_other_host_is_left_alone(self):
        url = "https://images.example/shoe.jpg?w=2000"
        assert notify.telegram_photo(url) == url

    def test_nothing_that_is_not_a_web_address(self):
        assert notify.telegram_photo(None) is None
        assert notify.telegram_photo("javascript:alert(1)") is None

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


class TestTheReferencePhraseNamesItsOwnEvidence:
    """"было 300" reads the same whichever of four things the 300 is, so the
    line names the source — and must name the count that belongs to it."""

    def test_the_recommended_price_counts_shops_that_struck_one_through(self):
        """Not shops that merely quote a price. `agreeing_prices` can keep a
        shop's price while it shows no tag at all, so the two differ."""
        deal = Deal(
            variant_id=1, product_id=1, price_usd=120.0, reference_usd=200.0,
            reference_source="msrp", discount_pct=40.0, saving_usd=80.0, score=70,
            all_time_low=False, fake_sale=False, dropped_hours_ago=None,
            history_points=1, market_shops=9, msrp_usd=200.0, msrp_shops=3,
        )
        assert "по 3 магазинам" in notify._reference_phrase(deal)
        assert "по 9" not in notify._reference_phrase(deal)

    def test_the_market_price_still_counts_shops_with_a_price(self):
        deal = Deal(
            variant_id=1, product_id=1, price_usd=120.0, reference_usd=200.0,
            reference_source="market", discount_pct=40.0, saving_usd=80.0, score=70,
            all_time_low=False, fake_sale=False, dropped_hours_ago=None,
            history_points=1, market_shops=9, msrp_usd=200.0, msrp_shops=3,
        )
        assert "по 9" in notify._reference_phrase(deal)


class TestTellingSomebodyAboutSomethingTheyFollow:
    """A different comparison from every other line: not "cheaper than the
    market" but "cheaper than when you looked"."""

    def test_it_names_the_price_they_last_saw(self):
        caption = notify.format_caption(
            a_deal(watched=True, price_usd=149.0),
            title="Salomon XT-6", url="https://shop.example/p", since_usd=180.0,
        )

        assert "Вы следите за этой вещью" in caption
        assert "было $180" in caption

    def test_without_a_recorded_price_it_simply_says_less(self):
        caption = notify.format_caption(
            a_deal(watched=True), title="Salomon XT-6", url="https://shop.example/p"
        )

        assert "Вы следите за этой вещью" in caption
        assert "было" not in caption

    def test_a_thing_that_went_up_is_not_reported_as_a_fall(self):
        """The line exists to show movement, and this movement is the wrong way."""
        caption = notify.format_caption(
            a_deal(watched=True, price_usd=200.0),
            title="Salomon XT-6", url="https://shop.example/p", since_usd=180.0,
        )

        assert "было" not in caption

    def test_nothing_is_said_to_somebody_who_follows_nothing(self):
        caption = notify.format_caption(
            a_deal(), title="Salomon XT-6", url="https://shop.example/p", since_usd=180.0,
        )

        assert "следите" not in caption
