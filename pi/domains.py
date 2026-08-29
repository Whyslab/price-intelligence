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

A chain's national sites are the same shop for that purpose, and this is where
it bites hardest. Foot Locker sells in four of the countries on the list;
counting .com, .de, .fr and .it separately would let one retailer's own pricing
department satisfy both `market_min_shops = 2` and `msrp_min_shops = 3` on its
own, and the corroborated market price — the whole reason for asking several
shops — would be one shop repeated. So the country is dropped along with the
subdomain. Measured over the 284 shops on the list, that merges exactly three
pairs — jdsports .com/.co.uk, snipes .com/.pl, superga .com/.co.uk — and every
one of them is genuinely one retailer, so nothing is lost by it.
"""
from __future__ import annotations

# Stripped when asking "same server?" — unambiguous.
_WWW = "www."
# Also stripped when asking "same owner?" — a storefront on a subdomain of the
# same registered domain is the same merchant, and so is its mobile site.
_STOREFRONT_PREFIXES = (
    "shop.", "store.", "us.", "eu.", "uk.", "it.", "de.", "fr.", "m.",
)
# Suffixes that are two labels long, so that size.co.uk yields "size" rather
# than "size.co". Only the ones the list actually uses; an unknown one costs a
# label too few, which merges nothing that was not already merged.
_TWO_LABEL_SUFFIXES = frozenset({
    "co.uk", "co.jp", "co.kr", "co.nz", "co.za", "com.au", "com.br", "com.cn",
    "com.hk", "com.mx", "com.pl", "com.sg", "com.tr", "com.ua",
})
# Chains that trade under a different second-level name in another market, where
# dropping the country is not enough. Kept explicit and short: a guess here
# either invents corroboration or destroys it, and neither is worth saving a
# line. Written as merchant-name -> canonical merchant-name.
_ALIASES = {
    "snipesusa": "snipes",
}


def same_host(domain: str) -> str:
    """Canonical spelling of one host: `www.` is decoration, nothing else is."""
    host = domain.strip().lower().rstrip(".")
    return host[len(_WWW):] if host.startswith(_WWW) else host


def same_shop(domain: str) -> str:
    """Canonical spelling of one *merchant*, for counting independent opinions.

    The result is a name rather than a hostname — footlocker, not
    footlocker.com — because it is only ever used as a grouping key, and the
    country is precisely what has to stop distinguishing them.
    """
    host = same_host(domain)
    for prefix in _STOREFRONT_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    labels = host.split(".")
    if len(labels) > 2 and ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES:
        name = ".".join(labels[:-2])
    elif len(labels) > 1:
        name = ".".join(labels[:-1])
    else:
        name = host
    return _ALIASES.get(name, name)
