"""The discount analysis recovers the behaviours the demo database plants.

The demo database is synthetic: four kinds of shop, each writing its struck-through
prices in a different way. If the analysis cannot tell them apart on data where the
answer is known, its numbers on real data mean nothing.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    work = tmp_path_factory.mktemp("demo")
    demo = _load("make_demo_db", "scripts/make_demo_db.py")
    discounts = _load("discounts", "analysis/discounts.py")
    db = work / "demo.db"
    demo.build(db)
    summary = discounts.run(db, work / "out", with_charts=False)
    rows = {}
    for line in (work / "out" / "stores.csv").read_text().splitlines()[1:]:
        cells = line.split(",")
        rows[cells[0]] = cells[-1]
    return summary, rows, work


def test_demo_build_is_deterministic(tmp_path):
    demo = _load("make_demo_db", "scripts/make_demo_db.py")
    import sqlite3

    def fingerprint(path):
        demo.build(path, seed=3)
        conn = sqlite3.connect(path)
        try:
            return conn.execute(
                "SELECT COUNT(*), ROUND(SUM(price_native), 2) FROM price_points"
            ).fetchone()
        finally:
            conn.close()

    assert fingerprint(tmp_path / "a.db") == fingerprint(tmp_path / "b.db")


@pytest.mark.parametrize(
    "domain, kind",
    [
        ("alpha-kicks.example", "irregular_seen"),  # irregular percentages, real "was"
        ("deadstock-depot.example", "round_seen"),  # round, but the "was" is real
        ("everyday-shoes.example", "round_unseen"),  # round and the "was" never charged
        ("grandslam-store.example", "irregular_unseen"),  # irregular, never charged
    ],
)
def test_each_planted_behaviour_is_recovered(results, domain, kind):
    _, rows, _ = results
    assert rows[domain] == kind


def test_a_shop_that_never_shows_a_tag_is_not_analysed(results):
    _, rows, _ = results
    assert "inkwell-wear.example" not in rows


def test_tags_that_appear_during_the_window_check_out(results):
    # Only the honest and promo shops start untagged, and their "was" prices are real.
    summary, _, _ = results
    assert summary["claims"]["cuts"]["fresh"]["unsupported_share"] == 0


def test_tags_that_were_always_there_mostly_never_reach_the_price(results):
    summary, _, _ = results
    assert summary["claims"]["cuts"]["standing_28d"]["unsupported_share"] > 0.9


def test_database_is_opened_read_only(results, tmp_path):
    discounts = sys.modules["discounts"]
    conn = discounts.connect_readonly(results[2] / "demo.db")
    with pytest.raises(Exception, match="readonly"):
        conn.execute("DELETE FROM stores")
    conn.close()


def test_missing_database_is_a_clear_error(tmp_path):
    discounts = _load("discounts", "analysis/discounts.py")
    with pytest.raises(SystemExit, match="no database"):
        discounts.connect_readonly(tmp_path / "nope.db")


def test_charts_are_written_when_matplotlib_is_present(results, tmp_path):
    pytest.importorskip("matplotlib")
    discounts = sys.modules["discounts"]
    summary = discounts.run(results[2] / "demo.db", tmp_path, with_charts=True)
    assert "round_vs_unsupported.png" in summary["charts"]
    assert (tmp_path / "round_vs_unsupported.png").stat().st_size > 1000


# --- edge cases found in review -------------------------------------------------


def _bare_db(path):
    """Just the tables the analysis reads, so a test can write the history it needs."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE stores (id INTEGER PRIMARY KEY, domain TEXT, status TEXT DEFAULT 'ok',
                             platform TEXT DEFAULT 'shopify');
        CREATE TABLE products (id INTEGER PRIMARY KEY, store_id INT, last_seen TEXT);
        CREATE TABLE variants (id INTEGER PRIMARY KEY, product_id INT);
        CREATE TABLE product_keys (product_id INT, key_type TEXT, key TEXT);
        CREATE TABLE price_points (variant_id INT, ts TEXT, price_usd REAL, compare_at_usd REAL,
            in_stock INT DEFAULT 1, currency TEXT, price_native REAL, fx_rate REAL,
            compare_at_native REAL, PRIMARY KEY (variant_id, ts));
        INSERT INTO stores (id, domain) VALUES (1, 'shop.example');
        INSERT INTO products VALUES (1, 1, '2026-09-27T00:00:00+00:00');
        INSERT INTO variants VALUES (1, 1);
        """
    )
    return conn


def _point(conn, ts, price, compare, currency="USD"):
    conn.execute(
        "INSERT INTO price_points (variant_id, ts, price_usd, currency, price_native,"
        " compare_at_native) VALUES (1, ?, ?, ?, ?, ?)",
        (ts, price, currency, price, compare),
    )


def _claims(db, tmp_path):
    discounts = _load("discounts", "analysis/discounts.py")
    return discounts.run(db, tmp_path / "o", with_charts=False)["claims"]


def test_an_empty_database_gives_zeros_not_a_crash(tmp_path):
    db = tmp_path / "empty.db"
    _bare_db(db).commit()
    claims = _claims(db, tmp_path)
    assert claims["variants_with_a_claim"] == 0
    assert claims["cuts"]["all"]["unsupported_share"] is None


def test_a_file_that_is_not_the_schema_is_a_clear_error(tmp_path):
    import sqlite3

    db = tmp_path / "other.db"
    sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.commit()
    discounts = _load("discounts", "analysis/discounts.py")
    with pytest.raises(SystemExit, match="does not look like"):
        discounts.run(db, tmp_path / "o", with_charts=False)


def test_a_small_tag_is_not_a_claim(tmp_path):
    # 3% above the price: the price itself would otherwise "vouch" for the tag.
    db = tmp_path / "small.db"
    conn = _bare_db(db)
    _point(conn, "2026-09-01T00:00:00+00:00", 10.0, 10.3)
    conn.commit()
    assert _claims(db, tmp_path)["variants_with_a_claim"] == 0


def test_a_price_in_another_currency_does_not_vouch_for_the_tag(tmp_path):
    # 100 NOK is not a sighting of a 12 EUR "was" price.
    db = tmp_path / "cur.db"
    conn = _bare_db(db)
    _point(conn, "2026-09-01T00:00:00+00:00", 100.0, None, "NOK")
    _point(conn, "2026-09-10T00:00:00+00:00", 9.0, 12.0, "EUR")
    conn.commit()
    cuts = _claims(db, tmp_path)["cuts"]
    assert cuts["all"]["unsupported"] == 1


def test_the_watch_window_ends_when_the_product_was_last_seen_not_last_changed(tmp_path):
    # One price point on day 1, product seen again on day 27 (=26 days): not four weeks.
    # Seen on day 30 or later, with the price never changing again: four weeks.
    db = tmp_path / "win.db"
    conn = _bare_db(db)
    _point(conn, "2026-08-24T00:00:00+00:00", 50.0, 100.0)
    conn.execute("UPDATE products SET last_seen = '2026-09-27T00:00:00+00:00'")
    conn.commit()
    cuts = _claims(db, tmp_path)["cuts"]
    assert cuts["long_watch"]["variants"] == 1
    assert cuts["standing_28d"]["variants"] == 1


def test_running_the_analysis_does_not_change_the_database(results):
    import hashlib

    _, _, work = results
    path = work / "demo.db"
    discounts = sys.modules["discounts"]
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    discounts.run(path, work / "again", with_charts=False)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
