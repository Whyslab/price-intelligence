"""How much one reader should care, as distinct from how good the discount is.

Two scales, deliberately kept apart. `pi.deals` answers how far below a fair
reference price something is — a fact about the product, the same for everybody,
and the thing a threshold is set on. This answers whether it is worth putting in
front of a particular person, which depends on their sizes, their brands and
what they wear.

Folding both into one number loses exactly the distinction that matters: −45% on
a brand you do not wear in a size you do not take and −28% on a Stone Island
jacket in yours both come out near 70, and the second one drowns. It would also
have to be recomputed for every reader the moment there is more than one, while
the quality of a discount is computed once.

The rules, and why they are what they are:

- **A size you do not take does not disqualify a find, it raises the bar.** The
  same shoe often comes back in stock, sizes get restocked, and a present is
  bought in somebody else's size. So the whole catalogue stays reachable and the
  bar simply moves.
- **A brand you named lowers the bar** and lifts the entry, but never becomes a
  filter. A hard list of brands fails precisely on what is not in it: a find in
  a brand you had not thought of would never arrive, and you would never learn
  that it did not.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db, landed
from .config import Filters

# Added to the discount's own score to order one reader's list.
# Following something by name outweighs every bonus here put together, and
# deliberately so: the rest are guesses about what somebody might want, and this
# one is what they said they wanted.
FOLLOWED_BONUS = 50
OWN_SIZE_BONUS = 25
FAVOURITE_BRAND_BONUS = 15
WANTED_KIND_BONUS = 10

# Moved on the threshold rather than applied as a filter. Twenty points is about
# the distance between a 30%-off and a 45%-off deal, so "somebody else's size"
# means roughly "only when it is properly cheap".
OTHER_SIZE_BAR = 20
FAVOURITE_BRAND_BAR = -10


@dataclass(frozen=True)
class Reader:
    """What one person wants, in the form the ranking needs."""

    sizes: frozenset[str] = frozenset()
    brands: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()
    genders: frozenset[str] = frozenset()

    @classmethod
    def from_profile(cls, row: sqlite3.Row | None) -> Reader:
        if row is None:
            return cls()

        def split(value: str | None) -> frozenset[str]:
            return frozenset(
                part.strip() for part in (value or "").split(",") if part.strip()
            )

        return cls(
            sizes=split(row["sizes"]),
            brands=frozenset(b.lower() for b in split(row["brands"])),
            kinds=split(row["kinds"]),
            genders=split(row["genders"]),
        )

    @classmethod
    def from_filters(cls, filters: Filters) -> Reader:
        """The reader described by filters.toml, for a database with no profile yet.

        Those keys are on their way out of that file — sizes and brands are facts
        about a person — but until the wizard has been run they are the only
        statement of what the one reader wants, and throwing that away on upgrade
        would silently widen every notification.
        """
        return cls(
            sizes=frozenset(filters.sizes),
            brands=frozenset(b.lower() for b in filters.brands_allow),
        )

    @property
    def has_opinions(self) -> bool:
        return bool(self.sizes or self.brands or self.kinds or self.genders)


def _matches(row: sqlite3.Row, reader: Reader) -> tuple[bool, bool, bool]:
    size = (row["size_norm"] or "").upper()
    family = (row["brand_family"] or row["brand_norm"] or "").lower()
    kind = row["kind"] or ""
    return (
        bool(reader.sizes) and size in reader.sizes,
        bool(reader.brands) and family in reader.brands,
        bool(reader.kinds) and kind in reader.kinds,
    )


def bar_for(row: sqlite3.Row, reader: Reader, base: int) -> int:
    """The score this find has to clear before this reader hears about it."""
    own_size, favourite, _ = _matches(row, reader)
    bar = base
    if reader.sizes and not own_size:
        bar += OTHER_SIZE_BAR
    if favourite:
        bar += FAVOURITE_BRAND_BAR
    return bar


def priority(
    deal,
    row: sqlite3.Row,
    reader: Reader,
    shipping: landed.Rules = landed.EMPTY,
    eur_usd: float | None = None,
) -> float:
    """Where this find sits in one reader's queue. Higher is sooner.

    Delivery takes points off but is capped and never disqualifies: the estimate
    is rough, and something this rough must be able to move a find down the list
    without being able to remove it.
    """
    own_size, favourite, wanted_kind = _matches(row, reader)
    value = float(deal.score)
    if own_size:
        value += OWN_SIZE_BONUS
    if favourite:
        value += FAVOURITE_BRAND_BONUS
    if wanted_kind:
        value += WANTED_KIND_BONUS
    if shipping.enabled:
        value -= landed.penalty(
            landed.landed_all(
                shipping, deal.price_usd, row["kind"], row["domain"],
                row["country"], eur_usd,
            )
        )
    return value


def ranker(
    reader: Reader,
    min_score: int,
    shipping: landed.Rules = landed.EMPTY,
    eur_usd: float | None = None,
    following: frozenset[int] | set[int] = frozenset(),
    filters=None,
):
    """A (deal, row) -> priority-or-None function for `find_deals` to sort by.

    None means below this reader's bar. The wanted gender is the one place that
    does filter rather than adjust: asking for women's things and being sent
    men's is not a near miss, it is the wrong answer.

    `filters` carries the rules about the find rather than about the reader —
    whether anyone but the seller says the price used to be higher, whether the
    brand and the type could be read at all. They are checked here because this
    is the notification path and only the notification path: the shelf keeps
    showing everything (see pipeline.shelf_config), and a reader who goes
    looking is a different act from being interrupted.

    `following` is the set of products this reader starred, and it passes
    everything — the bar, the gender, the find rules, the lot. Every other rule
    here is an inference from a profile about what somebody probably wants; a
    star is the person saying it. A shoe classified as men's, in a size they do
    not take, at 6% off, is still the shoe they asked to be told about — and so
    is one whose only evidence is the shop's own struck-through price.
    """

    def rank(deal, row: sqlite3.Row) -> float | None:
        if deal.product_id in following:
            return priority(deal, row, reader, shipping, eur_usd) + FOLLOWED_BONUS
        if filters is not None and not filters.worth_interrupting(
            deal.reference_source, deal.all_time_low,
            row["brand_family"] or row["brand_norm"], row["kind"], row["gender"],
        ):
            return None
        wants_women_only = "women" in reader.genders and "men" not in reader.genders
        if wants_women_only and row["gender"] != "women":
            return None
        if deal.score < bar_for(row, reader, min_score):
            return None
        return priority(deal, row, reader, shipping, eur_usd)

    return rank


def reader_for(conn: sqlite3.Connection, chat_id: str | None, filters: Filters) -> Reader:
    """Whose preferences the notifications follow, for one chat.

    The lookup is by chat rather than by user because that is what `.env` knows,
    and it is the same question every other reader asks with their own chat.
    """
    if chat_id:
        row = conn.execute(
            "SELECT * FROM bot_users WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        # Any preference at all counts, rather than a finished wizard. Setting
        # your sizes in /settings and having them ignored because you skipped
        # the wizard is not a distinction anybody would expect to matter — and
        # it silently left the notifications following the file instead.
        if row is not None and Reader.from_profile(row).has_opinions:
            return Reader.from_profile(row)
    return Reader.from_filters(filters)


@dataclass(frozen=True, slots=True)
class Subscriber:
    """One person the run writes to, and what they want.

    `user_id` is Telegram's, and it is what deduplication is keyed on. It is 0
    only for the owner in a database where they have never spoken to the bot —
    a fresh install writing to the chat id in `.env` and nothing else. That
    collides deliberately with the "everybody" rows described on alerts.user_id:
    before anyone has a profile there is exactly one reader, and treating what
    was sent to them as sent to everybody is what it meant at the time.
    """

    user_id: int
    chat_id: str
    reader: Reader
    label: str


def subscribers(
    conn: sqlite3.Connection, owner_chat_id: str | None, filters: Filters,
    subscription: bool = False,
) -> list[Subscriber]:
    """Everyone this run should write to, the owner first.

    Anyone who has spoken to the bot, not blocked it and is paying is a reader,
    whether or not they finished the wizard: somebody who pressed /start and
    skipped the questions is saying they want everything, not that they want
    nothing.

    Paying is what the feed is — **while the feed is being sold**. With
    `subscription` off nothing is, so everyone hears from here and the digest
    has nobody left to talk to. See Config.subscription and docs/subscription.md.

    A reader who has not subscribed hears from the digest once a day and not
    from here — the whole difference being sold is that this arrives when the
    price falls rather than at six in the evening.
    The grace period counts as paying, which is the entire point of having one:
    a failed renewal should cost the reader a reminder, not the product.

    The owner is always included, and never asked to pay. The chat id in `.env`
    is what a fresh install has instead of a subscriber list, a run that wrote
    to nobody would look exactly like a run that found nothing, and the person
    who owns the collector is not a customer of it.
    """
    out: list[Subscriber] = []
    seen: set[str] = set()
    owner = str(owner_chat_id) if owner_chat_id else None

    rows = conn.execute(
        "SELECT * FROM bot_users WHERE active = 1 ORDER BY created_at, id"
    ).fetchall()
    # The owner goes first so that a cap or a rate limit bites the newest
    # subscriber rather than the person who runs the thing.
    rows = sorted(rows, key=lambda r: str(r["chat_id"]) != owner)
    for row in rows:
        chat_id = str(row["chat_id"])
        if chat_id in seen:
            continue
        # Free readers are held back only while there is something to buy. With
        # selling switched off (Config.subscription) everyone is a reader, which
        # is the whole point of switching it off.
        if (
            subscription
            and chat_id != owner
            and db.subscription_state(conn, int(row["id"])) == "free"
        ):
            continue
        seen.add(chat_id)
        reader = Reader.from_profile(row)
        if not reader.has_opinions and chat_id == owner:
            # The owner's sizes may still only exist in filters.toml.
            reader = Reader.from_filters(filters)
        out.append(
            Subscriber(
                user_id=int(row["id"]),
                chat_id=chat_id,
                reader=reader,
                label=row["username"] or chat_id,
            )
        )

    if owner and owner not in seen:
        out.insert(
            0,
            Subscriber(
                user_id=0, chat_id=owner, reader=Reader.from_filters(filters),
                label=owner,
            ),
        )
    return out


def deactivate(conn: sqlite3.Connection, chat_id: str) -> None:
    """Stop writing to a chat Telegram has told us is closed to us."""
    conn.execute("UPDATE bot_users SET active = 0 WHERE chat_id = ?", (str(chat_id),))
