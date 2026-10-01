"""How honest are the struck-through prices?

Reads a price-intelligence database **read-only** and answers four questions:

1. How much data is this? (stores, products, price points, time span)
2. Of the variants that wear a "was" price, how many have been seen at anything
   near it? (``unsupported`` below)
3. Which stores show round, same-percentage-off discounts, and does the "was"
   price behind them check out? Round alone proves nothing (a real "30% off
   everything" sale is round too); round **and** never seen fits a percentage
   markdown applied on top of a price that was never charged. The data cannot
   say which direction the arithmetic ran, so no shop is called "computed".
4. How often do prices move at all?

Nothing here is a verdict about a single shop. A "was" price that was never
seen during the observation window may have been real before it began, so the
numbers are reported with the window length next to them and with two stricter
cuts: variants watched for at least four weeks, and variants first seen
*without* a tag, so the price before the tag was actually observed.

Run it::

    python analysis/discounts.py --db data/pi.db --out analysis/results
    python analysis/discounts.py --db data/demo.db --out /tmp/demo-results

Charts need matplotlib (``pip install -e ".[analysis]"``); without it the JSON
and CSV are still written.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

# A struck-through price counts as a real claim only when it is at least this
# much above the price: 10% off. Below that the claim is noise, and it would also
# overlap SEEN_SHARE: a tag 3% above the price is "seen" by the price itself.
MIN_CLAIM = 1.10
# The "was" price counts as seen if the variant was ever charged at least this
# share of it. 95%, not 100%, so a 0.01 rounding step cannot decide the verdict.
# It must stay below 1 / MIN_CLAIM (0.909) or the price would vouch for the tag.
SEEN_SHARE = 0.95
# A discount percentage this close to a multiple of five is "round". The width
# is a tenth of a percentage point either side, because a rule-made price is
# rounded to the cent and a price held for a year moves by more than that.
ROUND_TOLERANCE = 0.15
# Stores with fewer claims than this are too small to say anything about.
MIN_CLAIMS = 200
# A store is "round" when this much of its claims are round steps. The control
# window sits half a step away and has the same width; it is reported next to
# the round share so a reader can see how rarely the chance level is reached.
ROUND_THRESHOLD = 0.60
# A store is "unseen" when at least this share of its "was" prices was never
# charged in the data, and "seen" when at most the lower share was not.
UNSEEN_THRESHOLD = 0.80
SEEN_THRESHOLD = 0.20
LONG_WATCH_DAYS = 28
MIN_STORES_FOR_CURVE = 5

# round x unseen gives the four kinds of store the charts and the table use.
KINDS = {
    "round_unseen": "round steps and a “was” price never charged",
    "round_seen": "round steps, and the “was” price was charged",
    "irregular_unseen": "irregular percentages, “was” price never charged",
    "irregular_seen": "irregular percentages, “was” price was charged",
    "mixed": "in between",
}


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"no database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def overview(conn: sqlite3.Connection) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    first, last = conn.execute("SELECT MIN(ts), MAX(ts) FROM price_points").fetchone()
    days = (
        (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds() / 86400
        if first
        else 0
    )
    platforms = {
        f"{r[0]}/{r[1]}": r[2]
        for r in conn.execute(
            "SELECT status, platform, COUNT(*) FROM stores GROUP BY 1, 2 ORDER BY 3 DESC"
        )
    }
    return {
        "stores": one("SELECT COUNT(*) FROM stores"),
        "stores_read_ok": one("SELECT COUNT(*) FROM stores WHERE status = 'ok'"),
        "products": one("SELECT COUNT(*) FROM products"),
        "variants": one("SELECT COUNT(*) FROM variants"),
        "price_points": one("SELECT COUNT(*) FROM price_points"),
        "first_point": first,
        "last_point": last,
        "observation_days": round(days, 1),
        "stores_by_status_and_platform": platforms,
    }


def build_tables(conn: sqlite3.Connection) -> None:
    """Per-variant aggregates, kept in the connection's private temp database.

    Prices are compared in the shop's own currency. In dollars every daily
    exchange-rate tick would look like a price move, which is the mistake the
    collector itself avoids (see ``db.record_price``).
    """
    conn.executescript(
        f"""
        -- One row per variant and currency, so a shop that switches currency
        -- cannot make a price in one look like a price in the other.
        CREATE TEMP TABLE agg AS
            SELECT variant_id, currency,
                   MAX(price_native) AS hi,
                   MIN(price_native) AS lo,
                   COUNT(*)          AS n
            FROM price_points GROUP BY variant_id, currency;

        -- The window a variant was watched. A price point is written only when
        -- something changes, so the last point says when it last *changed*, not
        -- when it was last looked at; products.last_seen is the real end.
        CREATE TEMP TABLE win AS
            SELECT variant_id, MIN(ts) AS t0 FROM price_points GROUP BY variant_id;

        -- Was the variant already wearing a real discount tag the first time we
        -- saw it?
        CREATE TEMP TABLE first AS
            SELECT variant_id,
                   compare_at_native > price_native * {MIN_CLAIM} AS tagged_at_start,
                   MIN(ts) AS ts
            FROM price_points GROUP BY variant_id;

        -- The latest point on which the shop actually claimed a discount.
        CREATE TEMP TABLE tagged AS
            SELECT variant_id, currency, price_native AS p, compare_at_native AS c, MAX(ts) AS ts
            FROM price_points
            WHERE compare_at_native > price_native * {MIN_CLAIM}
            GROUP BY variant_id;

        CREATE TEMP TABLE claim AS
            SELECT s.id AS store_id,
                   s.domain,
                   t.variant_id,
                   100.0 * (1 - t.p / t.c)                        AS pct,
                   (a.hi >= {SEEN_SHARE} * t.c)                    AS seen,
                   (julianday(p.last_seen) - julianday(w.t0) >= {LONG_WATCH_DAYS}) AS long_watch,
                   (NOT COALESCE(f.tagged_at_start, 0))            AS fresh
            FROM tagged t
            JOIN agg a      ON a.variant_id = t.variant_id AND a.currency = t.currency
            JOIN win w      ON w.variant_id = t.variant_id
            JOIN first f    ON f.variant_id = t.variant_id
            JOIN variants v ON v.id = t.variant_id
            JOIN products p ON p.id = v.product_id
            JOIN stores s   ON s.id = p.store_id;

        CREATE INDEX claim_store ON claim (store_id);
        """
    )


def classify(round_share: float, unsupported: float) -> str:
    is_round = round_share >= ROUND_THRESHOLD
    if unsupported >= UNSEEN_THRESHOLD:
        return "round_unseen" if is_round else "irregular_unseen"
    if unsupported <= SEEN_THRESHOLD:
        return "round_seen" if is_round else "irregular_seen"
    return "mixed"


def per_store(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT domain,
               COUNT(*)                                         AS claims,
               AVG(NOT seen)                                    AS unsupported,
               AVG(ABS(pct - 5 * ROUND(pct / 5.0)) <= {ROUND_TOLERANCE})
                                                                AS round_share,
               AVG(ABS((pct - 5 * CAST(pct / 5.0 AS INTEGER)) - 2.5)
                                    <= {ROUND_TOLERANCE})       AS control_share,
               AVG(pct)                                         AS mean_pct,
               SUM(fresh)                                       AS fresh_claims,
               AVG(CASE WHEN fresh THEN NOT seen END)           AS fresh_unsupported
        FROM claim
        GROUP BY store_id
        HAVING claims >= {MIN_CLAIMS}
        ORDER BY claims DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["kind"] = classify(d["round_share"], d["unsupported"])
        out.append(d)
    return out


def _share(num, den):
    return (num or 0) / den if den else None


def concentration(conn: sqlite3.Connection, where: str, total: int) -> dict:
    """How many shops a cut rests on, and how much of it the three largest supply.

    A share over 1.3 million variants can still be one shop's catalogue. A cut that
    two shops dominate is a statement about those two shops.
    """
    rows = conn.execute(
        f"SELECT domain, COUNT(*) FROM claim WHERE {where} GROUP BY domain ORDER BY 2 DESC"
    ).fetchall()
    top3 = sum(c for _, c in rows[:3])
    return {
        "stores": len(rows),
        "top3_share_of_variants": (top3 / total) if total else None,
        "top3_stores": [d for d, _ in rows[:3]],
    }


def claims_summary(conn: sqlite3.Connection, stores: list[dict]) -> dict:
    """The same question answered four ways, because each one has a weakness.

    * all claims — the biggest sample, but a "was" price from before the first
      observation can never be seen, so this overstates;
    * long watch — only variants followed for four weeks (first point to the
      last time the product was seen), which is fairer but keeps the survivors:
      a variant has to stay listed that long;
    * fresh — only variants first seen *without* a tag, so the price before the
      tag appeared was actually observed. The cleanest test of a tag that
      *appeared* during the window, and blind to tags that were already there;
    * standing_28d — variants that arrived wearing a tag and were then watched
      for four weeks. The tag was already on at first sight and the product was
      still listed four weeks later; if the price never once reached it, nothing
      was ever visibly "reduced from" that number here. The tag may have changed
      in between, so this is "a tag", not "the same tag all along".
    """
    cuts = {}
    for name, where in (
        ("all", "1"),
        ("long_watch", "long_watch"),
        ("fresh", "fresh"),
        ("standing_28d", "long_watch AND NOT fresh"),
    ):
        total, unsupported = conn.execute(
            f"SELECT COUNT(*), SUM(NOT seen) FROM claim WHERE {where}"
        ).fetchone()
        cuts[name] = {
            "variants": total,
            "unsupported": unsupported or 0,
            "unsupported_share": _share(unsupported, total),
        }
    for name, where in (("all", "1"), ("long_watch", "long_watch"), ("fresh", "fresh"),
                        ("standing_28d", "long_watch AND NOT fresh")):
        cuts[name].update(concentration(conn, where, cuts[name]["variants"]))
    over30 = conn.execute("SELECT COUNT(*) FROM claim WHERE pct >= 30").fetchone()[0]
    kinds = {k: {"stores": 0, "claims": 0} for k in KINDS}
    for s in stores:
        kinds[s["kind"]]["stores"] += 1
        kinds[s["kind"]]["claims"] += s["claims"]
    analysed = sum(s["claims"] for s in stores)
    return {
        "variants_with_a_claim": cuts["all"]["variants"],
        "claims_over_30_percent": over30,
        "cuts": cuts,
        "stores_analysed": len(stores),
        "min_claims_per_store": MIN_CLAIMS,
        "claims_in_analysed_stores": analysed,
        "store_kinds": kinds,
        "median_store_unsupported": statistics.median(s["unsupported"] for s in stores)
        if stores
        else None,
        "median_round_share": statistics.median(s["round_share"] for s in stores)
        if stores
        else None,
        "median_control_share": statistics.median(s["control_share"] for s in stores)
        if stores
        else None,
    }


def movement(conn: sqlite3.Connection) -> dict:
    total, any_move, big_move, single = conn.execute(
        """
        SELECT SUM(n >= 3),
               SUM(n >= 3 AND hi > lo * 1.05),
               SUM(n >= 3 AND hi > lo * 1.20),
               SUM(n = 1)
        FROM agg
        """
    ).fetchone()
    variants = conn.execute("SELECT COUNT(*) FROM agg").fetchone()[0]  # variant x currency
    return {
        "variants_seen_once": single or 0,
        "variants_seen_once_share": (single or 0) / variants if variants else None,
        "variants_with_3plus_points": total or 0,
        "moved_over_5_percent": any_move or 0,
        "moved_over_20_percent": big_move or 0,
    }


def cross_store(conn: sqlite3.Connection) -> dict:
    """How many articles can be compared across shops at all.

    Deliberately counts only coverage. A price spread over the same article
    looks large and is mostly noise: pre-owned pairs, resale prices, different
    sizes and stale rows all land in it. See docs/findings.md.
    """
    rows = conn.execute(
        """
        SELECT n, COUNT(*) FROM (
            SELECT COUNT(DISTINCT p.store_id) AS n
            FROM product_keys k JOIN products p ON p.id = k.product_id
            WHERE k.key_type = 'style'
            GROUP BY k.key
        ) GROUP BY n ORDER BY n
        """
    ).fetchall()
    dist = {int(n): c for n, c in rows}
    return {
        "articles": sum(dist.values()),
        "in_2plus_stores": sum(c for n, c in dist.items() if n >= 2),
        "in_3plus_stores": sum(c for n, c in dist.items() if n >= 3),
    }


def histogram(conn: sqlite3.Connection, groups: dict[str, set[str]]) -> dict[str, dict[float, float]]:
    """Discount percentages in half-point bins, as shares, one curve per group."""
    out = {}
    for label, domains in groups.items():
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS pick (domain TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM pick")
        conn.executemany("INSERT INTO pick VALUES (?)", [(d,) for d in domains])
        rows = conn.execute(
            """
            SELECT ROUND(pct * 2) / 2.0 AS bin, COUNT(*) FROM claim
            WHERE domain IN (SELECT domain FROM pick) AND pct BETWEEN 5 AND 80
            GROUP BY bin ORDER BY bin
            """
        ).fetchall()
        total = sum(c for _, c in rows) or 1
        out[label] = {b: c / total for b, c in rows}
    return out


def write_csv(path: Path, stores: list[dict]) -> None:
    cols = ["domain", "claims", "unsupported", "round_share", "control_share", "mean_pct",
            "fresh_claims", "fresh_unsupported", "kind"]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for s in stores:
            fu = s["fresh_unsupported"]
            w.writerow(
                [s["domain"], s["claims"]]
                + [f"{s[c]:.4f}" for c in ("unsupported", "round_share", "control_share")]
                + [f"{s['mean_pct']:.2f}", s["fresh_claims"] or 0,
                   "" if fu is None else f"{fu:.4f}", s["kind"]]
            )


COLORS = {
    "round_unseen": "#c2410c",
    "irregular_unseen": "#a16207",
    "round_seen": "#2563eb",
    "irregular_seen": "#0f766e",
    "mixed": "#7b8794",
}


def charts(out: Path, stores: list[dict], hist: dict) -> list[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: skipping charts", file=sys.stderr)
        return []

    ink, muted = "#1f2933", "#7b8794"
    plt.rcParams.update(
        {
            "font.size": 11,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": muted,
            "axes.labelcolor": ink,
            "xtick.color": ink,
            "ytick.color": ink,
            "figure.dpi": 120,
        }
    )
    made = []

    # 1. Each store: how round its discounts are against how often the "was"
    # price was ever seen. The four corners are the four kinds of store.
    fig, ax = plt.subplots(figsize=(8.4, 5.6))
    for kind, color in COLORS.items():
        pts = [s for s in stores if s["kind"] == kind]
        if not pts:
            continue
        ax.scatter(
            [s["round_share"] * 100 for s in pts],
            [s["unsupported"] * 100 for s in pts],
            s=[max(18, min(260, s["claims"] / 120)) for s in pts],
            color=color, alpha=0.7, edgecolor="white", linewidth=0.6,
            label=f"{kind.replace('_', ' ')} ({len(pts)})",
        )
    ax.axvline(ROUND_THRESHOLD * 100, color=muted, linestyle="--", linewidth=1)
    ax.axhline(UNSEEN_THRESHOLD * 100, color=muted, linestyle="--", linewidth=1)
    ax.set_xlabel("Discounts that land on a round 5% step (%)")
    ax.set_ylabel("“Was” prices never charged in the data (%)")
    ax.set_title("Round discounts and unseen “was” prices, per store", loc="left")
    ax.legend(frameon=False, loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=9)
    fig.text(0.01, 0.005, "Marker size = number of discounted variants.", color=muted, fontsize=9)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(out / "round_vs_unsupported.png", bbox_inches="tight")
    plt.close(fig)
    made.append("round_vs_unsupported.png")

    # 2. The shape of the percentages. Round steps are a feature of promotions
    # as well as of unseen "was" prices, which is why a store is judged on round *and*
    # unseen together, never on round alone.
    fig, ax = plt.subplots(figsize=(8.4, 4.4))
    for label in ("round_unseen", "irregular_unseen", "round_seen", "irregular_seen"):
        curve = hist.get(label)
        n = sum(1 for s in stores if s["kind"] == label)
        # A curve drawn from one or two stores says more about those stores than
        # about a kind of store, so it is left out rather than drawn small.
        if not curve or n < MIN_STORES_FOR_CURVE:
            continue
        bins = sorted(curve)
        ax.plot(bins, [curve[b] * 100 for b in bins], color=COLORS[label], linewidth=1.3,
                label=f"{label.replace('_', ' ')} ({n} stores)")
    ax.set_xlabel("Discount shown (%)")
    ax.set_ylabel("Share of discounted variants (%)")
    ax.set_title("Where the discount percentages fall, by kind of store", loc="left")
    if ax.get_legend_handles_labels()[0]:
        ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(out / "discount_distribution.png")
    plt.close(fig)
    made.append("discount_distribution.png")

    return made


def run(db: Path, out: Path, with_charts: bool = True) -> dict:
    conn = connect_readonly(db)
    try:
        return _run(conn, out, with_charts)
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"{db} does not look like a price-intelligence database: {exc}") from exc
    finally:
        conn.close()


def _run(conn: sqlite3.Connection, out: Path, with_charts: bool) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    build_tables(conn)
    stores = per_store(conn)
    summary = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "definitions": {
            "claim": f"a point where the struck-through price is >{(MIN_CLAIM - 1) * 100:.0f}% "
                     "above the price",
            "unsupported": f"no point in the data charged ≥{SEEN_SHARE * 100:.0f}% of the "
                           "latest struck-through price",
            "round": f"discount within {ROUND_TOLERANCE} points of a multiple of 5",
            "control": "same window, half a step away — a reference, not a proof",
            "store kinds": {
                k: v for k, v in KINDS.items()
            },
            "store thresholds": {
                "round": f"round share ≥ {ROUND_THRESHOLD * 100:.0f}%",
                "unseen": f"unsupported ≥ {UNSEEN_THRESHOLD * 100:.0f}%",
                "seen": f"unsupported ≤ {SEEN_THRESHOLD * 100:.0f}%",
            },
        },
        "overview": overview(conn),
        "claims": claims_summary(conn, stores),
        "movement": movement(conn),
        "cross_store": cross_store(conn),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    write_csv(out / "stores.csv", stores)
    if with_charts:
        groups = {k: {s["domain"] for s in stores if s["kind"] == k} for k in KINDS}
        summary["charts"] = charts(out, stores, histogram(conn, groups))
        (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=Path("data/pi.db"))
    ap.add_argument("--out", type=Path, default=Path("analysis/results"))
    ap.add_argument("--no-charts", action="store_true")
    args = ap.parse_args()
    summary = run(args.db, args.out, with_charts=not args.no_charts)
    c = summary["claims"]
    for name, cut_ in c["cuts"].items():
        share = cut_["unsupported_share"]
        shown = "n/a" if share is None else f"{share * 100:.1f}%"
        print(f"{name:>10}: {cut_['variants']:>9,} variants, {shown} never seen at the 'was' price")
    for kind, v in c["store_kinds"].items():
        print(f"{kind:>17}: {v['stores']:>3} stores, {v['claims']:>9,} claims")
    print(f"wrote {args.out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
