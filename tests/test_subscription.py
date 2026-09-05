"""Who is paying, who is in grace, and who has lapsed."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pi import db as dbm

from .conftest import ts


def _reader(conn, user_id: int = 777) -> int:
    dbm.upsert_bot_user(conn, user_id, chat_id=str(user_id), username="reader")
    return user_id


def _in(days: float) -> str:
    """Timestamp N days in the future, in the format the database stores."""
    return (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds")


def test_a_reader_who_never_paid_is_free(conn):
    user = _reader(conn)
    assert dbm.subscription_state(conn, user) == "free"
    assert dbm.is_subscribed(conn, user) is False


def test_an_unknown_reader_is_free(conn):
    assert dbm.subscription_state(conn, 12345) == "free"


def test_paying_opens_the_shelf(conn):
    user = _reader(conn)
    dbm.grant(conn, user, days=30, charge_id="chg_1", stars=150)
    assert dbm.subscription_state(conn, user) == "paid"
    assert dbm.is_subscribed(conn, user) is True
    row = dbm.get_bot_user(conn, user)
    assert row["plan"] == "paid"
    assert row["stars_paid"] == 150
    assert row["charge_id"] == "chg_1"
    assert row["plan_since"] is not None


def test_grace_keeps_the_feed_and_closes_the_shelf(conn):
    user = _reader(conn)
    dbm.grant(conn, user, days=30)
    conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = ?", (ts(1), user))
    assert dbm.subscription_state(conn, user) == "grace"
    # The distinction the grace period exists for: still fed, no longer sold to.
    assert dbm.is_subscribed(conn, user) is False


def test_grace_runs_out(conn):
    user = _reader(conn)
    dbm.grant(conn, user, days=30)
    conn.execute(
        "UPDATE bot_users SET paid_until = ? WHERE id = ?",
        (ts(dbm.SUBSCRIPTION_GRACE_DAYS + 1), user),
    )
    assert dbm.subscription_state(conn, user) == "free"


def test_paying_early_adds_to_what_is_left(conn):
    user = _reader(conn)
    dbm.grant(conn, user, days=30)
    first = dbm.get_bot_user(conn, user)["paid_until"]
    dbm.grant(conn, user, days=30)
    second = dbm.get_bot_user(conn, user)["paid_until"]
    gap = datetime.fromisoformat(second) - datetime.fromisoformat(first)
    # A second month bought before the first ran out is a second month, not a
    # month starting today with the remainder thrown away.
    assert timedelta(days=29, hours=23) < gap <= timedelta(days=30)


def test_paying_after_a_lapse_starts_from_today(conn):
    user = _reader(conn)
    dbm.grant(conn, user, days=30)
    conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = ?", (ts(90), user))
    dbm.grant(conn, user, days=30)
    paid_until = datetime.fromisoformat(dbm.get_bot_user(conn, user)["paid_until"])
    # The three months nobody paid for are not credited back.
    assert paid_until > datetime.now(UTC) + timedelta(days=29)


def test_stars_accumulate_across_renewals(conn):
    user = _reader(conn)
    dbm.grant(conn, user, days=30, stars=150)
    dbm.grant(conn, user, days=30, stars=150)
    assert dbm.get_bot_user(conn, user)["stars_paid"] == 300


def test_plan_since_survives_a_lapse(conn):
    user = _reader(conn)
    dbm.grant(conn, user, days=30)
    first_seen = dbm.get_bot_user(conn, user)["plan_since"]
    conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = ?", (ts(90), user))
    dbm.expire_due(conn)
    dbm.grant(conn, user, days=30)
    assert dbm.get_bot_user(conn, user)["plan_since"] == first_seen


def test_expire_due_only_touches_the_lapsed(conn):
    paying = _reader(conn, 1)
    lapsed = _reader(conn, 2)
    grace = _reader(conn, 3)
    dbm.grant(conn, paying, days=30)
    dbm.grant(conn, lapsed, days=30)
    dbm.grant(conn, grace, days=30)
    conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = ?", (ts(90), lapsed))
    conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = ?", (ts(1), grace))

    assert dbm.expire_due(conn) == 1
    assert dbm.get_bot_user(conn, paying)["plan"] == "paid"
    assert dbm.get_bot_user(conn, lapsed)["plan"] == "free"
    # Still inside the grace window: the feed is the whole point of not cutting.
    assert dbm.get_bot_user(conn, grace)["plan"] == "paid"


def test_expire_due_keeps_the_profile_and_the_starred(conn):
    user = _reader(conn)
    dbm.upsert_bot_user(conn, user, chat_id=str(user), sizes="EU44", brands="Nike")
    dbm.grant(conn, user, days=30)
    conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = ?", (ts(90), user))
    dbm.expire_due(conn)
    row = dbm.get_bot_user(conn, user)
    assert row["plan"] == "free"
    assert row["sizes"] == "EU44"
    assert row["brands"] == "Nike"


def test_expiring_soon_finds_the_ones_to_remind(conn):
    soon = _reader(conn, 1)
    later = _reader(conn, 2)
    already_lapsed = _reader(conn, 3)
    dbm.grant(conn, soon, days=30)
    dbm.grant(conn, later, days=30)
    dbm.grant(conn, already_lapsed, days=30)
    conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = ?", (_in(2), soon))
    conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = ?", (_in(20), later))
    conn.execute("UPDATE bot_users SET paid_until = ? WHERE id = ?", (ts(1), already_lapsed))

    due = [row["id"] for row in dbm.expiring_soon(conn, within_days=3)]
    # Somebody already in grace has had the reminder; sending it again is noise.
    assert due == [soon]


def test_the_migration_is_idempotent(conn):
    dbm.migrate(conn)
    dbm.migrate(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(bot_users)")}
    assert {"plan", "paid_until", "plan_since", "stars_paid", "charge_id"} <= columns
