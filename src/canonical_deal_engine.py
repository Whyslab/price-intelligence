"""
Canonical Deal Score Engine - единый источник истины для Deal Score.
Все компоненты (Dashboard, Telegram, CLI) ДОЛЖНЫ использовать только эту функцию.
"""
from typing import Dict, List, Optional
from src.robust_statistics import robust_market_metrics, calculate_confidence


def calculate_canonical_deal_score(
    prices_data: List[Dict],
    historical_metrics: Optional[Dict] = None,
    conn=None
) -> Dict:
    """
    Canonical Deal Score.
    
    Args:
        prices_data: List of {"price": float, "store_id": int, "in_stock": bool}
    """
    if not prices_data:
        return {"deal_score": 0, "confidence": 0, "classification": "NO_DATA",
                "best_price": None, "market_median": 0, "discount_pct": 0}
    
    in_stock = [p["price"] for p in prices_data if p.get("in_stock", True) and p["price"] > 0]
    if not in_stock:
        return {"deal_score": 0, "confidence": 0, "classification": "OUT_OF_STOCK",
                "best_price": None, "market_median": 0, "discount_pct": 0}
    
    metrics = robust_market_metrics(in_stock)
    median = metrics.get("median", 0)
    best = min(in_stock)
    
    discount = ((median - best) / median * 100) if median > 0 else 0
    
    stores = len(set(p.get("store_id") for p in prices_data))
    conf = calculate_confidence(len(in_stock), stores, 0) * 100
    
    score = int(min(100, discount * 2))
    
    if score >= 80: cls = "EXCELLENT_DEAL"
    elif score >= 60: cls = "GOOD_DEAL"
    elif score >= 40: cls = "FAIR_PRICE"
    else: cls = "OVERPRICED"
    
    return {
        "deal_score": score, "confidence": int(conf),
        "classification": cls, "best_price": best,
        "market_median": median, "discount_pct": round(discount, 2),
        "num_stores": stores, "num_observations": len(in_stock)
    }


# Backward compat
calculate_deal_score_v2 = calculate_canonical_deal_score
calculate_deal_score = calculate_canonical_deal_score
