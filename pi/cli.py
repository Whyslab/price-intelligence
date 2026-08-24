"""Command line entry point: python -m pi <command>."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import db as dbm
from . import pipeline
from .config import Config, load_config
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
    """Read the site list: one URL or bare domain per line, # for comments."""
    if not path.exists():
        raise SystemExit(f"site list not found: {path}")
    domains: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        host = urlparse(line if "//" in line else f"https://{line}").netloc or line
        host = host.split("@")[-1].split(":")[0].strip().lower()
        if host:
            domains.append(host)
    return list(dict.fromkeys(domains))


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
                limiter=RateLimiter(rate=config.shopify_rate),
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
        "--jsonld-budget", type=int, default=jsonld.DEFAULT_BUDGET,
        help="product pages to crawl per non-Shopify store per run",
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("prune", help="drop old price history and reclaim disk space")
    p.add_argument(
        "--keep-days", type=int, default=180,
        help="history to keep, in days (default 180; the newest price per "
             "variant is always kept)",
    )
    p.add_argument("--no-vacuum", action="store_true", help="skip VACUUM (faster, frees nothing)")
    p.set_defaults(func=cmd_prune)

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
