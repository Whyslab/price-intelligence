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
        ("alpha-kicks.example", "supported"),  # irregular percentages, real "was"
        ("deadstock-depot.example", "percentage_promo"),  # round, but the "was" is real
        ("everyday-shoes.example", "computed_tag"),  # round and the "was" never charged
        ("grandslam-store.example", "standing_tag"),  # irregular, never charged
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
