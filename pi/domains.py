"""Deciding when two hostnames are the same shop.

Two different questions wear the same name, and they want different answers.

*Is this a duplicate entry in the site list?* Only `www.` may be stripped here.
`www.smoothitalia.com` and `smoothitalia.com` are the same server by definition,
and the list held both — so did `sneakerjunkiesusa.com`, whose two spellings were
crawled separately into 9,868 and 9,812 near-identical products. Anything bolder
than `www.` risks throwing away a shop that genuinely lives on a subdomain.

*Are these two independent shops, for the purpose of believing them?* Here the
bolder form is the safe one. A price confirmed by `bdgastore.com` and
`shop.bdgastore.com` is one shop agreeing with itself; counting it twice would
manufacture corroboration. Over-merging only ever makes us ask for more evidence.
"""
from __future__ import annotations

# Stripped when asking "same server?" — unambiguous.
_WWW = "www."
# Also stripped when asking "same owner?" — a storefront on a subdomain of the
# same registered domain is the same merchant.
_STOREFRONT_PREFIXES = ("shop.", "store.", "us.", "eu.", "uk.", "it.", "de.", "fr.")


def same_host(domain: str) -> str:
    """Canonical spelling of one host: `www.` is decoration, nothing else is."""
    host = domain.strip().lower().rstrip(".")
    return host[len(_WWW):] if host.startswith(_WWW) else host


def same_shop(domain: str) -> str:
    """Canonical spelling of one *merchant*, for counting independent opinions."""
    host = same_host(domain)
    for prefix in _STOREFRONT_PREFIXES:
        if host.startswith(prefix):
            return host[len(prefix):]
    return host
