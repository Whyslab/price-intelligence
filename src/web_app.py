"""
Web Dashboard: FastAPI + история цен.
Запуск: python -m src.web_app
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL
from src.pricing import deal_metrics, MAX_PRICE
import statistics
from collections import defaultdict

app = FastAPI(title="Price Intelligence Dashboard")
templates = Jinja2Templates(directory="templates")

def get_db():
    return create_engine(DATABASE_URL).connect()

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, min_score: int = 40, q: str = ""):
    conn = get_db()
    query = """
        SELECT pm.canonical_variant_id, pv.sku, p.canonical_name, b.name,
               MIN(o.current_price), COUNT(DISTINCT o.store_id)
        FROM product_matches pm
        JOIN product_variants pv ON pm.canonical_variant_id = pv.id
        JOIN products p ON pv.product_id = p.id
        JOIN brands b ON p.brand_id = b.id
        JOIN offers o ON (o.variant_id = pm.canonical_variant_id OR o.variant_id = pm.matched_variant_id)
        WHERE o.in_stock = true
    """
    params = {}
    if q:
        query += " AND (p.canonical_name ILIKE :q OR pv.sku ILIKE :q OR b.name ILIKE :q)"
        params['q'] = f"%{q}%"
    query += " GROUP BY pm.canonical_variant_id, pv.sku, p.canonical_name, b.name"

    deals = []
    for row in conn.execute(text(query), params).fetchall():
        canon_id, sku, name, brand, best_price, store_count = row
        best_price = float(best_price)
        prices = [float(r[0]) for r in conn.execute(text("""
            SELECT DISTINCT o.current_price FROM offers o WHERE o.variant_id = :id
            UNION
            SELECT DISTINCT o.current_price FROM offers o
            JOIN product_matches pm ON o.variant_id = pm.matched_variant_id
            WHERE pm.canonical_variant_id = :id
        """), {'id': canon_id}).fetchall()]

        metrics = deal_metrics(prices, best_price)
        if not metrics or metrics['deal_score'] < min_score:
            continue
        deals.append({'id': canon_id, 'sku': sku, 'name': name, 'brand': brand,
                      'best_price': best_price, 'store_count': store_count, **metrics})

    deals.sort(key=lambda x: (x['deal_score'], x['discount_pct']), reverse=True)
    deals = deals[:50]

    stats = conn.execute(text("""
        SELECT (SELECT COUNT(*) FROM stores), (SELECT COUNT(*) FROM products),
               (SELECT COUNT(*) FROM product_variants), (SELECT COUNT(*) FROM product_matches)
    """)).fetchone()
    conn.close()

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "deals": deals,
        "stats": {'stores': stats[0], 'products': stats[1], 'variants': stats[2], 'matches': stats[3]},
        "min_score": min_score, "q": q
    })

@app.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(request: Request, product_id: int):
    conn = get_db()

    product_info = conn.execute(text("""
        SELECT pv.sku, p.canonical_name, b.name
        FROM product_variants pv
        JOIN products p ON pv.product_id = p.id
        JOIN brands b ON p.brand_id = b.id
        WHERE pv.id = :id
    """), {'id': product_id}).fetchone()
    if not product_info:
        conn.close()
        raise HTTPException(status_code=404, detail="Product not found")

    offers = []
    prices = []
    for row in conn.execute(text("""
        SELECT o.current_price, o.old_price, o.in_stock, s.name, o.url
        FROM offers o JOIN stores s ON o.store_id = s.id WHERE o.variant_id = :id
        UNION
        SELECT o.current_price, o.old_price, o.in_stock, s.name, o.url
        FROM offers o JOIN stores s ON o.store_id = s.id
        JOIN product_matches pm ON o.variant_id = pm.matched_variant_id
        WHERE pm.canonical_variant_id = :id
    """), {'id': product_id}).fetchall():
        price = float(row[0])
        prices.append(price)
        offers.append({'price': price, 'old_price': float(row[1]) if row[1] else None,
                       'in_stock': row[2], 'store': row[3], 'url': row[4]})
    offers.sort(key=lambda x: x['price'])

    # === ИСТОРИЯ ЦЕН ===
    history_rows = conn.execute(text("""
        SELECT ph.timestamp, ph.price, s.name FROM price_history ph
        JOIN stores s ON ph.store_id = s.id WHERE ph.variant_id = :id
        UNION ALL
        SELECT ph.timestamp, ph.price, s.name FROM price_history ph
        JOIN stores s ON ph.store_id = s.id
        JOIN product_matches pm ON ph.variant_id = pm.matched_variant_id
        WHERE pm.canonical_variant_id = :id
        ORDER BY 1
    """), {'id': product_id}).fetchall()

    series = defaultdict(dict)
    hist_prices = []
    for ts, price, store in history_rows:
        p = float(price)
        if 0 < p <= MAX_PRICE:
            series[store][ts] = p
            hist_prices.append(p)

    labels = sorted({ts for s in series.values() for ts in s.keys()})
    chart_labels = [ts.strftime('%m-%d %H:%M') for ts in labels]
    chart_datasets = [
        {'store': store, 'data': [points.get(ts) for ts in labels]}
        for store, points in series.items()
    ]

    best = offers[0] if offers else None
    metrics = deal_metrics(
        prices, best['price'] if best else 0,
        history_prices=hist_prices,
        old_price=best['old_price'] if best else None
    )

    conn.close()

    return templates.TemplateResponse(request=request, name="product.html", context={
        "product": {'id': product_id, 'sku': product_info[0], 'name': product_info[1], 'brand': product_info[2]},
        "offers": offers,
        "metrics": metrics,
        "stats": {
            'market_median': statistics.median(prices) if prices else 0,
            'market_min': min(prices) if prices else 0,
            'market_max': max(prices) if prices else 0,
            'store_count': len(set(o['store'] for o in offers)),
            'hist_points': len(hist_prices),
        },
        "chart": {'labels': chart_labels, 'datasets': chart_datasets},
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
