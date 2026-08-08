"""Находит новые Shopify магазины, которых ещё нет в БД"""
import json
from urllib.parse import urlparse

from sqlalchemy import create_engine, text

from src.config import DATABASE_URL

# Загружаем список Shopify сайтов
with open('shopify_sites.json', 'r') as f:
    shopify_sites = json.load(f)

# Получаем список существующих магазинов из БД
engine = create_engine(DATABASE_URL)
with engine.connect() as conn:
    existing = set(row[0] for row in conn.execute(text(
        "SELECT domain FROM stores"
    )).fetchall())

# Находим новые
new_stores = []
for site in shopify_sites:
    url = site['url']
    domain = urlparse(url).netloc
    
    # Убираем www. для нормализации
    clean_domain = domain.replace('www.', '')
    
    # Проверяем, есть ли такой домен в БД
    if clean_domain not in existing:
        new_stores.append(site)

print(f"📊 Всего Shopify сайтов: {len(shopify_sites)}")
print(f"📊 Уже в БД: {len(existing)}")
print(f"📊 Новых для импорта: {len(new_stores)}")

if new_stores:
    print("\n🆕 Новые магазины:")
    for site in new_stores[:10]:
        print(f"   {site['url']}")
    
    if len(new_stores) > 10:
        print(f"   ... и ещё {len(new_stores) - 10}")
    
    # Сохраняем список новых
    with open('new_shopify_stores.json', 'w') as f:
        json.dump(new_stores, f, indent=2)
    
    print("\n✅ Список сохранён в new_shopify_stores.json")
else:
    print("\n✅ Новых магазинов нет")
