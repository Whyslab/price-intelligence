# ПОЛНЫЙ ОТЧЁТ ПО ПРОЕКТУ PRICE INTELLIGENCE

Дата: 05 августа 2026
Автор: Богдан
Репозиторий: github.com/Whyslab/price-intelligence
Статус: Production-ready


## 1. ОПИСАНИЕ ПРОЕКТА

Автоматизированная система мониторинга цен streetwear и sneaker магазинов 
с интеллектуальным анализом сделок, SKU-матчингом между магазинами 
и уведомлениями о лучших предложениях в Telegram.

Ключевая ценность: пользователь получает только РЕАЛЬНЫЕ скидки 
(с учётом истории цен и детекции маркетинговых манипуляций), 
а не фейковые "sale" от магазинов.


## 2. ТЕХНИЧЕСКИЙ СТЕК

- Backend: Python 3.14
- Database: PostgreSQL 18
- Web framework: FastAPI 0.141.1
- ORM: SQLAlchemy 2.0.51
- DB Driver: psycopg 3.3.4
- Templates: Jinja2 3.1.6
- Frontend: Tailwind CSS + Chart.js + HTMX (CDN)
- HTTP Client: requests 2.34.2
- Scheduling: cron (системный)
- OS: Arch Linux


## 3. РЕАЛИЗОВАННЫЕ КОМПОНЕНТЫ

### 3.1 Сбор данных
- Shopify Adapter — парсинг products.json API
- Batch Importer — 133 магазина с rate limiting
- Platform Detector — авто-определение Shopify/Magento/Custom
- Magento Adapter — отброшен (у реальных сайтов нет открытого API)

Покрытие: 327 сайтов просканировано → 140 Shopify найдено → 133 импортируется

### 3.2 Обработка данных
- SKU Matching — точный матч по артикулу (12,221 связей)
- Brand Normalization — Unicode-нормализация (STÜSSY → STUSSY)
- Currency Normalizer — KRW/EUR/GBP → USD (35K+ товаров)
- Price Sanity Layer — защита от аномальных цен
- Variant Deduplication — 757 дублей слито

### 3.3 Аналитика
- Deal Score — 50% cross-market + 50% historical
- Price History — 1.5M+ записей
- Fake Discount Detection — детекция маркетинговых манипуляций
- Market Metrics — median, percentile, trend analysis

### 3.4 User Interface
- Web Dashboard — FastAPI + Tailwind, http://localhost:8000
- Telegram Bot — топ-5 сделок каждые 6 часов
- Cron Automation — 2 задачи: pipeline + dashboard autostart


## 4. СТАТИСТИКА БД (ФИНАЛЬНАЯ)

- Магазинов (stores): 122
- Товаров (products): 79,181
- Вариантов (product_variants): 537,917
- SKU-матчей между магазинами: 12,221
- Актуальных офферов (offers): 539,440
- Исторических записей (price_history): 1,498,709
- Брендов (brands): ~3,000


## 5. КЛЮЧЕВЫЕ ТЕХНИЧЕСКИЕ РЕШЕНИЯ

### 5.1 Архитектура данных

Stores → Offers ← Variants → Products → Brands
                     ↓
              Price History
                     ↓
             Product Matches

### 5.2 Price Sanity Layer (3 уровня защиты)

1. Абсолютные границы: цена в диапазоне (0, $20,000]
2. Outlier ratio: отклонение от медианы не более 5x
3. Plausible change: новая цена не может отличаться от старой >10x

Пример: магазин Dope-Factory отдал 590,000 KRW
- Без нормализации: $590,000 мусор в БД
- С Currency Normalizer: корректные $447 USD

### 5.3 Deal Score Algorithm

Deal Score = 0.5 × Cross-Market Score + 0.5 × Historical Score
Cross-Market Score = min(100, discount_vs_median × 2)
Historical Score = min(100, discount_vs_history × 2)
Confidence = min(100, store_count × 25)

Fake Discount Flag:
  old_price > historical_median × 1.15 
  AND current_price >= historical_median × 0.9

### 5.4 Индексы БД

- idx_price_history_variant_time — история по variant_id + timestamp
- idx_products_brand — JOIN products → brands
- idx_variants_product_sku — поиск вариантов по SKU
- idx_offers_variant — JOIN offers → variants
- idx_matches_canon — product_matches.canonical_variant_id
- idx_matches_matched — product_matches.matched_variant_id


## 6. АВТОМАТИЗАЦИЯ (CRON)

Каждые 6 часов (00:00, 06:00, 12:00, 18:00):
  0 */6 * * * cd /home/bogdan/price-intelligence && \
      /usr/bin/python -m src.master_pipeline >> /tmp/price_bot.log 2>&1

Автозапуск dashboard при reboot:
  @reboot cd /home/bogdan/price-intelligence && \
      /usr/bin/python -m src.web_app >> /tmp/web_app.log 2>&1 &

Pipeline выполняется в 4 этапа:
  1. Импорт товаров со 133 магазинов
  2. SKU-матчинг между магазинами
  3. Расчёт Deal Score для всех матчей
  4. Отправка топ-5 сделок в Telegram


## 7. КРИТИЧЕСКИЕ ПРОБЛЕМЫ И РЕШЕНИЯ

### 7.1 Проблема: KRW валюта (Dope-Factory)
- Симптом: Цена товара $590,000 вместо $447
- Причина: Магазин отдаёт цену в корейских вонах (590,000 KRW)
- Решение: Currency Normalizer с whitelist + fallback по домену
- Результат: Корректная конвертация в USD ✅

### 7.2 Проблема: Дубли вариантов
- Симптом: 6 одинаковых офферов oneblockdown.it
- Причина: Парсер создавал новый вариант вместо upsert
- Решение: Скрипт дедупликации + UNIQUE constraint
- Результат: 757 дублей слито ✅

### 7.3 Проблема: Our Legacy THIRD CUT с ценой $295,215
- Симптом: Медiana $295,215 в dashboard
- Причина: Один магазин отдал мусорную цену
- Решение: Price Sanity Layer фильтрует outliers
- Результат: Мусор не попадает в аналитику ✅

### 7.4 Проблема: Magento сайты не работают
- Симптом: 0 из 81 Magento-сайта отдают рабочий API
- Причина: Foot Locker/Champs — не Magento (Express.js + Kasada anti-bot), 
  мелкие бутики — API отключен
- Решение: Отказ от Magento-адаптера
- Результат: Честная оценка — enterprise сайты парсить без прокси-инфраструктуры 
  невозможно ❌

### 7.5 Проблема: FastAPI TemplateResponse API change
- Симптом: TypeError: cannot use 'tuple' as a dict key
- Причина: В новой версии Starlette изменилась сигнатура
- Решение: Передача request как первого аргумента
- Результат: Dashboard работает ✅


## 8. ЧТО НЕ РЕАЛИЗОВАНО

- Magento Adapter ❌ — у реальных сайтов нет открытого API
- Fuzzy Matching ⏸ — требует NLP/embeddings, отложено
- Учёт доставки ⏸ — требует checkout scraping, юридические риски
- Enterprise sites (Foot Locker, Champs) ❌ — enterprise anti-bot Kasada + $500/мес прокси
- Мобильное приложение ⏸ — Telegram достаточно для MVP
- Монетизация ⏸ — нужна юридическая подготовка (GDPR, ToS)


## 9. ФАЙЛОВАЯ СТРУКТУРА ПРОЕКТА

price-intelligence/
├── src/
│   ├── adapters/
│   │   ├── shopify_adapter.py       # Парсер Shopify
│   │   └── magento_adapter.py       # Парсер Magento (не используется)
│   ├── batch_import_fast.py         # Batch importer с лимитами
│   ├── currency_normalizer.py       # Конвертация валют
│   ├── deal_engine.py               # Расчёт Deal Score
│   ├── dedup_variants.py            # Дедупликация вариантов
│   ├── detect_platforms.py          # Детектор платформ
│   ├── filter_shopify.py            # Фильтр Shopify сайтов
│   ├── find_new_stores.py           # Поиск новых магазинов
│   ├── import_new_stores.py         # Импорт только новых магазинов
│   ├── init_db.py                   # Инициализация схемы БД
│   ├── master_pipeline.py           # Главный pipeline (cron)
│   ├── match_products.py            # SKU-матчинг
│   ├── models.py                    # SQLAlchemy модели
│   ├── pricing.py                   # Price Sanity Layer + история
│   ├── reimport_problematic.py      # Переимпорт проблемных магазинов
│   ├── telegram_notifier.py         # Telegram уведомления
│   └── web_app.py                   # FastAPI dashboard
├── templates/
│   ├── dashboard.html               # Главная страница
│   └── product.html                 # Детальная страница товара
├── .env                             # Секреты (в .gitignore)
├── .env.example                     # Шаблон конфигурации
├── .gitignore                       # Защита от попадания секретов
└── README.md                        # Документация


## 10. РЕЗУЛЬТАТЫ ДЛЯ БИЗНЕСА

Что система делает автоматически:
- Мониторит 133 магазина каждые 6 часов
- Находит реальные скидки (с учётом истории)
- Фильтрует маркетинговые манипуляции (fake discounts)
- Присылает топ-5 сделок в Telegram
- Конвертирует все валюты в USD
- Защищает от аномальных цен

Что получает пользователь:
- Только РЕАЛЬНЫЕ скидки (не маркетинг)
- Прозрачная аналитика: медиана, история, percentile
- Прямые ссылки на магазины
- Экономия времени: не нужно мониторить 133 магазина вручную


## 11. РЕКОМЕНДАЦИИ ПО РАЗВИТИЮ

### Приоритет 1: Накопление истории (2-4 недели)
Система должна поработать без изменений, чтобы накопить:
- 5-10M записей в price_history
- Полноценные графики трендов
- Точный Historical Deal Score

### Приоритет 2: Расширение функционала (через месяц)
1. SKU Normalization — матчинг по артикулу без размера (+20-30% матчей)
2. Fuzzy Matching через OpenAI API (~$50/мес) — для товаров без SKU
3. Экспорт в Google Sheets — ежедневная выгрузка топ-100

### Приоритет 3: Масштабирование (через 3 месяца)
1. Прокси-инфраструктура для enterprise сайтов
2. Мобильное приложение (React Native + push)
3. Affiliate интеграции (Awin, CJ, Impact) — 3-8% комиссия
4. SaaS модель — подписка для реселлеров


## 12. ФИНАЛЬНЫЕ МЕТРИКИ ПРОЕКТА

- Длительность разработки: ~1 сессия
- Строк кода: ~3,400
- Магазинов подключено: 133
- Товаров в БД: 79,181
- SKU-матчей: 12,221
- Записей истории цен: 1,498,709
- Telegram уведомлений: 5 каждые 6 часов
- Время полного pipeline: ~40 минут
- Production-ready: Да


## 13. ИТОГ

Проект Price Intelligence — полностью рабочая production-система, которая:

- Собирает данные с 133 магазинов
- Нормализует валюты и защищает от мусора
- Находит реальные скидки с учётом истории
- Автоматически уведомляет о лучших сделках
- Имеет веб-интерфейс с графиками
- Работает без вмешательства пользователя

Код доступен на GitHub: github.com/Whyslab/price-intelligence

Следующий шаг: дать системе поработать 2-4 недели для накопления истории цен, 
затем добавить fuzzy matching через OpenAI API.
