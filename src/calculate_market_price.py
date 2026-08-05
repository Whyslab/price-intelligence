"""
Market Price Engine: рассчитывает реальную рыночную цену и Deal Score
на основе сматченных товаров из разных магазинов.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from src.config import DATABASE_URL
from decimal import Decimal
import statistics

def get_matched_prices(db, canonical_variant_id: int) -> list:
    """
    Получает цены всех сматченных вариантов (включая canonical).
    Возвращает список словарей с ценами и магазинами.
    """
    result = db.execute(text("""
        SELECT 
            o.current_price,
            o.old_price,
            o.in_stock,
            s.name as store_name,
            p.canonical_name,
            pv.sku
        FROM offers o
        JOIN stores s ON o.store_id = s.id
        JOIN product_variants pv ON o.variant_id = pv.id
        JOIN products p ON pv.product_id = p.id
        WHERE o.variant_id = :canonical_id
        
        UNION
        
        SELECT 
            o.current_price,
            o.old_price,
            o.in_stock,
            s.name as store_name,
            p.canonical_name,
            pv.sku
        FROM offers o
        JOIN stores s ON o.store_id = s.id
        JOIN product_variants pv ON o.variant_id = pv.id
        JOIN products p ON pv.product_id = p.id
        JOIN product_matches pm ON o.variant_id = pm.matched_variant_id
        WHERE pm.canonical_variant_id = :canonical_id
    """), {'canonical_id': canonical_variant_id})
    
    return [
        {
            'price': float(row[0]),
            'old_price': float(row[1]) if row[1] else None,
            'in_stock': row[2],
            'store': row[3],
            'name': row[4],
            'sku': row[5]
        }
        for row in result.fetchall()
    ]

def calculate_market_metrics(prices: list) -> dict:
    """Рассчитывает метрики рынка: median, average, min, max."""
    if not prices:
        return {}
    
    current_prices = [p['price'] for p in prices]
    
    return {
        'market_median': statistics.median(current_prices),
        'market_average': statistics.mean(current_prices),
        'market_min': min(current_prices),
        'market_max': max(current_prices),
        'store_count': len(set(p['store'] for p in prices)),
        'prices': prices
    }

def calculate_deal_score(current_price: float, market_median: float, market_min: float) -> float:
    """
    Рассчитывает Deal Score (0-100).
    Чем ниже цена относительно рынка, тем выше скор.
    """
    if market_median == 0:
        return 0
    
    # Насколько текущая цена ниже медианы (в процентах)
    discount_vs_median = ((market_median - current_price) / market_median) * 100
    
    # Бонус если цена близка к минимуму
    proximity_to_min = ((current_price - market_min) / market_median) * 100 if market_min > 0 else 0
    
    # Базовый скор: чем больше скидка от медианы, тем лучше
    base_score = min(100, max(0, discount_vs_median * 2))
    
    # Штраф если цена сильно выше минимума
    penalty = min(20, proximity_to_min)
    
    return max(0, min(100, base_score - penalty))

def classify_deal(score: float) -> str:
    """Классифицирует сделку на основе Deal Score."""
    if score >= 80:
        return "🔥 VERY GOOD DEAL"
    elif score >= 60:
        return "🟢 GOOD DEAL"
    elif score >= 40:
        return "🟡 NORMAL PRICE"
    else:
        return "🔴 BAD DEAL"

def analyze_top_products():
    """Анализирует топ-10 сматченных товаров и выводит отчёт."""
    engine = create_engine(DATABASE_URL)
    
    with engine.connect() as conn:
        # Получаем топ-10 canonical вариантов с наибольшим количеством матчей
        result = conn.execute(text("""
            SELECT 
                pm.canonical_variant_id,
                pv.sku,
                p.canonical_name,
                COUNT(pm.matched_variant_id) + 1 as total_stores
            FROM product_matches pm
            JOIN product_variants pv ON pm.canonical_variant_id = pv.id
            JOIN products p ON pv.product_id = p.id
            GROUP BY pm.canonical_variant_id, pv.sku, p.canonical_name
            ORDER BY total_stores DESC
            LIMIT 10
        """))
        
        top_products = result.fetchall()
        
        print("="*80)
        print("📊 MARKET PRICE ANALYSIS — TOP MATCHED PRODUCTS")
        print("="*80)
        
        for row in top_products:
            canonical_id = row[0]
            sku = row[1]
            name = row[2]
            
            print(f"\n🏷️  {name}")
            print(f"   SKU: {sku}")
            
            # Получаем цены
            prices_data = get_matched_prices(conn, canonical_id)
            
            if not prices_data:
                print("   ⚠️  No price data available")
                continue
            
            # Рассчитываем метрики
            metrics = calculate_market_metrics(prices_data)
            
            # Находим текущую цену canonical варианта
            canonical_price_data = next((p for p in prices_data if True), None)  # Берём первую цену
            current_price = canonical_price_data['price']
            
            # Рассчитываем Deal Score
            deal_score = calculate_deal_score(
                current_price,
                metrics['market_median'],
                metrics['market_min']
            )
            
            deal_class = classify_deal(deal_score)
            
            print(f"\n   📈 Market Metrics:")
            print(f"      Median Price: ${metrics['market_median']:.2f}")
            print(f"      Average Price: ${metrics['market_average']:.2f}")
            print(f"      Min Price: ${metrics['market_min']:.2f}")
            print(f"      Max Price: ${metrics['market_max']:.2f}")
            print(f"      Stores: {metrics['store_count']}")
            
            print(f"\n   💰 Current Price: ${current_price:.2f}")
            
            discount_vs_median = ((metrics['market_median'] - current_price) / metrics['market_median']) * 100
            print(f"      vs Median: {discount_vs_median:+.1f}%")
            
            print(f"\n   🎯 Deal Score: {deal_score:.0f}/100")
            print(f"      {deal_class}")
            
            print(f"\n   🛒 Price by Store:")
            for p in sorted(prices_data, key=lambda x: x['price']):
                stock_icon = "✅" if p['in_stock'] else "❌"
                print(f"      {stock_icon} {p['store']}: ${p['price']:.2f}")
            
            print("-"*80)

if __name__ == "__main__":
    analyze_top_products()
