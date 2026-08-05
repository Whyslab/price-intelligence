"""
Фильтрует только Shopify сайты из результатов сканирования.
"""
import json

def filter_shopify_sites(input_file: str, output_file: str):
    with open(input_file, 'r') as f:
        results = json.load(f)
    
    shopify_sites = [
        {
            'url': r['url'],
            'notes': r['notes']
        }
        for r in results 
        if r['platform'] == 'shopify'
    ]
    
    # Сохраняем в JSON
    with open(output_file, 'w') as f:
        json.dump(shopify_sites, f, indent=2)
    
    # Сохраняем в простой текстовый список
    with open(output_file.replace('.json', '.txt'), 'w') as f:
        for site in shopify_sites:
            f.write(f"{site['url']}\n")
    
    print(f"✅ Found {len(shopify_sites)} Shopify sites")
    print(f"   Saved to {output_file}")
    
    # Статистика по доступности products.json
    available = sum(1 for s in shopify_sites if 'available' in s['notes'])
    blocked = sum(1 for s in shopify_sites if 'blocked' in s['notes'])
    
    print(f"\n📊 products.json availability:")
    print(f"   Available: {available}")
    print(f"   Blocked: {blocked}")

if __name__ == "__main__":
    filter_shopify_sites(
        input_file="platforms_scan.json",
        output_file="shopify_sites.json"
    )
