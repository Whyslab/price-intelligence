"""Command line entry point: python -m pi <command>."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import replace
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import bot, digest, landed, personal, pipeline, reference, taxonomy, tls, web
from . import db as dbm
from .config import Config, load_config
from .domains import same_host, shop_link
from .fx import load_rates
from .notify import Telegram
from .sources import detect, jsonld
from .sources.base import normalize_size
from .throttle import RateLimiter


def _log(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


# How long a heavy command waits for another to finish before standing aside.
# A sweep takes about a quarter of an hour and the next one is an hour away, so
# a run that finds the prune still going gives up after ten minutes and leaves
# the collecting to the next hour. Maintenance started by a timer or by hand is
# worth waiting for longer: it is rare, and the run it waits for always ends.
RUN_WAIT_SECONDS = 10 * 60
MAINTENANCE_WAIT_SECONDS = 25 * 60


def _stood_aside(what: str) -> None:
    print(
        f"{what}: база занята другой тяжёлой командой (прогон, pi prune, reshelve, "
        "reindex или reclassify) — ничего не сделано, попробуйте позже",
        file=sys.stderr,
    )


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
        if args.all:
            where = ""
        elif args.broken:
            # Everything not producing data, whatever the reason. Worth a second
            # look after any change to how shops are read — the verdict recorded
            # last time was reached by the code as it was then.
            where = " WHERE platform NOT IN ('shopify', 'jsonld') OR last_error IS NOT NULL"
        else:
            where = " WHERE platform = 'unknown' OR platform = 'new'"
        rows = conn.execute(
            "SELECT domain FROM stores" + where + " ORDER BY domain"
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
    with dbm.collector_lock(config.db_path, RUN_WAIT_SECONDS) as held:
        if not held:
            _stood_aside("pi run")
            return 0
        return _cmd_run(args, config)


def _cmd_run(args, config: Config) -> int:
    conn = dbm.connect(config.db_path)
    try:
        stats = asyncio.run(
            pipeline.run(
                config, conn,
                domains=tuple(d.strip() for d in args.stores.split(",")) if args.stores else (),
                collect_only=args.collect_only,
                dry_run=args.dry_run,
                rescan=args.rescan,
                limit=args.limit,
                jsonld_budget=args.jsonld_budget,
                verify_budget=args.verify_budget,
            )
        )
    except Exception as exc:
        # Written down so the daily summary can say a run died, which until now
        # nothing did: two runs on 20.09 ended in a traceback and the only
        # trace of it was the journal.
        dbm.note_failed_run(conn, f"{type(exc).__name__}: {exc}")
        raise
    print(
        f"\nstores ok {stats.stores_ok} / failed {stats.stores_failed} · "
        f"products {stats.products_seen:,} · price changes {stats.points_written:,} · "
        f"alerts sent {stats.alerts_sent}"
    )
    for domain, error in stats.failures[:15]:
        print(f"  ! {domain}: {error}")
    return 0



def cmd_verify(args, config: Config) -> int:
    with dbm.collector_lock(config.db_path, RUN_WAIT_SECONDS) as held:
        if not held:
            _stood_aside("pi verify")
            return 0
        return _cmd_verify(args, config)


def _cmd_verify(args, config: Config) -> int:
    """Check the oldest cards on the shelf still exist, without sweeping anything.

    A price this finds is scored and sent like any other: it goes through the
    same run, only with the collection step left out.
    """
    conn = dbm.connect(config.db_path)
    stats = asyncio.run(
        pipeline.run(
            config, conn,
            collect=False,
            dry_run=args.dry_run,
            verify_budget=args.limit,
        )
    )
    print(
        f"\nпроверено {stats.verified} · снято с полки {stats.withdrawn} · "
        f"отправлено {stats.alerts_sent}"
    )
    return 0


def cmd_web(args, config: Config) -> int:
    """Serve the shelf as a page. Blocks until interrupted."""
    if not config.db_path.exists():
        print(f"no database at {config.db_path} — run `pi run` first", file=sys.stderr)
        return 1
    if args.host != "127.0.0.1":
        print(
            f"serving on {args.host}: there is no login on this page, so put it "
            "behind something that has one",
            file=sys.stderr,
        )
    # Open it once through the migrator: the server itself connects raw — one
    # read-only handle per request — so nothing else here would ever bring an
    # older database up to the schema this build writes favourites into.
    dbm.connect(config.db_path).close()
    if args.owner and args.host != "127.0.0.1":
        # --owner is a machine saying "everyone who reaches me is this person".
        # On localhost that is true. On an address other people can reach it
        # hands them somebody else's favourites, so the two do not combine.
        print(
            "--owner works only on 127.0.0.1: away from this machine, identity "
            "has to come from Telegram",
            file=sys.stderr,
        )
        return 2
    web.serve(
        config.db_path,
        host=args.host,
        port=args.port,
        bot_token=config.bot_token,
        owner_id=args.owner,
        # The person who runs the collector is not a customer of it. Their id
        # comes from .env, which for a private chat is the same number, and it
        # grants nothing on its own — Telegram still has to sign the request.
        # Only a positive id, which is what a person's is. A bot posting into a
        # channel has a negative chat id, and taking that as the owner's user id
        # matches nobody — locking the owner out of their own shelf with nothing
        # anywhere saying why.
        exempt_id=int(config.chat_id) if (config.chat_id or "").isdigit() else None,
        subscription=config.subscription,
    )
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
    with dbm.collector_lock(config.db_path, MAINTENANCE_WAIT_SECONDS) as held:
        if not held:
            _stood_aside("pi seed")
            return 1
        return _cmd_seed(args, config)


def _cmd_seed(args, config: Config) -> int:
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
        kids=args.kids,
    )
    # The shop's own boundary, drawn here rather than inside `arrange_for`:
    # that function also writes the shelf (pipeline.py, `on_offer`), and a row
    # never written cannot be revealed by asking. So women's stay in the table
    # and out of the answer — the same split the page makes.
    if not args.women:
        found = [pair for pair in found if pair[1]["gender"] != "women"]
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


def cmd_price(args, config: Config) -> int:
    """What every shop charges for one article, cheapest first.

    The other half of what the shelf shows on a product card, asked from the
    outside: the card can only compare something already discounted, because it
    starts from a product id and the page only has ids for what is on offer.
    """
    conn = dbm.connect(config.db_path)
    found = dbm.lookup_article(conn, args.query, limit=args.limit)

    if found["too_common"]:
        print(
            f"«{found['too_common']}» стоит в поле артикула у слишком многих разных "
            f"товаров — это слово, а не номер модели. Ищу по названию.\n"
        )
    if not found["shops"]:
        print("ничего не нашлось")
        return 1

    shown = len(found["shops"])
    if found["same_thing"]:
        head = f"артикул {found['key']} · {bot.plural(found['found'], 'магазин', 'магазина', 'магазинов')}"
        print(head + (f", показано {shown}\n" if shown < found["found"] else "\n"))
    else:
        print(
            f"поиск по названию · {bot.plural(found['found'], 'товар', 'товара', 'товаров')}"
            + (f", показано {shown}" if shown < found["found"] else "")
            + " — это разные вещи, а не одна в разных магазинах\n"
        )

    for row in found["shops"]:
        shop = row["store_name"] or row["domain"]
        cut = f"−{row['discount_pct']:.0f}%" if row["discount_pct"] else ""
        native = ""
        if row["currency"] and row["currency"] != "USD":
            native = f"{row['price_native']:,.0f} {row['currency']}"
        print(
            f"${row['price_usd']:>9,.2f} {cut:>5}  {shop[:26]:<26} "
            f"{(row['country'] or ''):<3} {native:<14} {row['title'][:44]}"
        )
        print(f"           {row['url']}")

    if found["same_thing"] and found["found"] == 1:
        print("\nтолько один магазин — сравнить не с чем")
    return 0


def _renormalise_sizes(conn) -> int:
    """Re-derive `variants.size_norm` from the size the shop wrote.

    Normalisation is stored, not computed on read, so a shop's size only gets
    the current reading when that shop is next collected — weeks, for a catalogue
    swept in slices. After a change to `normalize_size` the stored values are
    stale, and a size filter silently skips whatever still carries the old form:
    "X-Large" normalised to "X-L" for 12,017 variants, and nobody's filter says
    "X-L".
    """
    rows = conn.execute("SELECT id, size, size_norm FROM variants WHERE size IS NOT NULL")
    changed = [
        (fresh, variant_id)
        for variant_id, size, stored in rows
        if (fresh := normalize_size(size)) != stored
    ]
    if changed:
        with dbm.transaction(conn):
            conn.executemany("UPDATE variants SET size_norm = ? WHERE id = ?", changed)
    return len(changed)



def cmd_reindex(args, config: Config) -> int:
    with dbm.collector_lock(config.db_path, MAINTENANCE_WAIT_SECONDS) as held:
        if not held:
            _stood_aside("pi reindex")
            return 1
        return _cmd_reindex(args, config)


def _cmd_reindex(args, config: Config) -> int:
    """Rebuild what the code derives from what the shops actually sent.

    Article numbers, so products can be matched between shops, and normalised
    sizes. A normal run keeps both current for what it collects; this fills them
    in for a catalogue gathered earlier, and re-derives everything after a change
    to how they are read.
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
    resized = _renormalise_sizes(conn)
    print(f"размеров пересчитано: {resized:,}")
    return 0



def cmd_reclassify(args, config: Config) -> int:
    with dbm.collector_lock(config.db_path, MAINTENANCE_WAIT_SECONDS) as held:
        if not held:
            _stood_aside("pi reclassify")
            return 1
        return _cmd_reclassify(args, config)


def _cmd_reclassify(args, config: Config) -> int:
    """Re-derive brand, gender, kind and audience from what the shops wrote.

    Separate from `reindex` because it answers a different question — that one
    asks which products are the same product, this one asks what each product
    is. Both are rebuilt rather than patched, and both need re-running after a
    change to the rules they apply.
    """
    conn = dbm.connect(config.db_path)
    stats = taxonomy.classify(conn)
    total = stats["products"]

    def share(n: int) -> str:
        return f"{n:,} ({n / total * 100:.1f}%)" if total else "0"

    known = stats["gender_stated"] + stats["gender_borrowed"]
    print(f"товаров: {total:,}")
    print(f"марка определена:  {share(stats['brand'])}, словарь {stats['brands']:,} марок")
    print(f"тип определён:     {share(stats['kind'])}")
    print(f"детских товаров:   {share(stats['kids'])}")
    print(
        f"пол определён:     {share(known)}"
        f" — {stats['gender_stated']:,} названо магазином,"
        f" {stats['gender_borrowed']:,} перенесено по артикулу"
    )
    return 0


def cmd_bot(args, config: Config) -> int:
    """Serve the Telegram bot until stopped.

    Unlike every other command here this one does not finish: answering a button
    press needs a process that is already running. It is the only long-lived part
    of the project, and it collects nothing — it reads what the sweep left.
    """
    conn = dbm.connect(config.db_path)
    try:
        return asyncio.run(bot.serve(config, conn))
    except KeyboardInterrupt:
        return 0



def cmd_prune(args, config: Config) -> int:
    with dbm.collector_lock(config.db_path, MAINTENANCE_WAIT_SECONDS) as held:
        if not held:
            _stood_aside("pi prune")
            return 1
        return _cmd_prune(args, config)


def _cmd_prune(args, config: Config) -> int:
    conn = dbm.connect(config.db_path)
    before = config.db_path.stat().st_size if config.db_path.exists() else 0

    points = dbm.prune_history(conn, args.keep_days)
    grace = args.delisted_grace_days
    if grace is None:
        grace = config.filters.delisted_grace_days
    delisted = dbm.drop_delisted(conn, grace)
    orphans = dbm.drop_orphans(conn)
    print(
        f"удалено: {points:,} точек истории старше {args.keep_days} дн, "
        f"{delisted:,} товаров, снятых с продажи больше {grace} дн назад, "
        f"{orphans['variants']:,} вариантов и {orphans['products']:,} товаров без цен"
    )
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0] * page
    if args.vacuum:
        # Rewrites the whole file and holds the write lock the whole time — 47
        # minutes on 2.7 GB, during which the bot cannot save a thing. Only on
        # request, and best with the services stopped.
        print("VACUUM…", flush=True)
        conn.execute("VACUUM")
    else:
        # Free pages are reused by the next writes, and the file grows back
        # within days anyway, so shrinking it every week bought nothing.
        print(f"свободно внутри файла: {free / 1e6:,.0f} MB (займёт новая история; "
              "сжать файл: pi prune --vacuum при остановленных службах)")
    after = config.db_path.stat().st_size
    print(f"размер базы: {before / 1e6:,.0f} MB -> {after / 1e6:,.0f} MB")
    return 0


def cmd_digest(args, config: Config) -> int:
    """Send the free tier's two finds of the day.

    Separate from `run` on purpose. The run is hourly and its budget belongs to
    collection; the digest is daily, reads only the shelf, and must arrive even
    on a day when every sweep was blocked.
    """
    from .bot import format_card

    conn = dbm.connect(config.db_path)
    picked = digest.pick(conn, count=args.count)
    if not picked:
        print("нечего показать: всё, что стоит на полке, уже было в бесплатной ленте")
        return 0

    now = dbm.utcnow()
    shipping = landed.load_rules()
    rates = load_rates(config.db_path.parent / "fx_cache.json")
    eur = rates.to_usd(1.0, "EUR")
    eur_usd = eur[0] if eur else None

    cards = []
    for row in picked:
        delivered = landed.landed_all(
            shipping, row["price_usd"], row["kind"], row["domain"],
            row["country"], eur_usd,
        )
        sizes = dbm.sizes_in_stock(conn, row["product_id"])
        caption = format_card(row, sizes, now, delivered)
        # The card shown in the bot has a button to the shop; this one travels as
        # a bare photo, and without the link in its text a free reader was shown
        # a find with no way to open it.
        link = shop_link(row["url"], row["domain"])
        if link:
            caption += f'\n🔗 <a href="{escape(link)}">Открыть в магазине</a>'
        cards.append((row, caption))

    if args.dry_run:
        for _, caption in cards:
            print(caption.replace("<b>", "").replace("</b>", ""))
            print("-" * 60)
        print(f"({len(cards)} шт, ничего не отправлено и не записано)")
        return 0

    if not config.telegram_ready:
        print("TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return 1

    audience = digest.readers(conn, config.subscription)
    if not audience:
        print("бесплатных читателей нет — нечего рассылать")
        return 0

    async def go() -> int:
        sent = 0
        async with httpx.AsyncClient(timeout=30) as client:
            for row, caption in cards:
                # Claimed once for the whole audience, not per reader: the digest
                # is one publication that many people receive, and a find that
                # reached half of them has been published.
                if not digest.record(conn, row, dbm.utcnow()):
                    continue
                delivered_to = 0
                for reader in audience:
                    async with Telegram(config.bot_token, reader["chat_id"], client) as tg:
                        if await tg.send_deal(caption, row["image_url"]):
                            delivered_to += 1
                        elif tg.chat_is_gone:
                            personal.deactivate(conn, reader["chat_id"])
                if delivered_to:
                    sent += 1
                else:
                    digest.unrecord(conn, row)
        return sent

    published = asyncio.run(go())
    print(f"опубликовано {published} из {len(cards)} для {len(audience)} читателей")
    return 0


def cmd_grant(args, config: Config) -> int:
    """Give or take access from the terminal, without touching SQL by hand.

    The reason this exists is a bug it prevents: comping somebody with
    `UPDATE bot_users SET paid_until = datetime('now','+30 days')` writes a
    timestamp in a shape nothing else in this project writes, and that one row
    used to be enough to stop the notification pass for every reader.
    """
    conn = dbm.connect(config.db_path)
    if args.revoke:
        try:
            taken = dbm.revoke(conn, args.user_id, force=args.force)
        except dbm.StillRecurring:
            print(
                f"у {args.user_id} активное автопродление: доступ вернётся при "
                f"следующем списании.\n"
                f"  вернуть деньги: pi subscriptions --refund {args.user_id}\n"
                f"  забрать всё равно: pi grant {args.user_id} --revoke --force",
                file=sys.stderr,
            )
            return 1
        if taken:
            print(f"доступ у {args.user_id} забран")
            return 0
        print(f"читателя {args.user_id} нет в базе", file=sys.stderr)
        return 1

    try:
        row = dbm.comp(conn, args.user_id, days=args.days)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    when = "без ограничения по времени" if args.days >= dbm.COMP_DAYS else (
        f"до {row['paid_until'][:10]}"
    )
    print(f"доступ выдан {args.user_id} — {when}")
    return 0


def cmd_subscriptions(args, config: Config) -> int:
    """Remind, expire, and refund. Run daily by a timer.

    The bot cannot do this itself: it only wakes when somebody writes to it, and
    a subscription ending is precisely the event nobody writes about.
    """
    conn = dbm.connect(config.db_path)

    if args.refund:
        row = dbm.get_bot_user(conn, args.refund)
        if row is None or not row["charge_id"]:
            print(f"нет платежа, который можно вернуть у {args.refund}", file=sys.stderr)
            return 1

        async def refund() -> bool:
            async with httpx.AsyncClient(timeout=30) as client:
                base = f"https://api.telegram.org/bot{config.bot_token}"

                async def call(method: str, payload: dict) -> tuple[bool, str]:
                    resp = await client.post(f"{base}/{method}", json=payload)
                    body = resp.json()
                    return bool(body.get("ok")), str(body.get("description") or "")

                # Stop the renewal first. Refunding a subscription without
                # cancelling it gives the money back and leaves the charge armed:
                # thirty days later Telegram fires it, the bot grants a fresh
                # month, and the refunded reader is back on the paid shelf. It is
                # possible Telegram cancels on refund by itself — this must not
                # be the code that depends on finding out.
                if row["sub_charge_id"]:
                    ok, why = await call("editUserStarSubscription", {
                        "user_id": args.refund,
                        "telegram_payment_charge_id": row["sub_charge_id"],
                        "is_canceled": True,
                    })
                    if not ok:
                        # A warning, not a stop. The commonest reason this fails
                        # is that the reader already pressed /cancel, and
                        # refusing to give their money back over a renewal that
                        # is already stopped leaves the operator with nothing to
                        # do but edit the row by hand — which is the habit the
                        # rest of this module exists to remove.
                        print(
                            f"продление остановить не удалось ({why}); "
                            "возможно, оно уже отменено — возвращаю деньги",
                            file=sys.stderr,
                        )

                ok, why = await call("refundStarPayment", {
                    "user_id": args.refund,
                    "telegram_payment_charge_id": row["charge_id"],
                })
                if not ok:
                    print(f"Telegram отказал: {why}", file=sys.stderr)
                return ok

        if not args.stars:
            print(
                "⚠️  --stars не задан: возвращённые звёзды останутся в счётчике "
                "выручки. Число есть в уведомлении Telegram о возврате.",
                file=sys.stderr,
            )
        if not asyncio.run(refund()):
            return 1
        # The subscription goes with the money, and so does the record of it:
        # a charge id left standing gets refunded twice by the next operator,
        # and stars that came back must stop counting as revenue.
        conn.execute(
            """
            UPDATE bot_users
               SET plan = 'free', paid_until = NULL,
                   charge_id = NULL, sub_charge_id = NULL,
                   stars_paid = MAX(0, stars_paid - ?)
             WHERE id = ?
            """,
            (int(args.stars or 0), args.refund),
        )
        print(f"возврат проведён, подписка {args.refund} снята")
        return 0

    due = dbm.expiring_soon(conn, within_days=args.remind_days)
    if args.dry_run:
        # Nothing is written, including the expiry sweep. "list them, send
        # nothing" that moves rows is a dry run in name only.
        print(f"напомнить: {len(due)} (ничего не записано и не отправлено)")
        for row in due:
            print(f"  {row['id']} ({row['username'] or row['chat_id']}) до {row['paid_until']}")
        return 0

    expired = dbm.expire_due(conn)
    print(f"напомнить: {len(due)} · вернулось на бесплатный: {expired}")
    if not due:
        return 0
    if not config.telegram_ready:
        print("TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return 1

    async def remind() -> int:
        sent = 0
        async with httpx.AsyncClient(timeout=30) as client:
            for row in due:
                async with Telegram(config.bot_token, row["chat_id"], client) as tg:
                    if await tg.send_text(
                        "⏳ Подписка заканчивается "
                        f"<b>{row['paid_until'][:10]}</b>.\n\n"
                        "Если продление настроено, Telegram спишет звёзды сам. "
                        "Если нет — оформить можно командой /subscription.\n\n"
                        "После окончания останутся две находки в день; профиль "
                        "и отмеченное никуда не денутся."
                    ):
                        sent += 1
                    elif tg.chat_is_gone:
                        personal.deactivate(conn, row["chat_id"])
        return sent

    print(f"отправлено напоминаний: {asyncio.run(remind())}")
    return 0



def cmd_reshelve(args, config: Config) -> int:
    with dbm.collector_lock(config.db_path, MAINTENANCE_WAIT_SECONDS) as held:
        if not held:
            _stood_aside("pi reshelve")
            return 1
        return _cmd_reshelve(args, config)


def _cmd_reshelve(args, config: Config) -> int:
    """Rebuild the shelf from the database: no shop is read, nothing is sent."""
    conn = dbm.connect(config.db_path)
    domains = tuple(d.strip() for d in args.stores.split(",")) if args.stores else ()
    written, withdrawn = pipeline.reshelve(conn, config, domains)
    total = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    print(f"на полке: {written:,} записано, {withdrawn:,} снято; всего предложений {total:,}")
    return 0


def cmd_backup(args, config: Config) -> int:
    conn = dbm.connect(config.db_path)
    dest = Path(args.out) if args.out else config.db_path.with_name("pi-snapshot.db")
    size = dbm.snapshot(conn, dest)
    print(f"снимок базы: {dest} ({size / 1e6:,.0f} MB)")
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
    p.add_argument(
        "--broken", action="store_true",
        help="re-probe every store that is not producing data, whatever the reason",
    )
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
    p.add_argument(
        "--verify-budget", type=int, default=pipeline.VERIFY_BUDGET,
        help=f"cards to open one by one to check they still exist "
             f"(default {pipeline.VERIFY_BUDGET}, 0 to skip)",
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser(
        "verify",
        help="open the oldest cards on the shelf one by one and drop the dead ones",
    )
    p.add_argument(
        "--limit", type=int, default=pipeline.VERIFY_BUDGET,
        help=f"how many cards to check (default {pipeline.VERIFY_BUDGET})",
    )
    p.add_argument("--dry-run", action="store_true", help="print the messages instead of sending")
    p.set_defaults(func=cmd_verify)

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
    p.add_argument(
        "--vacuum", action="store_true",
        help="shrink the file afterwards; holds the database for the better part of an "
             "hour, so stop the services first",
    )
    # Kept so an old unit or script that passes it still runs; it is the default now.
    p.add_argument("--no-vacuum", action="store_true", help=argparse.SUPPRESS)
    p.add_argument(
        "--delisted-grace-days", type=int,
        help="delete products their shop stopped listing this many days ago "
             "(default: delisted_grace_days in filters.toml)",
    )
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser(
        "reshelve",
        help="rebuild the shelf from the database without reading any shop or "
             "sending anything (after a repair, or a change to how deals are judged)",
    )
    p.add_argument("--stores", help="comma-separated domains: only their part of the shelf")
    p.set_defaults(func=cmd_reshelve)

    p = sub.add_parser(
        "backup",
        help="write a consistent copy of the database for the backup to pick up",
    )
    p.add_argument("--out", help="where to write it (default: data/pi-snapshot.db)")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("grant", help="give or take access without anybody paying")
    p.add_argument("user_id", type=int, help="Telegram user id")
    p.add_argument("--days", type=int, default=dbm.COMP_DAYS,
                   help="how long for (default: no time limit)")
    p.add_argument("--revoke", action="store_true", help="take it away instead")
    p.add_argument("--force", action="store_true",
                   help="with --revoke: take access even from a live recurring "
                        "subscription (it will come back on the next charge)")
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser(
        "subscriptions", help="remind before a subscription ends, expire the lapsed"
    )
    p.add_argument("--remind-days", type=int, default=3,
                   help="how far ahead to remind (default 3)")
    p.add_argument("--dry-run", action="store_true", help="list them, send nothing")
    p.add_argument("--refund", type=int, metavar="USER_ID",
                   help="refund this reader's last payment and drop their subscription")
    p.add_argument("--stars", type=int, default=0, metavar="N",
                   help="with --refund: how many stars went back, so the revenue "
                        "line stops counting them (Telegram's refund notice says)")
    p.set_defaults(func=cmd_subscriptions)

    p = sub.add_parser("digest", help="send the free tier's finds of the day")
    p.add_argument("--count", type=int, default=2, help="how many finds (default 2)")
    p.add_argument("--dry-run", action="store_true", help="print them instead of sending")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser(
        "reindex",
        help="rebuild the article numbers products are matched by between shops",
    )
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser(
        "reclassify",
        help="re-derive brand, gender, kind and audience for every product",
    )
    p.set_defaults(func=cmd_reclassify)

    p = sub.add_parser(
        "find", help="search the database for what is discounted right now"
    )
    p.add_argument("--brand", help="comma-separated, matched as substrings")
    p.add_argument("--size", help="comma-separated, e.g. US10,EU44")
    p.add_argument("--any-size", action="store_true", help="ignore the size filter entirely")
    p.add_argument(
        "--kids", action="store_true",
        help="include children's clothing, which is left out by default",
    )
    p.add_argument(
        "--women", action="store_true",
        help="include women's, which this shop does not stock — for checking a"
             " misreading, since one nobody can see is one nobody can report",
    )
    p.add_argument("--shop", help="only this shop's domain")
    p.add_argument("--min-discount", type=float, metavar="PCT")
    p.add_argument("--min-price", type=float, metavar="USD")
    p.add_argument("--max-price", type=float, metavar="USD")
    p.add_argument("--min-saving", type=float, metavar="USD")
    p.add_argument("--min-score", type=int)
    p.add_argument("--limit", type=int, default=25, help="rows to print (default 25)")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser(
        "price", help="what every shop charges for one article or name",
    )
    p.add_argument("query", help="article number (CW2288-111) or a name")
    p.add_argument("--limit", type=int, default=25, help="rows to print (default 25)")
    p.set_defaults(func=cmd_price)

    p = sub.add_parser("bot", help="serve the Telegram bot (runs until stopped)")
    p.set_defaults(func=cmd_bot)

    p = sub.add_parser("web", help="browse the shelf in a browser")
    p.add_argument("--host", default="127.0.0.1", help="default: this machine only")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--owner",
        type=int,
        metavar="TELEGRAM_USER_ID",
        help="be this reader without Telegram, for favourites while debugging",
    )
    p.set_defaults(func=cmd_web)

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
