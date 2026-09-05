"""The Telegram bot: what to show *this* person, out of what is on offer.

The division of labour that shapes this file: `filters.toml` decides what counts
as a discount, which is a fact about a product and the same for everybody, and
this decides what is worth putting in front of one reader. So nothing here
scores anything. It reads the `offers` table the collector maintains, filters it
by a profile kept in `bot_users`, and formats the result.

That is also why it can answer a button press at all. Asking the question from
scratch — the way `pi find` does — means scoring the whole database and takes a
minute or two.

Two surfaces, because browsing and reading are different jobs. Browsing is a
page of five offers, each its own photo with the price, the discount, the shop
and the size under it — clothes are chosen by looking at them, and a list of
text is the wrong shape for that. Reading is one card: the whole account of why
this is a deal, what sizes are left, and what it costs delivered. Notifications
use the card, since a notification is always one product.

A first version made the browsing surface a compact list without pictures, on
the argument that comparing wants numbers. Using it settled the question the
other way.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from html import escape
from typing import ClassVar

import httpx

from . import db as dbm
from . import landed
from .config import Config
from .fx import load_rates
from .notify import _money

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
# Five, not ten: every entry is now its own photo message, and Telegram wants
# roughly a second between messages to one chat. Ten would be eleven messages
# trickling in over ten seconds.
PAGE = 5
# Telegram's own limit on callback_data. Everything below is kept far inside it,
# which is why the payloads are terse rather than readable.
CALLBACK_LIMIT = 64

GENDER_LABEL = {"all": "все", "men": "мужское", "women": "женское"}
KIND_LABEL = {"shoes": "обувь", "clothing": "одежда", "accessories": "аксессуары"}


# --- profile ----------------------------------------------------------------

def _split(value: str | None) -> list[str] | None:
    """A stored filter as a list, or None for "no opinion".

    Empty and absent are deliberately different: absent means the question was
    skipped and everything is wanted, while empty would mean nothing is. The
    wizard never writes empty, and this keeps that distinction rather than
    quietly turning one into the other.
    """
    if value is None:
        return None
    items = [part.strip() for part in value.split(",") if part.strip()]
    return items or None


def profile_of(user: sqlite3.Row) -> dict[str, list[str] | None]:
    return {
        "genders": _split(user["genders"]),
        "kinds": _split(user["kinds"]),
        "sizes": _split(user["sizes"]),
        "brands": _split(user["brands"]),
    }


def describe_profile(user: sqlite3.Row) -> str:
    profile = profile_of(user)
    genders = profile["genders"]
    lines = [
        f"👤 Пол: {', '.join(GENDER_LABEL.get(g, g) for g in genders) if genders else 'все'}",
        f"👟 Тип: {', '.join(KIND_LABEL.get(k, k) for k in profile['kinds']) if profile['kinds'] else 'любой'}",
        f"📏 Размеры: {', '.join(profile['sizes']) if profile['sizes'] else 'любые'}",
        f"🏷 Марки: {', '.join(profile['brands']) if profile['brands'] else 'любые'}",
    ]
    return "\n".join(lines)


# --- formatting -------------------------------------------------------------

def _age(then: str | None, now: str) -> str:
    """How long ago, in words. Both arguments are ISO-8601 as the database stores."""
    if not then:
        return "давно"
    delta = datetime.fromisoformat(now) - datetime.fromisoformat(then)
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "только что"
    if hours < 24:
        return f"{hours:.0f} ч назад"
    return f"{hours / 24:.0f} дн назад"


def format_entry(row: sqlite3.Row, now: str) -> str:
    """One offer as the caption under its photo.

    Short on purpose. Clothes are chosen by looking, and a paragraph under every
    picture turns a page of them into a wall of text — the full account is on the
    card behind the button.
    """
    shop = escape(row["store_name"] or row["domain"])
    title = escape(row["title"])[:70]
    brand = escape(row["brand_norm"] or row["brand"] or "")
    size = escape(row["size_norm"] or row["size"] or "—")
    head = f"−{row['discount_pct']:.0f}% · <b>{_money(row['price_usd'])}</b>"
    if row["all_time_low"]:
        head += " · 📉 минимум"
    lines = [head]
    if brand:
        lines.append(f"<b>{brand}</b> · {title}")
    else:
        lines.append(title)
    lines.append(f"<i>{shop} · {size} · {_age(row['found_at'], now)}</i>")
    return "\n".join(lines)


def format_page_header(page: int, total: int) -> str:
    pages = max(1, -(-total // PAGE))
    return f"💰 <b>Скидки</b> · стр. {page + 1} из {pages} · всего {total:,}"


EMPTY_SHELF = (
    "Под эти условия сейчас ничего нет.\n\nПопробуйте ослабить фильтры — /settings."
)


def format_landed(items: list) -> list[str]:
    """What the thing costs delivered, per destination.

    Both destinations, always. The sum is often what decides where to have
    something sent, so putting one behind a button means switching back and
    forth on every offer — and there are only two lines.
    """
    if not items:
        return []
    lines = ["", "📦 <b>С доставкой</b> <i>(оценка)</i>"]
    for item in sorted(items, key=lambda i: i.total_usd):
        extra = item.total_usd - item.price_usd
        lines.append(
            f"   {item.name}: <b>{_money(item.total_usd)}</b>"
            f" <i>(+{_money(extra)})</i>"
        )
    return lines


def format_card(row: sqlite3.Row, sizes: list[tuple[str, bool]], now: str,
                landed: list | None = None) -> str:
    """One offer in full. The photo is sent with this as its caption."""
    shop = escape(row["store_name"] or row["domain"])
    country = f" ({row['country']})" if row["country"] else ""
    lines = [f"🔥 <b>−{row['discount_pct']:.0f}%</b> · экономия {_money(row['saving_usd'])}", ""]
    if row["brand_norm"]:
        lines.append(f"<b>{escape(row['brand_norm'])}</b>")
    lines.append(escape(row["title"]))
    lines.append("")
    lines.append(f"💰 <b>{_money(row['price_usd'])}</b> · было {_money(row['reference_usd'])}")
    if row["all_time_low"]:
        lines.append("📉 Минимум за всё время наблюдения")
    lines.append(f"🏪 {shop}{country}")

    in_stock = [size for size, ok in sizes if ok]
    gone = [size for size, ok in sizes if not ok]
    if in_stock:
        lines.append(f"📏 Есть: {', '.join(in_stock[:12])}")
    if gone:
        lines.append(f"   Нет: {', '.join(gone[:12])}")
    lines.append(f"🕐 Видим эту цену {_age(row['found_at'], now)}")
    lines.append(f"✅ Проверено {_age(row['checked_at'], now)}")
    lines.extend(format_landed(landed or []))
    return "\n".join(lines)


# --- subscription -----------------------------------------------------------

# What it costs, in Telegram Stars. A year is ten months' worth: the discount is
# the reason to commit, and a reader who commits is one the renewal cannot lose.
MONTHLY_STARS = 150
YEARLY_STARS = 1500


def subscription_pitch(conn: sqlite3.Connection) -> str:
    """Why this is worth paying for, in the terms a free channel cannot match.

    Honesty is invisible. "−28%, because that is the real thirty-day low" reads
    as a worse deal than "−70% 🔥" to somebody who has not yet been burned, so
    what is sold here is not the honesty but its three visible consequences.

    The numbers are counted from the shelf at the moment of asking rather than
    written into the text. A claim about the size of the catalogue is the one
    thing a reader can check in the next thirty seconds, and a stale one is
    worse than no number at all.
    """
    shelf = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    compared = conn.execute(
        "SELECT COUNT(*) FROM offers WHERE reference_source = 'market'"
    ).fetchone()[0]
    shops = conn.execute(
        "SELECT COUNT(*) FROM stores WHERE platform IN ('shopify', 'jsonld', 'asos')"
    ).fetchone()[0]
    # Thousands separated with a space, the way Russian writes them. Formatting
    # each number on its own rather than substituting over the finished text:
    # doing it to the whole string would eat the commas in the sentences too.
    def spaced(n: int) -> str:
        return f"{n:,}".replace(",", " ")

    return "\n".join([
        "💎 <b>Что даёт подписка</b>",
        "",
        f"Сейчас бесплатно вы видите 2 находки в день. На полке лежит "
        f"<b>{spaced(shelf)}</b> "
        f"{plural_word(shelf, 'предложение', 'предложения', 'предложений')} из "
        f"{shops} {plural_word(shops, 'магазина', 'магазинов', 'магазинов')}, "
        "и подписка открывает их все.",
        "",
        "<b>Что умеет только этот бот:</b>",
        "",
        f"1️⃣ <b>Дешевле, чем у соседей.</b> {spaced(compared)} "
        f"{plural_word(compared, 'предложение сравнено', 'предложения сравнены', 'предложений сравнены')} "
        "с ценой на ту же вещь в других магазинах по артикулу производителя. "
        "Канал со скидками пересылает ярлык — сравнить ему не с чем.",
        "",
        "2️⃣ <b>Цена подтверждена, а не найдена когда-то.</b> На каждой карточке "
        "написано, когда магазин в последний раз показал эту цену. Товар, "
        "который сняли с продажи, уходит с полки.",
        "",
        "3️⃣ <b>Цена на руках.</b> С доставкой и пошлиной в вашу страну, а не "
        "только та, что на ярлыке: −50% из Лос-Анджелеса и −50% из Берлина — "
        "это разные деньги.",
        "",
        "<b>Плюс:</b> поиск по артикулу и названию, лента под ваши размеры и "
        "марки, слежка за отмеченным — о нём напишут, даже если скидка не "
        "дотягивает до общих порогов.",
        "",
        f"⭐ <b>{MONTHLY_STARS} звёзд в месяц</b> или {YEARLY_STARS} за год "
        "(два месяца в подарок).",
    ])


def subscription_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": f"⭐ Месяц — {MONTHLY_STARS}", "callback_data": "buy:month"},
             {"text": f"⭐ Год — {YEARLY_STARS}", "callback_data": "buy:year"}],
            [{"text": "◀️ Назад", "callback_data": "menu"}],
        ]
    }


# --- keyboards --------------------------------------------------------------

BOT_LOOKUP_LIMIT = 8


def plural_word(n: int, one: str, few: str, many: str) -> str:
    """Which of the three Russian forms goes with this count, without the count.

    Separate from `plural` because a number is not always written the way
    `str(int)` writes it: thousands in these texts are spaced, and pasting the
    count in twice is exactly the bug this split prevents.
    """
    tail, unit = abs(n) % 100, abs(n) % 10
    if 10 < tail < 20:
        return many
    return one if unit == 1 else few if 1 < unit < 5 else many


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian counts, because "26 магазин(ов)" is a developer in the text."""
    return f"{n} {plural_word(n, one, few, many)}"


def format_lookup(found: dict) -> str:
    """The answer to "what does this cost", as a message.

    Two different answers wear the same shape and must not read alike. An
    article number is one thing priced by several merchants, and the cheapest
    of them is the answer. A name is a list of different things, and calling
    the cheapest of those a saving would be a lie the layout tells by itself.
    """
    if not found["shops"]:
        if found["too_common"]:
            return (
                f"«{escape(found['too_common'])}» стоит в поле артикула у слишком "
                "многих разных товаров — это слово, а не номер модели."
            )
        return (
            "Ничего не нашлось. Пришлите артикул с коробки "
            "(<code>CW2288-111</code>) или название — посмотрю, где дешевле.\n"
            "/deals — список скидок, /settings — настройки."
        )

    lines: list[str] = []
    if found["same_thing"]:
        lines.append(
            f"<b>{escape(str(found['key']))}</b> · "
            + plural(found["found"], "магазин", "магазина", "магазинов")
        )
        if found["found"] == 1:
            lines.append("<i>только один магазин — сравнить не с чем</i>")
    else:
        lines.append(
            "По названию нашлось "
            + plural(found["found"], "товар", "товара", "товаров")
        )
        lines.append("<i>это разные вещи, а не одна в разных магазинах</i>")
    lines.append("")

    for row in found["shops"][:BOT_LOOKUP_LIMIT]:
        shop = escape(row["store_name"] or row["domain"])
        country = f" · {escape(row['country'])}" if row["country"] else ""
        cut = f" · −{row['discount_pct']:.0f}%" if row["discount_pct"] else ""
        title = escape(row["title"])[:60]
        lines.append(
            f'<a href="{escape(row["url"])}">{_money(row["price_usd"])}</a>'
            f" — {shop}{country}{cut}"
        )
        if not found["same_thing"]:
            lines.append(f"   {title}")
    if found["found"] > BOT_LOOKUP_LIMIT:
        lines.append(f"\n<i>показано {BOT_LOOKUP_LIMIT} из {found['found']}</i>")
    return "\n".join(lines)


def entry_keyboard(row: sqlite3.Row, page: int) -> dict:
    """Under each photo: buy it, or see the whole account of why it is a deal."""
    return {
        "inline_keyboard": [[
            {"text": "🛒 В магазин", "url": row["url"]},
            {"text": "ℹ️ Подробно", "callback_data": f"o:{row['variant_id']}:{page}"},
        ]]
    }


def nav_keyboard(page: int, total: int) -> dict:
    """The last message of a page: where to go from here."""
    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Назад", "callback_data": f"p:{page - 1}"})
    nav.append({"text": "⚙️", "callback_data": "menu"})
    if (page + 1) * PAGE < total:
        nav.append({"text": "Дальше ➡️", "callback_data": f"p:{page + 1}"})
    return {"inline_keyboard": [nav]}


def card_keyboard(row: sqlite3.Row, page: int) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🛒 Открыть в магазине", "url": row["url"]}],
            [{"text": "⬅️ К списку", "callback_data": f"p:{page}"}],
        ]
    }


def shelf_button(web_url: str | None) -> list[dict] | None:
    """The button that opens the whole shelf, or nothing if there is nowhere to open.

    Telegram will only run a page inside itself over HTTPS, so an http:// address
    — which is what `pi web` serves until somebody puts it behind a tunnel —
    becomes an ordinary link instead. That still works when the reader is at the
    machine, and it is honest about being a link away rather than pretending to
    be part of the bot.
    """
    if not web_url:
        return None
    label = "🗂 Все скидки"
    if web_url.startswith("https://"):
        return [{"text": label, "web_app": {"url": web_url}}]
    return [{"text": label, "url": web_url}]


def menu_keyboard(user: sqlite3.Row, web_url: str | None = None,
                  subscribed: bool = False) -> dict:
    rows = [[{"text": "💰 Смотреть скидки", "callback_data": "p:0"}]]
    shelf = shelf_button(web_url)
    if shelf:
        rows.append(shelf)
    if not subscribed:
        # Offered rather than nagged: one button in the menu, and the reader
        # arrives at it having already seen what the bot finds.
        rows.append([{"text": "💎 Подписка", "callback_data": "pitch"}])
    rows += [
        [{"text": "👤 Пол", "callback_data": "ask:genders"},
         {"text": "👟 Тип", "callback_data": "ask:kinds"}],
        [{"text": "📏 Размеры", "callback_data": "ask:sizes"},
         {"text": "🏷 Марки", "callback_data": "ask:brands"}],
    ]
    return {"inline_keyboard": rows}


def gender_keyboard() -> dict:
    return {
        "inline_keyboard": [[
            {"text": "Все", "callback_data": "set:genders:"},
            {"text": "Мужское", "callback_data": "set:genders:men"},
            {"text": "Женское", "callback_data": "set:genders:women"},
        ]]
    }


def kinds_keyboard(current: list[str] | None) -> dict:
    """Toggles, because the answer is a set rather than a choice."""
    chosen = set(current or [])
    row = [
        {
            "text": ("✅ " if kind in chosen else "") + label,
            "callback_data": f"tog:{kind}",
        }
        for kind, label in KIND_LABEL.items()
    ]
    return {
        "inline_keyboard": [
            row,
            [{"text": "Любой", "callback_data": "set:kinds:"},
             {"text": "Готово", "callback_data": "menu"}],
        ]
    }


def skip_keyboard(step: str) -> dict:
    return {"inline_keyboard": [[{"text": "Пропустить", "callback_data": f"skip:{step}"}]]}


# --- the bot ----------------------------------------------------------------

class Bot:
    """Long polling, because the alternative needs a public address.

    A webhook would mean a host with a certificate — a VPS — and the whole point
    of the collector's design is that it runs on this laptop inside Shopify's
    per-IP quota without paying anyone. Polling costs one sleeping process.
    """

    def __init__(self, config: Config, conn: sqlite3.Connection):
        self.config = config
        self.conn = conn
        self.offset = 0
        self._client: httpx.AsyncClient | None = None
        self.shipping = landed.load_rules()
        # Only needed to turn Ukraine's €150 allowance into the dollars
        # everything else is in. Read from the same cache the sweep writes, so
        # the bot never fetches a rate of its own.
        rates = load_rates(config.db_path.parent / "fx_cache.json")
        self.eur_usd = rates.to_usd(1.0, "EUR")[0] if rates.to_usd(1.0, "EUR") else None

    # -- transport --

    async def _call(self, method: str, payload: dict) -> dict | None:
        assert self._client is not None
        try:
            resp = await self._client.post(
                API.format(token=self.config.bot_token, method=method), json=payload
            )
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("%s failed: %s", method, exc)
            return None
        if not body.get("ok"):
            log.warning("%s refused: %s", method, body.get("description"))
            return None
        return body.get("result")

    async def send(self, chat_id: str, text: str, keyboard: dict | None = None) -> None:
        payload = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        await self._call("sendMessage", payload)

    async def send_photo(
        self, chat_id: str, photo: str, caption: str, keyboard: dict | None = None
    ) -> bool:
        payload = {
            "chat_id": chat_id, "photo": photo, "caption": caption, "parse_mode": "HTML"
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        return await self._call("sendPhoto", payload) is not None

    async def edit(
        self, chat_id: str, message_id: int, text: str, keyboard: dict | None = None
    ) -> None:
        """Replace a message in place, which is what makes paging feel like paging."""
        payload = {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        await self._call("editMessageText", payload)

    # -- state --

    def _user(self, sender: dict, chat_id: str) -> sqlite3.Row:
        return dbm.upsert_bot_user(
            self.conn, sender["id"], chat_id, sender.get("username")
        )

    PROFILE_FIELDS = ("genders", "kinds", "sizes", "brands")

    def _save(self, user_id: int, **fields) -> sqlite3.Row:
        row = dbm.get_bot_user(self.conn, user_id)
        # Saying what you want *is* being set up. Keeping a separate "finished
        # the wizard" flag meant answering a question in /settings changed
        # nothing, because the notifications only consult a profile marked done.
        if any(field in self.PROFILE_FIELDS for field in fields):
            fields.setdefault("onboarded", 1)
        return dbm.upsert_bot_user(self.conn, user_id, row["chat_id"], row["username"], **fields)

    # -- screens --

    async def show_list(self, chat_id: str, user: sqlite3.Row, page: int, message_id: int | None):
        """A page of offers, each as its own photo.

        Clothes are chosen by looking at them, so every entry carries its
        picture. That costs one message per offer instead of one per page, which
        is why a page is five rather than ten: Telegram wants about a second
        between messages to the same chat, and eleven of them is a wall arriving
        slowly.

        Paging sends a new page rather than editing the old one — a photo
        message cannot be turned into a different photo — so the chat keeps what
        you have already looked at, which is what scrolling back expects.
        """
        rows, total = dbm.offers_for(
            self.conn, **profile_of(user), limit=PAGE, offset=page * PAGE
        )
        if not rows:
            await self.send(chat_id, EMPTY_SHELF)
            return

        now = dbm.utcnow()
        await self.send(chat_id, format_page_header(page, total))
        for row in rows:
            caption = format_entry(row, now)
            keyboard = entry_keyboard(row, page)
            sent = False
            if row["image_url"]:
                sent = await self.send_photo(chat_id, row["image_url"], caption, keyboard)
            if not sent:
                # No picture, or Telegram could not fetch it. The offer is still
                # worth showing — it just shows as text.
                await self.send(chat_id, caption, keyboard)
            await asyncio.sleep(0.6)
        await self.send(
            chat_id, f"стр. {page + 1} · показано {len(rows)} из {total:,}",
            nav_keyboard(page, total),
        )

    async def show_card(self, chat_id: str, variant_id: int, page: int):
        row = self.conn.execute(
            """
            SELECT o.*, v.size_norm, v.size, p.title, p.url, p.image_url, p.brand,
                   p.brand_norm, p.kind, s.domain, s.name AS store_name, s.country
              FROM offers o
              JOIN variants v ON v.id = o.variant_id
              JOIN products p ON p.id = o.product_id
              JOIN stores s   ON s.id = p.store_id
             WHERE o.variant_id = ?
            """,
            (variant_id,),
        ).fetchone()
        if row is None:
            # It was on the shelf when the list was drawn and is not now, which
            # is the honest answer rather than an error.
            await self.send(chat_id, "Это предложение уже закончилось.")
            return
        now = dbm.utcnow()
        delivered = landed.landed_all(
            self.shipping, row["price_usd"], row["kind"], row["domain"],
            row["country"], self.eur_usd,
        )
        caption = format_card(
            row, dbm.sizes_in_stock(self.conn, row["product_id"]), now, delivered
        )
        keyboard = card_keyboard(row, page)
        if row["image_url"] and await self.send_photo(
            chat_id, row["image_url"], caption, keyboard
        ):
            return
        await self.send(chat_id, caption, keyboard)

    async def show_menu(self, chat_id: str, user: sqlite3.Row):
        await self.send(
            chat_id,
            "⚙️ <b>Что показывать</b>\n\n" + describe_profile(user),
            menu_keyboard(
                user, self.config.web_url,
                subscribed=dbm.subscription_state(self.conn, user["id"]) != "free",
            ),
        )

    # -- the wizard --

    WIZARD = ("genders", "kinds", "sizes", "brands")
    ASK: ClassVar[dict[str, str]] = {
        "genders": "👤 <b>Чьи вещи показывать?</b>\n\nМожно поменять в любой момент.",
        "kinds": "👟 <b>Что интересует?</b>\n\nОтметьте нужное и нажмите «Готово».",
        "sizes": (
            "📏 <b>Ваши размеры</b>\n\nНапишите через запятую, например:\n"
            "<code>EU44, EU44.5, US10.5, L, XL</code>\n\n"
            "Размер не отсекает находки — он поднимает их в списке."
        ),
        "brands": (
            "🏷 <b>Любимые марки</b>\n\nЧерез запятую, например:\n"
            "<code>Nike, New Balance, Stone Island</code>\n\n"
            "Это приоритет, а не фильтр: находка по другой марке всё равно придёт."
        ),
    }

    async def ask(self, chat_id: str, user: sqlite3.Row, step: str, wizard: bool = False):
        self._save(user["id"], wizard_step=step if wizard else None)
        text = self.ASK[step]
        if step == "genders":
            await self.send(chat_id, text, gender_keyboard())
        elif step == "kinds":
            await self.send(chat_id, text, kinds_keyboard(profile_of(user)["kinds"]))
        else:
            await self.send(chat_id, text, skip_keyboard(step) if wizard else None)

    async def next_step(self, chat_id: str, user: sqlite3.Row, done: str):
        """Move the wizard on, or finish it. Only runs while a wizard is active."""
        if not user["wizard_step"]:
            return
        index = self.WIZARD.index(done)
        if index + 1 < len(self.WIZARD):
            await self.ask(chat_id, user, self.WIZARD[index + 1], wizard=True)
            return
        user = self._save(user["id"], wizard_step=None, onboarded=1)
        await self.send(chat_id, "Готово.\n\n" + describe_profile(user))
        await self.show_list(chat_id, user, page=0, message_id=None)

    # -- routing --

    async def on_message(self, message: dict):
        chat_id = str(message["chat"]["id"])
        sender = message.get("from") or {}
        if not sender:
            return
        user = self._user(sender, chat_id)
        text = (message.get("text") or "").strip()

        if text.startswith("/start"):
            await self.send(
                chat_id,
                "Привет. Я слежу за ценами в магазинах кроссовок и одежды и "
                "показываю то, что действительно подешевело.\n\n"
                "Можно настроить подборку под себя — четыре вопроса, — "
                "или сразу посмотреть всё.",
                {"inline_keyboard": [
                    [{"text": "Настроить", "callback_data": "wizard"},
                     {"text": "Показать всё", "callback_data": "p:0"}],
                    *([shelf] if (shelf := shelf_button(self.config.web_url)) else []),
                ]},
            )
            return
        if text.startswith(("/settings", "/menu")):
            await self.show_menu(chat_id, user)
            return
        if text.startswith(("/deals", "/list")):
            await self.show_list(chat_id, user, page=0, message_id=None)
            return

        step = user["wizard_step"]
        if step in ("sizes", "brands"):
            value = self._clean(step, text)
            user = self._save(user["id"], **{step: value})
            await self.send(chat_id, f"Записал: {value or 'любые'}")
            await self.next_step(chat_id, user, step)
            return
        # Anything else is a thing somebody wants priced. This is the one
        # question the shelf could never answer: it starts from a product id
        # and the page only has ids for what is already discounted.
        found = dbm.lookup_article(self.conn, text, limit=BOT_LOOKUP_LIMIT)
        await self.send(chat_id, format_lookup(found))

    @staticmethod
    def _clean(step: str, text: str) -> str | None:
        """Turn what somebody typed into what the database filters on."""
        from .sources.base import normalize_size

        parts = [part.strip() for part in text.split(",") if part.strip()]
        if not parts:
            return None
        if step == "sizes":
            # Normalised the same way the catalogue is, or "EU 44" would never
            # match the stored "EU44" and the filter would silently find nothing.
            return ",".join(sorted({normalize_size(part) or part.upper() for part in parts}))
        return ",".join(sorted({part.lower() for part in parts}))

    async def on_callback(self, query: dict):
        data = query.get("data") or ""
        message = query.get("message") or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        message_id = message.get("id") or message.get("message_id")
        sender = query.get("from") or {}
        if not chat_id or not sender:
            return
        user = self._user(sender, chat_id)
        # Always answer, or the button spins on the sender's phone until it times out.
        await self._call("answerCallbackQuery", {"callback_query_id": query["id"]})

        if data == "wizard":
            await self.ask(chat_id, user, "genders", wizard=True)
        elif data == "pitch":
            await self.send(
                chat_id, subscription_pitch(self.conn), subscription_keyboard()
            )
        elif data == "menu":
            await self.show_menu(chat_id, user)
        elif data.startswith("p:"):
            await self.show_list(chat_id, user, int(data[2:]), None)
        elif data.startswith("o:"):
            _, variant_id, page = data.split(":")
            await self.show_card(chat_id, int(variant_id), int(page))
        elif data.startswith("ask:"):
            await self.ask(chat_id, user, data[4:])
        elif data.startswith("skip:"):
            await self.next_step(chat_id, user, data[5:])
        elif data.startswith("set:"):
            _, field, value = data.split(":", 2)
            user = self._save(user["id"], **{field: value or None})
            await self.next_step(chat_id, user, field)
            if not user["wizard_step"]:
                await self.show_menu(chat_id, user)
        elif data.startswith("tog:"):
            kind = data[4:]
            chosen = set(profile_of(user)["kinds"] or [])
            chosen ^= {kind}
            user = self._save(user["id"], kinds=",".join(sorted(chosen)) or None)
            await self._call("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": kinds_keyboard(sorted(chosen)),
            })

    async def handle(self, update: dict):
        try:
            if "message" in update:
                await self.on_message(update["message"])
            elif "callback_query" in update:
                await self.on_callback(update["callback_query"])
        except Exception:
            # One malformed update must not take the bot down: it would stay
            # down until somebody noticed, and nobody watches a bot that works.
            log.exception("update %s failed", update.get("update_id"))

    async def poll(self) -> None:
        async with httpx.AsyncClient(timeout=70) as client:
            self._client = client
            me = await self._call("getMe", {})
            log.info("bot @%s is listening", (me or {}).get("username", "?"))
            while True:
                updates = await self._call(
                    "getUpdates",
                    {"offset": self.offset, "timeout": 50,
                     "allowed_updates": ["message", "callback_query"]},
                )
                if updates is None:
                    await asyncio.sleep(5)  # network trouble; do not spin
                    continue
                for update in updates:
                    self.offset = update["update_id"] + 1
                    await self.handle(update)


async def serve(config: Config, conn: sqlite3.Connection) -> int:
    if not config.bot_token:
        log.error("TELEGRAM_BOT_TOKEN is not set")
        return 1
    await Bot(config, conn).poll()
    return 0
