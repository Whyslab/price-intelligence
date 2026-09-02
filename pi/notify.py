"""Telegram delivery.

Deals go out as sendPhoto with an HTML caption, because a sneaker alert without
a picture of the sneaker is close to useless. Telegram caps a caption at 1024
characters (a plain message at 4096), so the caption is built to fit and trimmed
at a line boundary rather than mid-word. If a product has no image, or Telegram
refuses to fetch the one it has, the same text is sent as a normal message.
"""
from __future__ import annotations

import asyncio
import logging
from html import escape

import httpx

from .deals import Deal

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024
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
        lines.append("⭐ Из вашего списка отслеживания")
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

    async def _call(self, method: str, payload: dict) -> tuple[bool, str]:
        """POST to the Bot API, obeying retry_after. Returns (ok, description)."""
        assert self._client is not None, "use Telegram as an async context manager"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await self._client.post(
                    API.format(token=self.token, method=method), json=payload
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    return False, f"network error: {exc}"
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
        """Photo with caption, falling back to text if there is no usable image."""
        if image_url:
            ok, why = await self._call(
                "sendPhoto",
                {
                    "chat_id": self.chat_id,
                    "photo": image_url,
                    "caption": _trim(caption, CAPTION_LIMIT),
                    "parse_mode": "HTML",
                },
            )
            if ok:
                return True
            # Telegram could not fetch the image — the deal still deserves to be sent.
            log.warning("sendPhoto failed (%s), falling back to text", why)
        return await self.send_text(caption, disable_preview=False)
