"""
Robust Statistics Module (P1-22, P1-30, P1-31): продвинутые статистические методы.

Заменяет примитивные эвристики на:
- True weighted percentile (использует weights)
- MAD (Median Absolute Deviation) для outlier detection
- IQR (Interquartile Range) для outlier detection
- Confidence scoring на основе наблюдений и variance
"""

import numpy as np


def weighted_percentile(values: list[float], weights: list[float], percentile: float) -> float:
    """
    P1-22: Настоящий weighted percentile.
    
    Args:
        values: список значений
        weights: список весов (например, days_at_price)
        percentile: процентиль (0.0 - 1.0)
    
    Returns:
        float: взвешенный процентиль
    
    Example:
        weighted_percentile([100, 150, 200], [10, 5, 3], 0.5)  # median
    """
    if not values or not weights or len(values) != len(weights):
        return float(np.median(values)) if values else 0.0
    
    # Сортируем по значениям
    sorted_pairs = sorted(zip(values, weights), key=lambda x: x[0])
    sorted_values, sorted_weights = zip(*sorted_pairs)
    
    total_weight = sum(sorted_weights)
    if total_weight == 0:
        return float(np.median(values))
    
    # Находим точку, где cumulative weight >= percentile * total_weight
    cumulative = 0
    target = percentile * total_weight
    
    for i, (val, weight) in enumerate(zip(sorted_values, sorted_weights)):
        cumulative += weight
        if cumulative >= target:
            # Интерполяция между текущим и предыдущим значением
            if i == 0:
                return float(val)
            prev_val = sorted_values[i - 1]
            prev_cumulative = cumulative - weight
            # Линейная интерполяция
            fraction = (target - prev_cumulative) / weight
            return float(prev_val + fraction * (val - prev_val))
    
    return float(sorted_values[-1])


def calculate_mad(values: list[float]) -> float:
    """
    P1-30: Median Absolute Deviation.
    
    MAD = median(|xi - median(x)|)
    
    Args:
        values: список значений
    
    Returns:
        float: MAD значение
    
    Example:
        calculate_mad([100, 110, 120, 1000])  # 1000 - outlier
    """
    if not values:
        return 0.0
    
    median_val = float(np.median(values))
    abs_deviations = [abs(v - median_val) for v in values]
    return float(np.median(abs_deviations))


def remove_outliers_mad(values: list[float], threshold: float = 3.5) -> list[float]:
    """
    P1-30: Удаление outliers через MAD.
    
    Outlier если: |x - median| / MAD > threshold
    
    Args:
        values: список значений
        threshold: порог (по умолчанию 3.5, соответствует ~99% для нормального распределения)
    
    Returns:
        List[float]: отфильтрованные значения
    """
    if len(values) < 3:
        return values
    
    median_val = float(np.median(values))
    mad = calculate_mad(values)
    
    if mad == 0:
        # Все значения одинаковые или только одно значение
        return values
    
    # Modified Z-score: (x - median) / (MAD * 1.4826)
    # 1.4826 - константа для согласования с стандартным отклонением
    modified_z_scores = [abs(v - median_val) / (mad * 1.4826) for v in values]
    
    return [v for v, z in zip(values, modified_z_scores) if z <= threshold]


def calculate_iqr(values: list[float]) -> tuple[float, float, float]:
    """
    P1-31: Interquartile Range.
    
    IQR = Q3 - Q1
    Outliers: < Q1 - 1.5*IQR или > Q3 + 1.5*IQR
    
    Args:
        values: список значений
    
    Returns:
        Tuple[float, float, float]: (Q1, Q3, IQR)
    """
    if not values:
        return (0.0, 0.0, 0.0)
    
    q1 = float(np.percentile(values, 25))
    q3 = float(np.percentile(values, 75))
    iqr = q3 - q1
    
    return (q1, q3, iqr)


def remove_outliers_iqr(values: list[float], multiplier: float = 1.5) -> list[float]:
    """
    P1-31: Удаление outliers через IQR.
    
    Args:
        values: список значений
        multiplier: множитель для IQR (по умолчанию 1.5, для экстремальных outliers используйте 3.0)
    
    Returns:
        List[float]: отфильтрованные значения
    """
    if len(values) < 4:
        return values
    
    q1, q3, iqr = calculate_iqr(values)
    
    lower_bound = q1 - multiplier * iqr
    upper_bound = q3 + multiplier * iqr
    
    return [v for v in values if lower_bound <= v <= upper_bound]


def calculate_confidence(
    num_observations: int,
    num_stores: int,
    variance: float,
    min_observations: int = 3,
    min_stores: int = 2
) -> float:
    """
    P1-29: Confidence scoring для market price.
    
    Args:
        num_observations: количество наблюдений (price changes)
        num_stores: количество уникальных магазинов
        variance: дисперсия цен
        min_observations: минимальное количество наблюдений для 100% confidence
        min_stores: минимальное количество магазинов для 100% confidence
    
    Returns:
        float: confidence score (0.0 - 1.0)
    """
    if num_observations < 1 or num_stores < 1:
        return 0.0
    
    # Observation confidence: логарифмическая шкала
    obs_confidence = min(1.0, np.log(num_observations + 1) / np.log(min_observations + 1))
    
    # Store diversity confidence
    store_confidence = min(1.0, num_stores / min_stores)
    
    # Variance penalty: высокая дисперсия снижает confidence
    # Нормализуем variance относительно медианы (если есть)
    variance_penalty = 1.0
    if variance > 0:
        # Если CV (coefficient of variation) > 0.3, снижаем confidence
        cv = variance  # предполагаем, что variance уже нормализован
        if cv > 0.3:
            variance_penalty = max(0.5, 1.0 - (cv - 0.3) * 2)
    
    # Взвешенная комбинация
    confidence = (obs_confidence * 0.4 + store_confidence * 0.4 + variance_penalty * 0.2)
    
    return float(confidence)


def robust_market_metrics(prices: list[float], weights: list[float] | None = None) -> dict:
    """
    Полная замена для calculate_market_metrics.
    
    Args:
        prices: список цен
        weights: опциональные веса (например, days_at_price)
    
    Returns:
        dict с robust метриками
    """
    if not prices:
        return {
            'median': 0,
            'percentile_10': 0,
            'percentile_90': 0,
            'min': 0,
            'max': 0,
            'mean': 0,
            'std': 0,
            'count': 0,
            'outliers_removed': 0,
            'confidence': 0
        }
    
    # Удаляем outliers через MAD (более robust чем IQR для малых выборок)
    clean_prices = remove_outliers_mad(prices, threshold=3.5)
    outliers_removed = len(prices) - len(clean_prices)
    
    if not clean_prices:
        # Все цены были outliers, используем исходные
        clean_prices = prices
        outliers_removed = 0
    
    # Если есть веса, используем weighted percentile
    if weights and len(weights) == len(prices):
        # Фильтруем веса вместе с ценами
        clean_weights = [w for p, w in zip(prices, weights) if p in clean_prices]
        
        median = weighted_percentile(clean_prices, clean_weights, 0.5)
        percentile_10 = weighted_percentile(clean_prices, clean_weights, 0.1)
        percentile_90 = weighted_percentile(clean_prices, clean_weights, 0.9)
    else:
        median = float(np.median(clean_prices))
        percentile_10 = float(np.percentile(clean_prices, 10))
        percentile_90 = float(np.percentile(clean_prices, 90))
    
    # Базовые статистики
    mean = float(np.mean(clean_prices))
    std = float(np.std(clean_prices)) if len(clean_prices) > 1 else 0.0
    
    # Confidence scoring
    num_stores = len(set(store_ids)) if 'store_ids' in locals() else len(set(prices))  # предполагаем, что каждая цена от уникального магазина
    cv = std / mean if mean > 0 else 0
    confidence = calculate_confidence(
        num_observations=len(prices),
        num_stores=num_stores,
        variance=cv
    )
    
    return {
        'median': median,
        'percentile_10': percentile_10,
        'percentile_90': percentile_90,
        'min': float(np.min(clean_prices)),
        'max': float(np.max(clean_prices)),
        'mean': mean,
        'std': std,
        'count': len(clean_prices),
        'outliers_removed': outliers_removed,
        'confidence': confidence
    }
