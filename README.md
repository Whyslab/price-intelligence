# Price Intelligence 🛍️💰

Система мониторинга цен streetwear/sneakers магазинов с автоматическим нахождением лучших сделок.

## Возможности

- 📊 **133 Shopify магазина** с автоматическим импортом
- 🔄 **SKU-матчинг** между магазинами для сравнения цен
- 📈 **Deal Score** с учётом истории цен и fake discount detection
- 💱 **Нормализация валют** (KRW/EUR/GBP → USD)
- 📱 **Telegram-уведомления** о лучших сделках
- 🌐 **Web Dashboard** с графиками истории цен
- ⏰ **Автоматизация** через cron (каждые 6 часов)

## Архитектура

- **Backend:** Python 3.14, FastAPI, SQLAlchemy
- **Database:** PostgreSQL 18
- **Scraping:** Shopify products.json API
- **Notifications:** Telegram Bot API
- **Scheduling:** cron

## Установка

### Требования
- Python 3.11+
- PostgreSQL 14+
- Arch Linux (или любой другой)

### Настройка БД

```bash
sudo -u postgres initdb -D /var/lib/postgres/data
sudo systemctl start postgresql
sudo -u postgres createuser -s $USER
sudo -u postgres createdb -O $USER price_intelligence
Установка зависимостей
python -m venv webenv
source webenv/bin/activate
pip install fastapi "uvicorn[standard]" jinja2 sqlalchemy "psycopg[binary]" python-dotenv requests
Конфигурация
cp .env.example .env
# Отредактируй .env с твоими credentials
Инициализация схемы
python -m src.init_db
Использование
Импорт товаров
# Быстрый импорт всех Shopify магазинов
python -m src.batch_import_fast

# Полный pipeline (импорт + матчинг + анализ + Telegram)
python -m src.master_pipeline
Web Dashboard
python -m src.web_app
# Открой http://localhost:8000
Матчинг товаров
python -m src.match_products
Анализ сделок
python -m src.deal_engine
Структура проекта
price-intelligence/
├── src/
│   ├── adapters/          # Парсеры магазинов (Shopify, Magento)
│   ├── web_app.py         # FastAPI dashboard
│   ├── master_pipeline.py # Автоматический pipeline
│   ├── match_products.py  # SKU-матчинг
│   ├── deal_engine.py     # Расчёт Deal Score
│   ├── pricing.py         # Sanity layer + исторические метрики
│   └── currency_normalizer.py # Конвертация валют
├── templates/             # HTML шаблоны dashboard
├── .env.example           # Шаблон конфигурации
└── README.md

Автоматизация (cron)
# Каждые 6 часов
0 */6 * * * cd /path/to/price-intelligence && python -m src.master_pipeline >> /tmp/price_bot.log 2>&1

# Автозапуск dashboard при reboot
@reboot cd /path/to/price-intelligence && python -m src.web_app >> /tmp/web_app.log 2>&1 &
Лицензия
MIT
# price-intelligence
