import os
from dotenv import load_dotenv

# Загружаем .env из корня проекта
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан в .env файле!")

print(f"✅ Config loaded. DB: {DATABASE_URL[:40]}...")
