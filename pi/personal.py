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

from . import landed
from .config import Filters

# Added to the discount's own score to order one reader's list.
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
):
    """A (deal, row) -> priority-or-None function for `find_deals` to sort by.

    None means below this reader's bar. The wanted gender is the one place that
    does filter rather than adjust: asking for women's things and being sent
    men's is not a near miss, it is the wrong answer.
    """

    def rank(deal, row: sqlite3.Row) -> float | None:
        wants_women_only = "women" in reader.genders and "men" not in reader.genders
        if wants_women_only and row["gender"] != "women":
            return None
        if deal.score < bar_for(row, reader, min_score):
            return None
        return priority(deal, row, reader, shipping, eur_usd)

    return rank


def reader_for(conn: sqlite3.Connection, chat_id: str | None, filters: Filters) -> Reader:
    """Whose preferences the notifications follow.

    One reader today: the chat the collector was configured to write to. The
    lookup is by chat rather than by user because that is what `.env` knows, and
    it is the same question a second reader would ask with their own chat.
    """
    if chat_id:
        row = conn.execute(
            "SELECT * FROM bot_users WHERE chat_id = ? AND onboarded = 1", (str(chat_id),)
        ).fetchone()
        if row is not None:
            return Reader.from_profile(row)
    return Reader.from_filters(filters)
