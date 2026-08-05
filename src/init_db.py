from sqlalchemy import create_engine
from src.config import DATABASE_URL
from src.models import Base

def init_db():
    print(f"Connecting to {DATABASE_URL}...")
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)
    print("✅ Tables created successfully.")

if __name__ == "__main__":
    init_db()