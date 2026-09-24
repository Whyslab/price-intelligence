"""What somebody following a product hears besides a price drop.

A star says "tell me about this one", and a price is only one of the things
worth telling. Two events never show up as a price at all:

* **A size coming back.** The commonest reason to follow something is that it
  is sold out in the size you take. A restock at the old price is not a deal,
  so the discount side of the pipeline never said a word about it — and it is
  the moment the person was waiting for.
* **The shop taking it down.** A followed product that disappears simply
  stopped producing news, which from the outside looks exactly like a price
  that did not move. Two weeks later `pi prune` deletes it, and the star goes
  with it without anybody having been told.
* **It coming back after that.** The notice about it going promises to say so,
  and nothing else would: a product back at its old price and stock is no
  price news at all.

Both are facts about a product, so they are found once per run; who hears is
decided per reader, the same split the rest of the pipeline keeps.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape

from .domains import shop_link
from .notify import _money

# A size that flickers in and out of stock would otherwise announce itself
# every hour. One word a day about the same product is plenty.
RESTOCK_QUIET_HOURS = 24

# How long a product has to stay unlisted before a follower hears it went.
# A read can miss a live product — a page shifting under the walk skips one at
# the boundary — and it is back at the next read: told at once, that is
# "снято" and "снова в продаже" for nothing, twice for every flicker (review
# 24.09). Two days rather than one because a quiet shop is read about once a
# day, and a read that slips a little past its day must not decide it. The
# product is kept for a fortnight either way.
GONE_NOTICE_AFTER_HOURS = 48


@dataclass(frozen=True)
class Notice:
    user_id: int
    chat_id: str
    product_id: int
    kind: str            # "restock" | "gone" | "back"
    text: str
    image_url: str | None = None


def _where(row: sqlite3.Row) -> str:
    shop = escape((row["store_name"] or row["domain"] or "").strip())
    return f"{shop} ({escape(row['country'])})" if row["country"] else shop


def _name(row: sqlite3.Row) -> list[str]:
    brand = row["brand_family"] or row["brand"]
    lines = [f"<b>{escape(brand)}</b>"] if brand else []
    lines.append(escape(row["title"]))
    return lines


def _restock_text(first: sqlite3.Row, sizes: list[str]) -> str:
    lines = ["🔔 <b>Снова в наличии</b>", "", *_name(first)]
    if sizes:
        lines.append("Размер: " + ", ".join(escape(size) for size in sizes))
    price = _money(first["price_usd"])
    native = (
        f" · в магазине {_money(first['price_native'], first['currency'])}"
        if first["currency"] and first["currency"].upper() != "USD" else ""
    )
    lines += [f"💰 <b>{price}</b>{native}", f"🏪 {_where(first)}", "⭐ Вы следите за этой вещью"]
    link = shop_link(first["url"], first["domain"])
    if link:
        lines += ["", f"🔗 {escape(link)}"]
    return "\n".join(lines)


_SHOE_SIZE = re.compile(r"^(EU|US|UK)\d")
_LETTER_SIZE = re.compile(r"^\d?X*[SML]$")


def _size_family(size: str, kind: str | None = "shoes") -> str | None:
    """Shoe sizes (EU44, US10.5), letter sizes (S, XL, 2XL), or neither.

    A bare number is normalised to EU between 35 and 50 and to US otherwise,
    so a trouser's waist 32 arrives as US32 and its 36 as EU36. On clothing
    and accessories no number is a shoe size; on a product not classified at
    all, an EU size is the likelier shoe.
    """
    if _LETTER_SIZE.match(size):
        return "letter"
    if _SHOE_SIZE.match(size) and (
        kind == "shoes" or (kind is None and size.startswith("EU"))
    ):
        return "shoe"
    return None


def _sizes_speak(conn: sqlite3.Connection, product_id: int, sizes: frozenset[str]) -> bool:
    """Whether a reader's sizes say anything about this product at all.

    Somebody with only shoe sizes following a hoodie has none of its sizes,
    and read literally, nothing about it would ever be in their size: its S
    back in stock was never told, and it coming back on sale was "нет в
    наличии" while S and M were there (review 24.09). Their sizes are about
    another kind of thing. But EU46 on a shoe made in EU40–43 is about this
    very thing — it is not made in their size — and says so by staying quiet.
    So sizes speak when one of them is the product's, or of the same family.
    """
    if not sizes:
        return False
    listed = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT size_norm FROM variants WHERE product_id = ? AND size_norm IS NOT NULL",
            (product_id,),
        )
    }
    if listed & sizes:
        return True
    kind = conn.execute("SELECT kind FROM products WHERE id = ?", (product_id,)).fetchone()
    kind = kind[0] if kind else None
    # A reader's own sizes are what they wear: US10.5 on a reader is a shoe.
    families = {_size_family(size) for size in sizes} - {None}
    return bool(families & ({_size_family(size, kind) for size in listed} - {None}))


def restock_notices(
    conn: sqlite3.Connection,
    restocked: list[int],
    readers: list,
    now: datetime | None = None,
) -> list[Notice]:
    """One notice per reader and product whose followed size came back.

    A star made on one size's card follows that size. A star on the product
    follows the reader's own sizes, if they gave any — a shoe back in a size
    nobody here takes is not what they were waiting for — and any size if not.
    """
    if not restocked or not readers:
        return []
    now = now or datetime.now(UTC)
    quiet_since = (now - timedelta(hours=RESTOCK_QUIET_HOURS)).isoformat(timespec="seconds")
    by_user = {reader.user_id: reader for reader in readers}
    conn.execute("DROP TABLE IF EXISTS temp.pi_restocked")
    conn.execute("CREATE TEMP TABLE pi_restocked (id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO pi_restocked (id) VALUES (?)", ((v,) for v in restocked)
    )
    try:
        rows = conn.execute(
            """
            SELECT f.user_id, f.product_id, f.variant_id AS followed,
                   f.restock_notified_at,
                   v.id AS variant_id, v.size, v.size_norm,
                   p.title, p.brand, p.brand_family, p.url, p.image_url,
                   s.domain, s.name AS store_name, s.country,
                   pp.price_usd, pp.price_native, pp.currency, pp.in_stock
              FROM pi_restocked r
              JOIN variants  v ON v.id = r.id
              JOIN favorites f ON f.product_id = v.product_id AND f.notify = 1
              JOIN products  p ON p.id = v.product_id
              JOIN stores    s ON s.id = p.store_id
              JOIN price_points pp ON pp.variant_id = v.id
                   AND pp.ts = (SELECT MAX(ts) FROM price_points WHERE variant_id = v.id)
             WHERE p.missing_since IS NULL
             ORDER BY f.user_id, f.product_id, pp.price_usd
            """
        ).fetchall()
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.pi_restocked")

    wanted: dict[tuple[int, int], list[sqlite3.Row]] = {}
    speaks: dict[tuple[int, int], bool] = {}
    for row in rows:
        reader = by_user.get(row["user_id"])
        if reader is None or not row["in_stock"]:
            continue
        if row["restock_notified_at"] and row["restock_notified_at"] > quiet_since:
            continue
        key = (row["user_id"], row["product_id"])
        if row["followed"] is not None:
            if row["variant_id"] != row["followed"]:
                continue
        else:
            if key not in speaks:
                speaks[key] = _sizes_speak(conn, row["product_id"], reader.reader.sizes)
            if speaks[key] and (row["size_norm"] or "") not in reader.reader.sizes:
                continue
        wanted.setdefault(key, []).append(row)

    notices = []
    for (user_id, product_id), found in wanted.items():
        labels = (row["size_norm"] or row["size"] for row in found)
        sizes = list(dict.fromkeys(label for label in labels if label))
        notices.append(
            Notice(
                user_id=user_id,
                chat_id=by_user[user_id].chat_id,
                product_id=product_id,
                kind="restock",
                text=_restock_text(found[0], sizes),
                image_url=found[0]["image_url"],
            )
        )
    return notices


def gone_notices(
    conn: sqlite3.Connection, readers: list, grace_days: int, now: datetime | None = None
) -> list[Notice]:
    """One notice per reader for each followed product the shop took down.

    Compared with the product's own mark, so a product that disappears, comes
    back and disappears again is reported each time, and never twice for the
    same disappearance.
    """
    if not readers:
        return []
    now = now or datetime.now(UTC)
    settled = (now - timedelta(hours=GONE_NOTICE_AFTER_HOURS)).isoformat(timespec="seconds")
    by_user = {reader.user_id: reader for reader in readers}
    rows = conn.execute(
        """
        SELECT f.user_id, f.product_id, f.gone_notified_at, p.missing_since,
               p.title, p.brand, p.brand_family, p.url, p.image_url,
               s.domain, s.name AS store_name, s.country
          FROM favorites f
          JOIN products p ON p.id = f.product_id
          JOIN stores   s ON s.id = p.store_id
         WHERE f.notify = 1
           AND p.missing_since IS NOT NULL
           AND p.missing_since <= ?
           AND (f.gone_notified_at IS NULL OR f.gone_notified_at < p.missing_since)
         ORDER BY f.user_id, f.product_id
        """,
        (settled,),
    ).fetchall()
    notices = []
    for row in rows:
        reader = by_user.get(row["user_id"])
        if reader is None:
            continue
        lines = ["🪦 <b>Снято с продажи</b>", "", *_name(row), f"🏪 {_where(row)}", ""]
        lines.append(
            "Магазин больше не показывает эту вещь. Если она вернётся в ближайшие "
            f"{_days_left(row['missing_since'], grace_days, now)} дн, я напишу; "
            "если нет — она уйдёт из избранного."
        )
        link = shop_link(row["url"], row["domain"])
        if link:
            lines += ["", f"🔗 {escape(link)}"]
        notices.append(
            Notice(
                user_id=row["user_id"],
                chat_id=reader.chat_id,
                product_id=row["product_id"],
                kind="gone",
                text="\n".join(lines),
            )
        )
    return notices


def _days_left(missing_since: str, grace_days: int, now: datetime) -> int:
    """Days until `pi prune` may delete a product marked at `missing_since`.

    A mark made long before anybody could be told — this notice is new, the
    marks are not — has less of the fortnight left than the whole of it.
    """
    try:
        marked = datetime.fromisoformat(missing_since)
    except (TypeError, ValueError):
        return grace_days
    if marked.tzinfo is None:
        marked = marked.replace(tzinfo=UTC)
    return max(1, grace_days - (now - marked).days)


def returned_notices(conn: sqlite3.Connection, readers: list) -> list[Notice]:
    """One notice per reader for a followed product they were told had gone and
    that is back on sale.

    Keyed on the gone notice itself: it was sent, and the product is no longer
    marked. Hearing that it is back clears it, so the next disappearance is
    news again.
    """
    if not readers:
        return []
    by_user = {reader.user_id: reader for reader in readers}
    rows = conn.execute(
        """
        SELECT f.user_id, f.product_id, f.variant_id AS followed,
               p.title, p.brand, p.brand_family, p.url, p.image_url,
               s.domain, s.name AS store_name, s.country
          FROM favorites f
          JOIN products p ON p.id = f.product_id
          JOIN stores   s ON s.id = p.store_id
         WHERE f.notify = 1
           AND f.gone_notified_at IS NOT NULL
           AND p.missing_since IS NULL
         ORDER BY f.user_id, f.product_id
        """
    ).fetchall()
    notices = []
    for row in rows:
        reader = by_user.get(row["user_id"])
        if reader is None:
            continue
        sizes = conn.execute(
            """
            SELECT v.id, v.size, v.size_norm,
                   pp.price_usd, pp.price_native, pp.currency, pp.in_stock
              FROM variants v
              JOIN price_points pp ON pp.variant_id = v.id
                   AND pp.ts = (SELECT MAX(ts) FROM price_points WHERE variant_id = v.id)
             WHERE v.product_id = ?
             ORDER BY pp.price_usd
            """,
            (row["product_id"],),
        ).fetchall()
        # The sizes this reader waits for: the one starred, or their own, or any.
        own = _sizes_speak(conn, row["product_id"], reader.reader.sizes)
        if row["followed"] is not None:
            theirs = [size for size in sizes if size["id"] == row["followed"]]
        elif own:
            theirs = [size for size in sizes if (size["size_norm"] or "") in reader.reader.sizes]
        else:
            theirs = list(sizes)
        on_sale = [size for size in theirs if size["in_stock"]]
        if on_sale:
            lines = ["🔔 <b>Снова в продаже</b>", "", *_name(row)]
            labels = [size["size_norm"] or size["size"] for size in on_sale]
            named = list(dict.fromkeys(label for label in labels if label))
            if named and (row["followed"] is not None or own):
                lines.append("Размер: " + ", ".join(escape(label) for label in named))
            cheapest = on_sale[0]
            native = (
                f" · в магазине {_money(cheapest['price_native'], cheapest['currency'])}"
                if cheapest["currency"] and cheapest["currency"].upper() != "USD" else ""
            )
            lines.append(f"💰 <b>{_money(cheapest['price_usd'])}</b>{native}")
        else:
            # Back in the catalogue, but not in anything they can buy: worth
            # saying — it is no longer about to be deleted — without calling
            # it on sale at the price of a size they do not take.
            lines = ["🔔 <b>Снова в каталоге</b>", "", *_name(row)]
            waiting = [size["size_norm"] or size["size"] for size in theirs]
            waiting = list(dict.fromkeys(label for label in waiting if label))
            if waiting and (row["followed"] is not None or own):
                lines.append(
                    "Ваш размер пока распродан: " + ", ".join(escape(label) for label in waiting)
                )
            else:
                lines.append("Пока нет в наличии — напишу, когда появится.")
        lines += [
            f"🏪 {_where(row)}",
            "⭐ Магазин снова её показывает — она остаётся в избранном",
        ]
        link = shop_link(row["url"], row["domain"])
        if link:
            lines += ["", f"🔗 {escape(link)}"]
        notices.append(
            Notice(
                user_id=row["user_id"],
                chat_id=reader.chat_id,
                product_id=row["product_id"],
                kind="back",
                text="\n".join(lines),
                image_url=row["image_url"],
            )
        )
    return notices


def mark_told(conn: sqlite3.Connection, notice: Notice, ts: str) -> None:
    """Write down that this reader heard it, so the next run does not repeat it."""
    if notice.kind == "back":
        # Told it is back, so the gone notice is spent: should it disappear
        # again, that is news again.
        conn.execute(
            "UPDATE favorites SET gone_notified_at = NULL WHERE user_id = ? AND product_id = ?",
            (notice.user_id, notice.product_id),
        )
        return
    column = "restock_notified_at" if notice.kind == "restock" else "gone_notified_at"
    conn.execute(
        f"UPDATE favorites SET {column} = ? WHERE user_id = ? AND product_id = ?",
        (ts, notice.user_id, notice.product_id),
    )
