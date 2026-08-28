"""Command line entry point: python -m pi <command>."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from . import db as dbm
from . import pipeline, reference, tls
from .config import Config, load_config
from .domains import same_host
from .notify import Telegram
from .sources import detect, jsonld
from .throttle import RateLimiter


def _log(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def read_sites(path: Path) -> list[str]:
    """Read the site list: one URL or bare domain per line, # for comments.

    Entries differing only by a `www.` prefix are the same server, and keeping
    both means crawling one shop twice: the list held `sneakerjunkiesusa.com` and
    `www.sneakerjunkiesusa.com`, which became 9,868 and 9,812 near-identical
    products in the database. The first spelling seen wins.
    """
    if not path.exists():
        raise SystemExit(f"site list not found: {path}")
    domains: list[str] = []
    seen: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        host = urlparse(line if "//" in line else f"https://{line}").netloc or line
        host = host.split("@")[-1].split(":")[0].strip().lower()
        if not host:
            continue
        key = same_host(host)
        if key in seen:
            if seen[key] != host:
                print(f"  {host}: same shop as {seen[key]}, skipped")
            continue
        seen[key] = host
        domains.append(host)
    return domains


def cmd_sites(args, config: Config) -> int:
    conn = dbm.connect(config.db_path)
    domains = read_sites(Path(args.file) if args.file else config.sites_file)
    added = 0
    with dbm.transaction(conn):
        for domain in domains:
            before = conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
            dbm.upsert_store(conn, domain)
            added += conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0] - before
    total = conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
    print(f"{len(domains)} domains in list, {added} new, {total} stores known")
    return 0


def cmd_detect(args, config: Config) -> int:
    conn = dbm.connect(config.db_path)
    if args.only:
        domains = [d.strip().lower() for d in args.only.split(",") if d.strip()]
    else:
        rows = conn.execute(
            "SELECT domain FROM stores"
            + ("" if args.all else " WHERE platform = 'unknown' OR platform = 'new'")
            + " ORDER BY domain"
        ).fetchall()
        domains = [r[0] for r in rows]
    if args.limit:
        domains = domains[: args.limit]
    if not domains:
        print("nothing to probe — everything is already classified (use --all to redo)")
        return 0

    async def go():
        async with pipeline.make_client(timeout=20) as client:
            return await detect.detect_all(
                conn, domains, client, config.concurrency,
                limiter=RateLimiter(
                    rate=config.shopify_rate, per_host_rate=config.shopify_host_rate
                ),
                ca_cache=tls.cache_dir(config.db_path),
            )

    tally = asyncio.run(go())
    print("\nplatform breakdown:")
    for platform, count in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {platform:10s} {count}")
    return 0


def cmd_run(args, config: Config) -> int:
    conn = dbm.connect(config.db_path)
    stats = asyncio.run(
        pipeline.run(
            config, conn,
            domains=tuple(d.strip() for d in args.stores.split(",")) if args.stores else (),
            collect_only=args.collect_only,
            dry_run=args.dry_run,
            rescan=args.rescan,
            limit=args.limit,
            jsonld_budget=args.jsonld_budget,
        )
    )
    print(
        f"\nstores ok {stats.stores_ok} / failed {stats.stores_failed} · "
        f"products {stats.products_seen:,} · price changes {stats.points_written:,} · "
        f"alerts sent {stats.alerts_sent}"
    )
    for domain, error in stats.failures[:15]:
        print(f"  ! {domain}: {error}")
    return 0


def cmd_health(args, config: Config) -> int:
    conn = dbm.connect(config.db_path)
    report = pipeline.health_report(conn)
    if not args.send:
        print(report.replace("<b>", "").replace("</b>", ""))
        return 0
    if not config.telegram_ready:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 1

    async def go():
        async with Telegram(config.bot_token, config.chat_id) as tg:
            return await tg.send_text(report)

    return 0 if asyncio.run(go()) else 1


def cmd_seed(args, config: Config) -> int:
    conn = dbm.connect(config.db_path)
    already = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    n = pipeline.seed_alerts(conn, config, dry_run=args.dry_run)
    if args.dry_run:
        print(f"сейчас проходят пороги: {n:,} товаров (ничего не записано)")
        return 0
    print(
        f"помечено как уже виденное: {n:,} товаров "
        f"(в alerts было {already:,}, стало "
        f"{conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]:,})"
    )
    print("дальше приходят только новые падения цены")
    return 0


def cmd_find(args, config: Config) -> int:
    """Ask the database what is on offer right now, instead of waiting to be told.

    The same scoring the notifier uses, with the thresholds overridable on the
    command line, and without the two rules that exist only because a
    notification run has to be short: nothing is capped per shop, and a deal
    already announced still shows up — it is still on offer.
    """
    conn = dbm.connect(config.db_path)
    overrides: dict = {}
    if args.brand:
        overrides["brands_allow"] = tuple(b.strip().lower() for b in args.brand.split(","))
    if args.size:
        overrides["sizes"] = tuple(z.strip().upper() for z in args.size.split(","))
    if args.any_size:
        overrides["sizes"] = ()
    for name, value in (
        ("min_discount_pct", args.min_discount),
        ("min_price_usd", args.min_price),
        ("max_price_usd", args.max_price),
        ("min_saving_usd", args.min_saving),
        ("min_score", args.min_score),
    ):
        if value is not None:
            overrides[name] = value
    scoped = replace(config, filters=replace(config.filters, **overrides))

    print("считаю по всей базе, это занимает минуту-другую…", file=sys.stderr)
    found = pipeline.find_deals(
        conn, pipeline.all_scorable_variants(conn), scoped,
        cap_per_store=False, skip_alerted=False,
        watched=pipeline.watched_products(conn, pipeline.read_watchlist(config.watchlist_file)),
    )
    if args.shop:
        wanted = same_host(args.shop.strip().lower())
        found = [pair for pair in found if same_host(pair[1]["domain"]) == wanted]
    if not found:
        print("ничего не нашлось — попробуйте ослабить пороги")
        return 0

    for deal, row in found[: args.limit]:
        size = row["size_norm"] or row["size"] or "—"
        shop = row["store_name"] or row["domain"]
        print(
            f"−{deal.discount_pct:4.0f}%  ${deal.price_usd:>8,.2f}  {size:<8} "
            f"{(row['brand'] or '')[:14]:<14} {row['title'][:44]:<44} {shop[:22]:<22} "
            f"score {deal.score:3d}"
        )
        print(f"        {row['url']}")
    print(f"\nвсего {len(found):,}, показано {min(len(found), args.limit)}")
    return 0


def cmd_reindex(args, config: Config) -> int:
    """Rebuild the keys products are matched by between shops.

    A normal run keeps them current for what it collects; this fills them in for
    a catalogue gathered before the keys existed, and re-derives them all after a
    change to how they are extracted.
    """
    conn = dbm.connect(config.db_path)
    rows = conn.execute(
        """
        SELECT p.id, p.brand, p.title, group_concat(v.sku, char(10)) AS skus
        FROM products p LEFT JOIN variants v ON v.product_id = p.id
        GROUP BY p.id
        """
    ).fetchall()
    keyed = 0
    with dbm.transaction(conn):
        for row in rows:
            keys = reference.keys_for(
                row["brand"], row["title"], (row["skus"] or "").split("\n")
            )
            dbm.set_product_keys(conn, row["id"], keys)
            keyed += bool(keys)
    shared = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT k.key_type, k.key FROM product_keys k
            JOIN products p ON p.id = k.product_id
            GROUP BY k.key_type, k.key HAVING COUNT(DISTINCT p.store_id) > 1
        )
        """
    ).fetchone()[0]
    print(f"{len(rows):,} товаров, у {keyed:,} есть опознавательные ключи")
    print(f"{shared:,} ключей встречаются больше чем в одном магазине")
    return 0


def cmd_prune(args, config: Config) -> int:
    conn = dbm.connect(config.db_path)
    before = config.db_path.stat().st_size if config.db_path.exists() else 0

    points = dbm.prune_history(conn, args.keep_days)
    orphans = dbm.drop_orphans(conn)
    print(
        f"удалено: {points:,} точек истории старше {args.keep_days} дн, "
        f"{orphans['variants']:,} вариантов и {orphans['products']:,} товаров без цен"
    )
    if not args.no_vacuum:
        print("VACUUM…", flush=True)
        conn.execute("VACUUM")
    after = config.db_path.stat().st_size
    print(f"размер базы: {before / 1e6:,.0f} MB -> {after / 1e6:,.0f} MB")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sites", help="load data/sites.txt into the store table")
    p.add_argument("--file", help="override the site list path")
    p.set_defaults(func=cmd_sites)

    p = sub.add_parser("detect", help="probe how each store can be read")
    p.add_argument("--limit", type=int, help="probe at most N stores")
    p.add_argument("--only", help="comma-separated domains to probe")
    p.add_argument("--all", action="store_true", help="re-probe already classified stores")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("run", help="collect prices, score them, send alerts")
    p.add_argument("--dry-run", action="store_true", help="print the messages instead of sending")
    p.add_argument("--collect-only", action="store_true", help="gather prices, do not score")
    p.add_argument(
        "--rescan", action="store_true",
        help="score every in-stock variant, not only the ones whose price moved "
             "(use after changing filters.toml)",
    )
    p.add_argument("--limit", type=int, help="send at most N alerts")
    p.add_argument("--stores", help="comma-separated domains, for testing")
    p.add_argument(
        "--jsonld-budget", type=int,
        default=int(os.getenv("PI_JSONLD_BUDGET", jsonld.DEFAULT_BUDGET)),
        help="product pages to crawl per non-Shopify store per run",
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser(
        "seed",
        help="treat every discount that qualifies right now as already seen, so "
             "only new price drops are announced",
    )
    p.add_argument("--dry-run", action="store_true", help="just count them")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("prune", help="drop old price history and reclaim disk space")
    p.add_argument(
        "--keep-days", type=int, default=180,
        help="history to keep, in days (default 180; the newest price per "
             "variant is always kept)",
    )
    p.add_argument("--no-vacuum", action="store_true", help="skip VACUUM (faster, frees nothing)")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser(
        "reindex",
        help="rebuild the article numbers products are matched by between shops",
    )
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser(
        "find", help="search the database for what is discounted right now"
    )
    p.add_argument("--brand", help="comma-separated, matched as substrings")
    p.add_argument("--size", help="comma-separated, e.g. US10,EU44")
    p.add_argument("--any-size", action="store_true", help="ignore the size filter entirely")
    p.add_argument("--shop", help="only this shop's domain")
    p.add_argument("--min-discount", type=float, metavar="PCT")
    p.add_argument("--min-price", type=float, metavar="USD")
    p.add_argument("--max-price", type=float, metavar="USD")
    p.add_argument("--min-saving", type=float, metavar="USD")
    p.add_argument("--min-score", type=int)
    p.add_argument("--limit", type=int, default=25, help="rows to print (default 25)")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("health", help="collection health summary")
    p.add_argument("--send", action="store_true", help="send it to Telegram")
    p.set_defaults(func=cmd_health)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    _log(config.log_level)
    return args.func(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
