"""Telegram delivery.

Deals go out as sendPhoto with an HTML caption, because a sneaker alert without
a picture of the sneaker is close to useless. Telegram caps a caption at 1024
characters (a plain message at 4096), so the caption is built to fit and trimmed
at a line boundary rather than mid-word. If a product has no image, or Telegram
refuses to fetch the one it has, the same text is sent as a normal message.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .deals import Deal

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024
# The width asked of an image CDN that can resize. Wider than any phone shows a
# notification photo, a fraction of the original's weight.
PHOTO_WIDTH = 1000
# Telegram takes an uploaded photo up to 10 MB; past that it is not worth a try.
UPLOAD_LIMIT = 10 * 1024 * 1024


async def _is_public(url: str) -> bool:
    """Whether every address the URL's host resolves to is a public one."""
    host = urlsplit(url).hostname
    if not host:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


def telegram_photo(url: str | None) -> str | None:
    """The address Telegram should fetch a product's picture from.

    Shopify's CDN hands out the original upload unless it is asked for a size —
    4284×5712 pixels and 2.3 MB for one www.thesneakcity.com sneaker — and
    Telegram gives up fetching those: 18 of 209 alerts in a week went out as
    bare text. Asked for 1000 pixels wide the same file is 350 KB. Other hosts
    are left alone; nothing says what they would do with the parameter.
    """
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return None
    if parts.netloc != "cdn.shopify.com" and "/cdn/shop/" not in parts.path:
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in ("width", "height", "crop")
    ]
    query.append(("width", str(PHOTO_WIDTH)))
    return urlunsplit(parts._replace(query=urlencode(query)))
MESSAGE_LIMIT = 4096
MAX_RETRIES = 3

_CURRENCY_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "KRW": "₩", "PLN": "zł"}


def _money(amount: float, currency: str = "USD") -> str:
    symbol = _CURRENCY_SYMBOL.get(currency.upper(), "")
    body = f"{amount:,.0f}" if amount >= 100 else f"{amount:,.2f}"
    return f"{symbol}{body}" if symbol and currency != "PLN" else f"{body} {currency}".strip()


def _age(hours: float | None) -> str | None:
    if hours is None:
        return None
    if hours < 1:
        return "цена упала только что"
    if hours < 24:
        return f"цена упала {hours:.0f} ч назад"
    days = hours / 24
    if days < 14:
        return f"цена держится {days:.0f} дн"
    return f"цена держится {days / 7:.0f} нед"


def _reference_phrase(deal: Deal) -> str:
    """Say where the "was" price came from, because they are not equally good.

    "было 300" reads the same whether 300 is what the brand recommends, what five
    other shops charge, or what this shop wrote on the label last Tuesday. Naming
    the source is what lets the reader tell those apart.
    """
    price = _money(deal.reference_usd)
    if deal.reference_source == "history":
        return f"минимум за 30 дней {price}"
    if deal.reference_source == "market":
        return f"в других магазинах {price}, по {deal.market_shops}"
    if deal.reference_source == "msrp":
        return f"рекомендованная {price}, по {deal.msrp_shops} магазинам"
    return f"зачёркнуто в магазине {price}"


def _trim(text: str, limit: int) -> str:
    """Cut to the limit on a line boundary so a message never ends mid-sentence."""
    if len(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        cost = len(line) + (1 if kept else 0)  # the separator, only between lines
        if used + cost > limit:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept) if kept else text[: limit - 1] + "…"


def format_caption(
    deal: Deal,
    *,
    title: str,
    url: str,
    brand: str | None = None,
    size: str | None = None,
    sku: str | None = None,
    store: str | None = None,
    country: str | None = None,
    native_price: float | None = None,
    currency: str = "USD",
    landed: list | None = None,
    since_usd: float | None = None,
    limit: int = CAPTION_LIMIT,
) -> str:
    """Build the HTML body of a deal notification."""
    heat = "🔥" if deal.score >= 80 else ("✅" if deal.score >= 65 else "👍")
    lines = [
        f"{heat} <b>−{deal.discount_pct:.0f}%</b> · экономия {_money(deal.saving_usd)}",
        "",
    ]
    if brand:
        lines.append(f"<b>{escape(brand)}</b>")
    lines.append(escape(title))

    details = []
    if size:
        details.append(f"Размер: {escape(size)}")
    if sku:
        details.append(f"SKU <code>{escape(sku)}</code>")
    if details:
        lines.append(" · ".join(details))
    lines.append("")

    lines.append(
        f"💰 <b>{_money(deal.price_usd)}</b> ({_reference_phrase(deal)})"
    )
    if deal.watched:
        # What it cost when this reader was last told about it. A different
        # comparison from every other line here, and the one somebody following
        # a particular thing actually asked for: not "cheaper than the market"
        # but "cheaper than when you looked".
        lines.append(
            "⭐ Вы следите за этой вещью"
            + (f" — было {_money(since_usd)}" if since_usd and since_usd > deal.price_usd else "")
        )
    if deal.all_time_low:
        lines.append("📉 Минимум за всё время наблюдения")
    if deal.beats_market and deal.reference_source != "market":
        lines.append(f"💎 Дешевле, чем в других магазинах ({deal.market_shops})")
    if deal.inflated_tag and deal.msrp_usd:
        lines.append(
            f"🚩 Магазин зачеркнул цену выше рекомендованной {_money(deal.msrp_usd)}"
        )
    if deal.also_in_shops:
        lines.append(
            f"🔁 Тот же артикул со скидкой ещё в {deal.also_in_shops} магазинах — "
            "это лучшее из предложений"
        )
    if deal.rule_priced:
        lines.append("⚠️ Магазин считает скидки по правилу, а не от прежней цены")
    if deal.fake_sale:
        lines.append("⚠️ Зачёркнутая цена не менялась неделями — «вечная распродажа»")

    # Shop names arrive as the shop wrote them, trailing spaces and all.
    where = escape(store.strip()) if store and store.strip() else None
    if where and country:
        where = f"{where} ({escape(country)})"
    if where:
        native = (
            f" · в магазине {_money(native_price, currency)}"
            if native_price is not None and currency.upper() != "USD"
            else ""
        )
        lines.append(f"🏪 {where}{native}")

    age = _age(deal.dropped_hours_ago)
    if age:
        lines.append(f"🕐 {age}")

    # Cheapest route first, and only the cheapest named in full: a caption is
    # capped at 1024 characters and this is the last thing that should push the
    # link out of it.
    for item in sorted(landed or [], key=lambda i: i.total_usd)[:2]:
        extra = item.total_usd - item.price_usd
        lines.append(
            f"📦 {item.name}: {_money(item.total_usd)} с доставкой "
            f"(+{_money(extra)}, оценка)"
        )

    lines.append("")
    lines.append(f"🔗 {escape(url)}")
    return _trim("\n".join(lines), limit)


class Telegram:
    """Thin Telegram client. Every send returns a bool — sent or not sent."""

    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient | None = None):
        self.token = token
        self.chat_id = chat_id
        self._client = client
        self._owned = client is None
        # Why the last send failed, so a caller can tell a chat that is gone
        # from one that was merely busy. Retrying the first one forever costs a
        # request and a place in the queue on every run, for a reader who left.
        self.last_error = ''

    # What Telegram says when a chat cannot be written to again, as opposed to
    # not right now. Matched loosely because the wording carries the bot's name
    # and has changed before.
    GONE = (
        "bot was blocked",
        "user is deactivated",
        "chat not found",
        "bot can't initiate conversation",
        "peer_id_invalid",
    )

    @property
    def chat_is_gone(self) -> bool:
        reason = self.last_error.lower()
        return any(phrase in reason for phrase in self.GONE)

    async def __aenter__(self) -> Telegram:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(
        self, method: str, payload: dict, files: dict | None = None
    ) -> tuple[bool, str]:
        """POST to the Bot API, obeying retry_after. Returns (ok, description).

        With `files` the payload goes as a multipart form, which is how a photo
        is uploaded rather than fetched by Telegram from an address.
        """
        assert self._client is not None, "use Telegram as an async context manager"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                url = API.format(token=self.token, method=method)
                if files:
                    resp = await self._client.post(url, data=payload, files=files)
                else:
                    resp = await self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    # The type, because some of these carry no message at all:
                    # "network error: " told nobody it was a timeout.
                    return False, f"network error: {type(exc).__name__}: {exc}"
                await asyncio.sleep(2**attempt)
                continue

            try:
                body = resp.json()
            except ValueError:
                return False, f"HTTP {resp.status_code}, non-JSON reply"

            if body.get("ok"):
                self.last_error = ""
                return True, "ok"

            description = str(body.get("description", "unknown error"))
            self.last_error = description
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                wait = float(body.get("parameters", {}).get("retry_after", 2**attempt))
                log.info("telegram rate limit, waiting %.0fs", wait)
                await asyncio.sleep(min(wait, 60))
                continue
            return False, description
        return False, "gave up after retries"

    async def send_text(self, text: str, disable_preview: bool = True) -> bool:
        ok, why = await self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": _trim(text, MESSAGE_LIMIT),
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            },
        )
        if not ok:
            log.error("sendMessage failed: %s", why)
        return ok

    async def send_deal(self, caption: str, image_url: str | None) -> bool:
        """Photo with caption, falling back to text if there is no usable image.

        Three tries at the picture before giving up on it: Telegram fetching it
        (from a CDN-resized address where there is one), then fetching it
        ourselves and uploading it — some image hosts turn Telegram's fetcher
        away and not a browser's — and only then the caption as plain text.
        """
        photo = telegram_photo(image_url)
        if photo:
            fields = {
                "chat_id": self.chat_id,
                "caption": _trim(caption, CAPTION_LIMIT),
                "parse_mode": "HTML",
            }
            ok, why = await self._call("sendPhoto", {**fields, "photo": photo})
            if ok:
                return True
            if self.chat_is_gone:
                return False
            # Uploaded ourselves only when Telegram said it could not fetch the
            # picture. After any other refusal — a bad caption, the network —
            # fetching it here would change nothing but the time it takes.
            could_not_fetch = any(phrase in why.lower() for phrase in self.UNFETCHABLE)
            picture = await self._download(photo) if could_not_fetch else None
            if picture is not None:
                ok, upload_why = await self._call(
                    "sendPhoto", fields, files={"photo": ("photo.jpg", picture)}
                )
                if ok:
                    return True
                why = f"{why}; upload: {upload_why}"
            # Nothing worked — the deal still deserves to be sent.
            log.warning("sendPhoto failed (%s), falling back to text", why)
        return await self.send_text(caption, disable_preview=False)

    # What Telegram says when it could not get the picture from the address.
    UNFETCHABLE = (
        "failed to get http url content",
        "wrong file identifier/http url specified",
        "wrong type of the web page content",
        "webpage_curl_failed",
        "webpage_media_empty",
    )

    async def _download(self, url: str) -> bytes | None:
        """The picture's bytes, or None if it is not a picture we can upload.

        The address was written by a shop, so it is fetched as something a
        stranger chose: only from a public address, with no redirects to follow
        somewhere else, and never more than Telegram would take — anything the
        collector can reach on this machine or this network would otherwise go
        to a reader's chat as a "picture".
        """
        assert self._client is not None
        if not await _is_public(url):
            log.info("not fetching a picture from a private address: %s", url)
            return None
        try:
            async with self._client.stream(
                "GET", url, follow_redirects=False, timeout=20
            ) as resp:
                kind = resp.headers.get("content-type", "")
                if resp.status_code != 200 or not kind.startswith("image/"):
                    return None
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > UPLOAD_LIMIT:
                        return None
                return bytes(body)
        except httpx.HTTPError as exc:
            log.info("could not fetch the picture myself either: %s", type(exc).__name__)
            return None
