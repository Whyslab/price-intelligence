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

import httpx

from ..db import upsert_store, utcnow
from . import shopify

log = logging.getLogger(__name__)

# 202 from a storefront is almost always a JavaScript bot challenge, not content.
BLOCKED_CODES = {202, 401, 402, 403, 405, 406, 423, 429, 503}


async def probe(client: httpx.AsyncClient, domain: str) -> dict:
    """Classify a single domain. Never raises."""
    base = f"https://{domain}".rstrip("/")
    out: dict = {"platform": "unknown", "currency": None, "name": None, "country": None}

    # 1. Shopify? The catalogue endpoint is the definitive test.
    try:
        resp = await client.get(f"{base}/products.json?limit=1", follow_redirects=True)
        if resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict) and "products" in body:
                out["platform"] = "shopify"
                out["currency"] = await shopify.detect_currency(client, base)
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
        out["platform"] = "tls" if "CERTIFICATE_VERIFY_FAILED" in detail else "dead"
        out["error"] = f"{type(exc).__name__}: {detail[:120]}"
        return out

    if resp.status_code in BLOCKED_CODES:
        out["platform"] = "blocked"
        out["error"] = f"HTTP {resp.status_code}"
        return out
    if resp.status_code != 200:
        out["platform"] = "dead"
        out["error"] = f"HTTP {resp.status_code}"
        return out

    if "application/ld+json" in resp.text:
        out["platform"] = "jsonld"
    else:
        out["platform"] = "unknown"
        out["error"] = "no structured data on the storefront"
    return out


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
    concurrency: int = 16,
) -> dict[str, int]:
    """Probe every domain and write the verdicts. Returns a platform tally."""
    semaphore = asyncio.Semaphore(concurrency)
    tally: dict[str, int] = {}

    async def one(domain: str) -> tuple[str, dict]:
        async with semaphore:
            return domain, await probe(client, domain)

    for coro in asyncio.as_completed([one(d) for d in domains]):
        domain, verdict = await coro
        fields = {
            "platform": verdict["platform"],
            "last_checked": utcnow(),
            "last_error": verdict.get("error"),
            "status": "ok" if verdict["platform"] in ("shopify", "jsonld") else "skipped",
        }
        for key in ("currency", "name", "country"):
            if verdict.get(key):
                fields[key] = verdict[key]
        upsert_store(conn, domain, **fields)
        tally[verdict["platform"]] = tally.get(verdict["platform"], 0) + 1
        log.info("%-40s %s", domain, verdict["platform"])
    return tally
