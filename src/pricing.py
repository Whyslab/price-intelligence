"""
Price Sanity Layer + исторические метрики + объяснимость.
"""
import statistics

from src.robust_statistics import remove_outliers_mad

MAX_PRICE = 20000.0
OUTLIER_RATIO = 3.0

def sanitize_prices(prices: list) -> list:
    """Убирает невозможные цены и выбросы."""
    valid = [p for p in prices if 0 < p <= MAX_PRICE]
    if len(valid) < 2:
        return valid
    med = statistics.median(valid)
    if med <= 0:
        return valid
    clean = [p for p in prices if med / OUTLIER_RATIO <= p <= med * OUTLIER_RATIO]
    return clean if clean else valid

def deal_metrics(prices: list, best_price: float,
                 history_prices: list = None, old_price: float = None) -> dict:
    """
    Метрики сделки.
    prices         — текущие цены всех магазинов
    best_price     — лучшая доступная цена
    history_prices — все исторические наблюдения цены
    old_price      — зачёркнутая цена магазина (для детекции fake discount)
    """
    if not best_price or best_price <= 0 or best_price > MAX_PRICE:
        return None

    clean = sanitize_prices(prices)
    if not clean:
        return None

    med = statistics.median(clean)
    # P1-30: Проверяем через MAD
    clean_prices = remove_outliers_mad([float(p) for p in prices if p and p > 0], threshold=3.5)
    if best_price not in clean_prices:
        return None

    discount_pct = ((med - best_price) / med) * 100 if med > 0 else 0
    score_cross = min(100, max(0, discount_pct * 2))

    hist = None
    fake_discount = False
    reasons = []

    # === ИСТОРИЧЕСКИЙ СЛОЙ ===
    if history_prices:
        hclean = sanitize_prices(history_prices)
        if len(hclean) >= 3:
            h_med = statistics.median(hclean)
            h_low = min(hclean)
            h_high = max(hclean)
            percentile = (sum(1 for p in hclean if p <= best_price) / len(hclean)) * 100
            hist_discount = ((h_med - best_price) / h_med) * 100 if h_med > 0 else 0
            score_hist = min(100, max(0, hist_discount * 2))

            # Deal Score = 50% рынок сейчас + 50% история
            deal_score = int(0.5 * score_cross + 0.5 * score_hist)

            # FAKE DISCOUNT: магазин завысил old_price относительно реальной истории
            if old_price and old_price > h_med * 1.15 and best_price >= h_med * 0.9:
                fake_discount = True

            hist = {
                'median': h_med, 'low': h_low, 'high': h_high,
                'percentile': percentile, 'discount_pct': hist_discount,
            }
            if hist_discount >= 15:
                reasons.append(f"{hist_discount:.0f}% ниже исторической медианы")
            if percentile <= 15:
                reasons.append(f"цена в нижних {percentile:.0f}% всех наблюдений")
        else:
            deal_score = int(score_cross)
    else:
        deal_score = int(score_cross)

    if discount_pct >= 15:
        reasons.append(f"{discount_pct:.0f}% ниже текущей медианы рынка")
    if fake_discount:
        reasons.append("⚠️ 'старая цена' завышена относительно истории (fake discount)")
    if not reasons:
        reasons.append("цена близка к рынку — реальной выгоды нет")

    if deal_score >= 80:
        classification, color = "🔥 VERY GOOD", "red"
    elif deal_score >= 60:
        classification, color = "🟢 GOOD", "green"
    elif deal_score >= 40:
        classification, color = "🟡 NORMAL", "yellow"
    else:
        classification, color = "🔴 BAD", "gray"

    return {
        'market_median': med,
        'discount_pct': discount_pct,
        'deal_score': deal_score,
        'classification': classification,
        'color': color,
        'outliers_removed': len(prices) - len(clean),
        'history': hist,
        'fake_discount': fake_discount,
        'reasons': reasons,
    }
