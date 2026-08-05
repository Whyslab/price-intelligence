# recon_mini.py
import requests, re, time

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
SITES = [
    "https://www.a-ma-maniere.com", "https://www.socialstatuspgh.com",
    "https://www.apbstore.com", "https://cncpts.com",
    "https://feature.com", "https://www.likelihood.us",
]

for base in SITES:
    out = {"site": base}
    try:
        r = requests.get(base, headers=UA, timeout=10)
        html = r.text
        out["status"] = r.status_code
        out["shopify"] = "cdn.shopify.com" in html          # сигнатура Shopify
        out["woocommerce"] = "wp-content/plugins/woocommerce" in html
        out["sitemap"] = requests.get(base + "/sitemap.xml", headers=UA, timeout=10).status_code
        pj = requests.get(base + "/products.json?limit=1", headers=UA, timeout=10)
        out["products_json"] = pj.status_code               # 200 = открытый JSON-эндпоинт
        out["product_links_on_home"] = len(re.findall(r'href="/products/[^"]+"', html))
    except Exception as e:
        out["error"] = str(e)[:80]
    print(out)
    time.sleep(1)  # вежливая пауза