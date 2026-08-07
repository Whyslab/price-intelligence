# scripts/test_p1_23.py
def calculate_weighted_median(prices: list[dict]) -> float:
    valid_prices = [p for p in prices if p['price'] > 0 and p['weight'] > 0]
    if not valid_prices: return 0.0
        
    sorted_prices = sorted(valid_prices, key=lambda x: x['price'])
    total_weight = sum(p['weight'] for p in sorted_prices)
    target_weight = total_weight / 2.0
    
    cumulative_weight = 0.0
    for p in sorted_prices:
        cumulative_weight += p['weight']
        if cumulative_weight >= target_weight:
            return p['price']
    return sorted_prices[-1]['price']

test_data = [
    {"price": 150.0, "weight": 0.2}, 
    {"price": 100.0, "weight": 0.5}, 
    {"price": 120.0, "weight": 1.5}, 
    {"price": 130.0, "weight": 1.0}
]

median = calculate_weighted_median(test_data)
print(f"Calculated median: {median}")
assert median == 120.0, "Error: expected 120.0!"
print("✅ Python logic for P1-23 is correct")
