# Struck-through prices: what 12.8 million price points show

*Data: 310 sneaker and streetwear shops, 34 days (24 Aug – 27 Sep 2026). Everything here is
reproducible with [`analysis/discounts.py`](../analysis/discounts.py); the numbers below are
copied from [`analysis/results/summary.json`](../analysis/results/summary.json) and the
per-shop table is [`stores.csv`](../analysis/results/stores.csv).*

## The question

A shop shows `$164` next to a struck-through `$301`. Was it ever `$301`?

Price Intelligence records every price a shop shows, so it can ask that question of the data
instead of the label. A "was" price is **seen** if the same variant was charged at least 95% of
it at some point in the data. Otherwise it is **unseen**. *Unseen is not the same as false*: the
number may be a manufacturer's list price, or the shop may have charged it before the first
observation. The point of the analysis is to find out how far that caveat stretches.

## Two kinds of discount, and the label does not say which

> 1,335,601 variants wore a discount of 10% or more. For 90.9% of them the "was" price was
> never charged in the data.

That headline number has an obvious weakness: a tag that was already on the product at our first
look can never be checked against a price from before the window. So the same data is cut four
ways, each answering the weakness of the last.

| Cut | Variants | Shops | "Was" price unseen | What it can and cannot say |
|---|---:|---:|---:|---|
| All discounted variants | 1,335,601 | 100 | 90.9% | Biggest sample; the past before day one is invisible |
| Watched ≥ 28 days | 813,930 | 93 | 90.2% | From the first point to the last time the product was seen |
| **First seen without a tag** | 84,402 | 58 | **8.7%** | The price before the tag was observed: the cleanest test |
| **Tag on at first sight, watched ≥ 28 days** | 767,611 | 92 | **94.8%** | The tag was already there and the price never reached it |

The two bold rows tell the story, and they point in opposite directions.

* **When a tag appears on a product we had seen at full price, the "was" price usually matches a
  price the shop really charged.** That holds in 91.3% of 84,402 cases. Read it with care: the cut
  is thin and lumpy. It rests on 58 shops, one shop (shop.simon.com) supplies 39% of it and the
  top three 57%; 30 of the 87 shops analysed below have none at all. A shop that lists
  every product with a tag already on cannot appear here by construction. So this says that *sales
  that start during the window* mostly reference a real earlier price, not that shops are honest.
* **When a tag is already there on first sight and the product is watched for four weeks, the
  price reaches it only 5.2% of the time** (767,611 variants, 92 shops, the three largest 25%).
  That is a standing markdown, or a list price used as a reference, not a sale. Both are common and in some jurisdictions legal.
  The label looks the same either way, and it does not say which one you are looking at.

The second cut is the better supported of the two: it is spread over 92 shops, and no three of
them make up more than a quarter of it.

## Round percentages are not the evidence

Some shops show nothing but 20%, 30%, 40%, 50%, 60% off. That looks like a rule — *"−40% on
everything"*.

![Where the discount percentages fall](../analysis/results/discount_distribution.png)

But a real "30% off sitewide" sale is round too, and its "was" price is genuine. Round alone
proves nothing. Round **and** unseen together fits a percentage markdown applied to a price that
was never charged, though the data cannot say which way the arithmetic ran: a price set to
*list × 0.6* gives the same round percentage and the same unseen "was". So each shop with at least
200 discounted variants (87 of them) is placed on two axes and given a neutral label:

![Round discounts against unseen was-prices, per shop](../analysis/results/round_vs_unsupported.png)

| Kind of shop | Shops | Discounted variants |
|---|---:|---:|
| `round_unseen` — round steps, "was" never charged | 32 | 350,738 |
| `irregular_unseen` — irregular percentages, "was" never charged | 42 | 748,072 |
| `round_seen` — round steps, "was" was charged | 2 | 21,418 |
| `irregular_seen` — irregular percentages, "was" was charged | 1 | 1,261 |
| `mixed` — in between | 10 | 213,284 |

The median shop has 98% of its "was" prices unseen and 53% of its discounts on a round step. Two
shops sit at the round-and-seen corner: a real promotion looks round but passes the second test,
so the pair of tests can tell the two cases apart. Two is a small number, and I would not build
on it. Shops in the `round_unseen` group that do have tags appearing mid-window mostly pass the
fresh-tag test (`fresh_unsupported` in `stores.csv`), so for them "round and unseen" describes a
standing markdown, not forged sale prices. The control window — the same width placed half a step
away — catches about 1% of discounts for the median shop, so round steps are not a chance
pattern.

## Prices move less than the shelf suggests

73% of variants were read exactly once: their price did not change in 34 days, so the shop
never wrote a second point (3,423,704 of about 4.7 million). Of the 851,027 with three or more
points, 170,212 moved by more than 5% and 114,707 by more than 20%. Most of the catalogue is
standing still; a small part moves a lot.

## About the shop names

`stores.csv` lists shops by domain because the figures are only checkable that way. Each row says
what a price series showed over 34 days: whether a shown "was" price was ever charged, and how
round the percentages were. It does not say why, and "unseen" is not "false": a manufacturer's
list price is a legitimate reference in many places. The labels are descriptive on purpose. If
you run a listed shop and want a row corrected or removed, open an issue.

## What I could not conclude

* **Cross-shop price spreads.** 23,864 articles (by manufacturer style code) appear in two or
  more shops, 11,191 in three or more, so a comparison is possible. My first pass found a median
  gap of 1.7× between the cheapest and dearest shop. It collapsed once I excluded pre-owned
  pairs, resale prices, stale rows and different sizes, leaving 30 comparable articles — too few
  to say anything. The analysis reports coverage only.
* **Anything beyond 34 days.** The window is short, and a longer one would turn "unseen" into a
  firmer statement for the standing tags.
* **Intent.** A number never charged is a fact about the data. Why the shop shows it is not.
* **Norway.** No shop here is Norwegian. Nine of them quote NOK to a Norwegian visitor, but they
  are foreign shops with foreign delivery and duty; the prices a Norwegian pays are different.

## How it was checked

* The unseen test ran against the shop's own currency, not dollars, so exchange-rate ticks cannot
  move a price (the collector avoids the same trap, see `db.record_price`). A shop that switches
  currency is not allowed to vouch for its own tag across the switch.
* A discount counts only from 10% up. A 3% tag is "seen" by the price itself, which made the test
  meaningless for small ones.
* A code review caught a mistake in the first version: the "watched ≥ 28 days" window was measured
  from the first to the *last price change*, but a price point is written only when something
  changes, so it measured how long a price kept changing. The window now ends at the time the
  product was last seen. The cut grew from 82,589 to 813,930 variants and the headline moved by a
  few points; the "tag on at first sight" cut moved from 74% to 95% unseen.
* I read raw histories for a few "unseen" variants. One example: an adidas model shown at €150 for
  the full 32 days while its price went €90 → €83. The test is doing what it says.
* The first cut of the cross-shop comparison was wrong, and I found that by opening the top
  entries and seeing pre-owned Travis Scott pairs next to retail ones.
* The same script runs on a synthetic database with four planted shop behaviours and recovers
  all four (`tests/test_analysis.py`).
