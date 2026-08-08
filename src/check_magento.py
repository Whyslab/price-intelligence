"""
Проверяет доступность Magento API (REST/GraphQL) для 81 сайта.
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


def check_magento_api(url: str) -> dict:
    """Проверяет REST и GraphQL endpoints."""
    result = {
        'url': url,
        'rest_api': 'unknown',
        'graphql': 'unknown',
        'version': 'unknown',
        'notes': ''
    }
    
    headers = {'User-Agent': 'Mozilla/5.0'}
    base = url.rstrip('/')
    
    try:
        # 1. REST API (публичный, без авторизации)
        rest_url = f"{base}/rest/V1/products?searchCriteria[pageSize]=1"
        resp = requests.get(rest_url, headers=headers, timeout=10)
        
        if resp.status_code == 200:
            result['rest_api'] = 'available'
            try:
                data = resp.json()
                if 'items' in data:
                    result['notes'] = f"{len(data['items'])} items in response"
            except:
                pass
        elif resp.status_code == 401:
            result['rest_api'] = 'requires_auth'
            result['notes'] = 'REST API needs authentication'
        else:
            result['rest_api'] = f'error_{resp.status_code}'
        
        # 2. GraphQL
        graphql_url = f"{base}/graphql"
        graphql_query = {
            "query": "{ products(search: \"test\", pageSize: 1) { items { sku name } } }"
        }
        resp = requests.post(graphql_url, json=graphql_query, headers=headers, timeout=10)
        
        if resp.status_code == 200:
            try:
                data = resp.json()
                if 'data' in data:
                    result['graphql'] = 'available'
                    result['notes'] += ' | GraphQL works'
                elif 'errors' in data:
                    result['graphql'] = 'available_with_errors'
                    result['notes'] += f" | GraphQL errors: {str(data['errors'])[:50]}"
            except:
                result['graphql'] = 'response_not_json'
        elif resp.status_code == 404:
            result['graphql'] = 'not_found'
        else:
            result['graphql'] = f'error_{resp.status_code}'
        
        # 3. Определяем версию Magento
        try:
            version_url = f"{base}/rest/V1/store/storeConfigs"
            resp = requests.get(version_url, headers=headers, timeout=5)
            if resp.status_code == 200:
                result['version'] = 'configurable'
        except:
            pass
        
    except Exception as e:
        result['rest_api'] = 'error'
        result['graphql'] = 'error'
        result['notes'] = str(e)[:100]
    
    return result

def scan_magento_sites():
    """Сканирует все Magento сайты."""
    with open('platforms_scan.json', 'r') as f:
        all_sites = json.load(f)
    
    magento_sites = [s for s in all_sites if s['platform'] == 'magento']
    print(f"🔍 Checking {len(magento_sites)} Magento sites...")
    
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(check_magento_api, s['url']): s for s in magento_sites}
        
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            
            if i % 10 == 0:
                print(f"  Progress: {i}/{len(magento_sites)}")
    
    # Статистика
    print("\n📊 API Availability:")
    rest_available = sum(1 for r in results if r['rest_api'] == 'available')
    rest_auth = sum(1 for r in results if r['rest_api'] == 'requires_auth')
    graphql_available = sum(1 for r in results if 'available' in r['graphql'])
    
    print(f"  REST API available: {rest_available}")
    print(f"  REST API requires auth: {rest_auth}")
    print(f"  GraphQL available: {graphql_available}")
    
    # Сохраняем результаты
    with open('magento_api_check.json', 'w') as f:
        json.dump(results, f, indent=2)
    
    print("\n✅ Results saved to magento_api_check.json")
    
    # Показываем сайты с доступным API
    working = [r for r in results if r['rest_api'] == 'available' or 'available' in r['graphql']]
    if working:
        print(f"\n🎯 Sites with working API ({len(working)}):")
        for r in working[:10]:
            print(f"  {r['url']}")
            print(f"    REST: {r['rest_api']} | GraphQL: {r['graphql']}")

if __name__ == "__main__":
    scan_magento_sites()
