"""
Detects platform type for each site (Shopify, WooCommerce, Magento, Custom).
"""
import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

def detect_platform(url: str) -> dict:
    """Определяет платформу сайта."""
    result = {
        'url': url,
        'platform': 'unknown',
        'status': 'unknown',
        'notes': ''
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        result['status'] = response.status_code
        
        if response.status_code != 200:
            result['notes'] = f"HTTP {response.status_code}"
            return result
        
        html = response.text.lower()
        
        # Shopify detection
        if 'cdn.shopify.com' in html or 'shopify.com' in html:
            result['platform'] = 'shopify'
            
            # Проверяем доступность products.json
            try:
                products_url = f"{url.rstrip('/')}/products.json?limit=1"
                prod_resp = requests.get(products_url, headers=headers, timeout=5)
                if prod_resp.status_code == 200:
                    result['notes'] = 'products.json available'
                else:
                    result['notes'] = 'products.json blocked'
            except:
                result['notes'] = 'products.json timeout'
            
            return result
        
        # WooCommerce detection
        if 'wp-content/plugins/woocommerce' in html:
            result['platform'] = 'woocommerce'
            
            # Проверяем REST API
            try:
                api_url = f"{url.rstrip('/')}/wp-json/wc/v3/products?per_page=1"
                api_resp = requests.get(api_url, headers=headers, timeout=5)
                if api_resp.status_code == 200:
                    result['notes'] = 'REST API available'
                elif api_resp.status_code == 401:
                    result['notes'] = 'REST API requires auth'
                else:
                    result['notes'] = f'REST API status: {api_resp.status_code}'
            except:
                result['notes'] = 'REST API timeout'
            
            return result
        
        # Magento detection
        if 'mage/' in html or 'magento' in html or '/static/version' in html:
            result['platform'] = 'magento'
            result['notes'] = 'Magento detected'
            return result
        
        # Custom/other
        result['platform'] = 'custom'
        result['notes'] = 'No known platform detected'
        
    except requests.exceptions.Timeout:
        result['status'] = 'timeout'
        result['notes'] = 'Connection timeout'
    except requests.exceptions.ConnectionError:
        result['status'] = 'connection_error'
        result['notes'] = 'Connection failed'
    except Exception as e:
        result['status'] = 'error'
        result['notes'] = str(e)[:50]
    
    return result

def scan_all_sites(sites_file: str, output_file: str):
    """Сканирует все сайты параллельно."""
    with open(sites_file, 'r') as f:
        sites = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    print(f"🔍 Scanning {len(sites)} sites...")
    
    results = []
    
    # Параллельное сканирование (10 потоков)
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detect_platform, site): site for site in sites}
        
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            
            if i % 10 == 0:
                print(f"  Progress: {i}/{len(sites)}")
    
    # Сохраняем результаты
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    # Статистика
    platforms = {}
    for r in results:
        p = r['platform']
        platforms[p] = platforms.get(p, 0) + 1
    
    print("\n📊 Platform Distribution:")
    for platform, count in sorted(platforms.items(), key=lambda x: x[1], reverse=True):
        print(f"  {platform}: {count}")
    
    print(f"\n✅ Results saved to {output_file}")

if __name__ == "__main__":
    import sys
    sites_file = sys.argv[1] if len(sys.argv) > 1 else "all_sites_combined.txt"
    scan_all_sites(
        sites_file=sites_file,
        output_file="platforms_scan.json"
    )
