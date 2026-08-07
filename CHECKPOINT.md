# CHECKPOINT — Price Intelligence

## Текущее состояние (05.08.2026)
- 133 Shopify магазина подключены
- 79,181 товаров в БД
- 12,221 SKU-матчей
- 1,498,709 записей истории цен
- Cron работает каждые 6 часов
- Telegram бот + Dashboard активны

## Реализовано
- Shopify adapter с rate limiting
- Currency normalizer (KRW/EUR/GBP → USD)
- Price Sanity Layer (защита от мусора)
- Deal Score (50% cross-market + 50% historical)
- Fake discount detection
- FastAPI dashboard + Telegram
- Variant deduplication (757 слито)
- 6 индексов для производительности

## НЕ реализовано (и почему)
- Magento — API не работают у реальных сайтов
- Fuzzy matching — требует NLP, отложено
- Enterprise sites (Foot Locker) — Kasada anti-bot

## Ключевые файлы
- src/master_pipeline.py — cron pipeline
- src/shopify_adapter.py — парсер
- src/pricing.py — sanity layer + история
- src/currency_normalizer.py — валюты
- src/web_app.py — FastAPI dashboard

## Следующие шаги (приоритеты)
1. Пауза 2-4 недели для накопления истории
2. SKU normalization (+20-30% матчингов)
3. Fuzzy matching через OpenAI API
4. Экспорт в Google Sheets

## Ссылки
- GitHub: github.com/Whyslab/price-intelligence
- БД: postgresql://bogdan@localhost/price_intelligence
- Dashboard: http://localhost:8000
