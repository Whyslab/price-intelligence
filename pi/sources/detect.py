"""Work out how each store can be read, and record it.

The previous incarnation of this project kept a hand-maintained list of "Shopify
sites". It went stale: shops migrate. Overkill and Footpatrol were both listed
as unreadable behind anti-bot while their /products.json had been open for
months. So the platform is probed rather than remembered, and every store ends
up in one of four states:

    shopify  — /products.json answers with a catalogue
    jsonld   — the storefront loads and carries schema.org markup
    blocked  — the site answers, but not to us (Cloudflare, Kasada, 403, JS challenge)
    tls      — the host is up but its certificate does not validate
    dead     — the domain does not resolve or never answers

These are kept apart on purpose: each has a different remedy, and each belongs
in the health report instead of silently returning zero products. A broken
certificate in particular is not a dead shop — it is usually fixed within days.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

import httpx

from ..db import upsert_store, utcnow
from ..throttle import NullLimiter, RateLimiter
from ..tls import context_with, repair
from . import impersonate, jsonld, shopify

log = logging.getLogger(__name__)

# 202 from a storefront is almost always a JavaScript bot challenge, not content.
BLOCKED_CODES = {202, 401, 402, 403, 405, 406, 423, 429, 503}
# Failures that say "not right now" rather than "not ever". These never replace
# a platform we have already seen working.
TRANSIENT_CODES = {429, 503}
WORKING = ("shopify", "jsonld")
# How many candidate product pages a probe may open before giving up on a shop.
PRODUCT_PROBE_PAGES = 3


async def probe(
    client: httpx.AsyncClient,
    domain: str,
    limiter: RateLimiter | NullLimiter | None = None,
    ca_cache: Path | None = None,
    allow_impersonation: bool = True,
) -> dict:
    """Classify a single domain. Never raises."""
    limiter = limiter or NullLimiter()
    base = f"https://{domain}".rstrip("/")
    out: dict = {"platform": "unknown", "currency": None, "name": None, "country": None}

    # 1. Shopify? The catalogue endpoint is the definitive test.
    try:
        async with limiter.slot(domain):
            resp = await client.get(f"{base}/products.json?limit=1", follow_redirects=True)
        if resp.status_code in TRANSIENT_CODES:
            await limiter.penalise(host=domain)
            return {**out, "platform": "throttled", "error": f"HTTP {resp.status_code}"}
        if resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict) and "products" in body:
                out["platform"] = "shopify"
                out["currency"] = await shopify.detect_currency(client, base, limiter)
                meta = await _meta(client, base)
                out["name"] = meta.get("name")
                out["country"] = meta.get("country")
                return out
    except (httpx.HTTPError, ValueError):
        pass

    # 2. Anything else that loads and publishes structured data.
    try:
        resp = await client.get(base, follow_redirects=True)
    except httpx.HTTPError as exc:
        detail = str(exc)
        # A certificate that does not validate is a live host with a fixable
        # problem, which is a different thing from a domain that has gone.
        if "CERTIFICATE_VERIFY_FAILED" in detail and ca_cache is not None:
            # A server that omits an intermediate is not untrustworthy, only
            # misconfigured — the certificate says where the missing link is, so
            # fetch it and ask again with verification still on. See pi.tls.
            verified, reason = await asyncio.to_thread(repair, domain, ca_cache)
            if verified:
                async with httpx.AsyncClient(
                    verify=context_with(ca_cache),
                    headers=client.headers,
                    timeout=client.timeout,
                    follow_redirects=True,
                ) as repaired:
                    return await probe(repaired, domain, limiter)
            out["platform"] = "tls"
            out["error"] = f"certificate: {reason}"
            return out
        out["platform"] = "tls" if "CERTIFICATE_VERIFY_FAILED" in detail else "dead"
        out["error"] = f"{type(exc).__name__}: {detail[:120]}"
        return out

    if resp.status_code in TRANSIENT_CODES:
        return {**out, "platform": "throttled", "error": f"HTTP {resp.status_code}"}
    if resp.status_code in BLOCKED_CODES:
        # What these sites match on is the TLS handshake, not the headers: sending
        # Chrome's User-Agent changes nothing, presenting Chrome's fingerprint
        # changes everything. Of the 25 shops that answer us 403, nine answer 200
        # this way and three go on to yield products.
        if allow_impersonation and impersonate.available():
            verdict = await _probe_as_a_browser(client, domain, limiter, ca_cache)
            if verdict is not None:
                return verdict
        out["platform"] = "blocked"
        out["error"] = f"HTTP {resp.status_code}"
        return out
    if resp.status_code != 200:
        out["platform"] = "dead"
        out["error"] = f"HTTP {resp.status_code}"
        return out

    if "application/ld+json" in resp.text:
        out["platform"] = "jsonld"
        return out

    # The homepage is the wrong place to look: schema.org/Product lives on the
    # product page. Deciding from the front page alone put 45 live stores under
    # one verdict, "no structured data on the storefront", which was true of the
    # storefront and told nobody anything about the shop. Two of the 45 turned
    # out to be readable; the rest render their prices in JavaScript, which the
    # error message now says instead of guessing. Costs a few requests per store,
    # once, and detection is not part of a collection run.
    why = "no product page was reachable"
    try:
        why = await jsonld.has_readable_products(client, base, tries=PRODUCT_PROBE_PAGES)
        if why is None:
            out["platform"] = "jsonld"
            return out
    except httpx.HTTPError as exc:
        log.debug("%s: product-page probe failed (%s)", base, exc)

    out["platform"] = "unknown"
    out["error"] = why
    return out


async def _probe_as_a_browser(
    client: httpx.AsyncClient,
    domain: str,
    limiter: RateLimiter | NullLimiter | None,
    ca_cache: Path | None,
) -> dict | None:
    """Re-probe with a browser's TLS fingerprint. None if that changes nothing."""
    try:
        async with impersonate.ImpersonatingClient(
            timeout=client.timeout.read or 30.0, headers=dict(client.headers)
        ) as browser:
            verdict = await probe(browser, domain, limiter, ca_cache, allow_impersonation=False)
    except Exception as exc:  # a shim over a C library; never take the run down
        log.debug("%s: impersonated probe failed (%s)", domain, exc)
        return None
    if verdict["platform"] not in WORKING:
        return None
    log.info("%-40s answers a browser fingerprint (%s)", domain, verdict["platform"])
    return {**verdict, "impersonate": 1}


async def _meta(client: httpx.AsyncClient, base: str) -> dict:
    try:
        resp = await client.get(f"{base}/meta.json")
        if resp.status_code == 200:
            data = resp.json()
            return {"name": data.get("name"), "country": data.get("country")}
    except (httpx.HTTPError, ValueError):
        pass
    return {}


async def detect_all(
    conn: sqlite3.Connection,
    domains: list[str],
    client: httpx.AsyncClient,
    concurrency: int = 8,
    limiter: RateLimiter | NullLimiter | None = None,
    ca_cache: Path | None = None,
) -> dict[str, int]:
    """Probe every domain and write the verdicts. Returns a platform tally."""
    limiter = limiter or NullLimiter()
    semaphore = asyncio.Semaphore(concurrency)
    known = {
        row["domain"]: row["platform"]
        for row in conn.execute("SELECT domain, platform FROM stores").fetchall()
    }
    tally: dict[str, int] = {}

    async def one(domain: str) -> tuple[str, dict]:
        async with semaphore:
            return domain, await probe(client, domain, limiter, ca_cache)

    for coro in asyncio.as_completed([one(d) for d in domains]):
        domain, verdict = await coro
        platform = verdict["platform"]

        # Never let a momentary throttle erase a platform we have seen working.
        if platform == "throttled":
            previous = known.get(domain, "unknown")
            upsert_store(
                conn, domain, last_checked=utcnow(),
                last_error=f"throttled while probing ({verdict.get('error')})",
            )
            tally["throttled"] = tally.get("throttled", 0) + 1
            log.warning("%-40s throttled — keeping %s", domain, previous)
            continue

        fields = {
            "platform": platform,
            "last_checked": utcnow(),
            "last_error": verdict.get("error"),
            "status": "ok" if platform in WORKING else "skipped",
        }
        for key in ("currency", "name", "country", "impersonate"):
            if verdict.get(key):
                fields[key] = verdict[key]
        upsert_store(conn, domain, **fields)
        tally[platform] = tally.get(platform, 0) + 1
        log.info("%-40s %s", domain, platform)
    return tally
