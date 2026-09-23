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

Both are facts about a product, so they are found once per run; who hears is
decided per reader, the same split the rest of the pipeline keeps.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape

from .domains import shop_link
from .notify import _money

# A size that flickers in and out of stock would otherwise announce itself
# every hour. One word a day about the same product is plenty.
RESTOCK_QUIET_HOURS = 24


@dataclass(frozen=True)
class Notice:
    user_id: int
    chat_id: str
    product_id: int
    kind: str            # "restock" | "gone"
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
    for row in rows:
        reader = by_user.get(row["user_id"])
        if reader is None or not row["in_stock"]:
            continue
        if row["restock_notified_at"] and row["restock_notified_at"] > quiet_since:
            continue
        if row["followed"] is not None:
            if row["variant_id"] != row["followed"]:
                continue
        elif reader.reader.sizes and (row["size_norm"] or "") not in reader.reader.sizes:
            continue
        wanted.setdefault((row["user_id"], row["product_id"]), []).append(row)

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


def gone_notices(conn: sqlite3.Connection, readers: list, grace_days: int) -> list[Notice]:
    """One notice per reader for each followed product the shop took down.

    Compared with the product's own mark, so a product that disappears, comes
    back and disappears again is reported each time, and never twice for the
    same disappearance.
    """
    if not readers:
        return []
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
           AND (f.gone_notified_at IS NULL OR f.gone_notified_at < p.missing_since)
         ORDER BY f.user_id, f.product_id
        """
    ).fetchall()
    notices = []
    for row in rows:
        reader = by_user.get(row["user_id"])
        if reader is None:
            continue
        lines = ["🪦 <b>Снято с продажи</b>", "", *_name(row), f"🏪 {_where(row)}", ""]
        lines.append(
            "Магазин больше не показывает эту вещь. Если она вернётся в ближайшие "
            f"{grace_days} дн, я напишу; если нет — она уйдёт из избранного."
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


def mark_told(conn: sqlite3.Connection, notice: Notice, ts: str) -> None:
    """Write down that this reader heard it, so the next run does not repeat it."""
    column = "restock_notified_at" if notice.kind == "restock" else "gone_notified_at"
    conn.execute(
        f"UPDATE favorites SET {column} = ? WHERE user_id = ? AND product_id = ?",
        (ts, notice.user_id, notice.product_id),
    )
