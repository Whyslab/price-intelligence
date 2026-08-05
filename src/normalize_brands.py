"""
Нормализует существующие бренды с учётом Unicode (умлауты, акценты).
"""
import unicodedata
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.config import DATABASE_URL
from src.models import Brand

def strip_accents(text: str) -> str:
    """Удаляет диакритические знаки: Ü→U, É→E, Ñ→N"""
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))

def normalize_brands():
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    db = Session()
    
    brands = db.query(Brand).all()
    print(f"🔧 Normalizing {len(brands)} brands...")
    
    changes = 0
    for brand in brands:
        old_norm = brand.normalized_name
        new_norm = strip_accents(brand.name.lower().strip())
        
        if old_norm != new_norm:
            # Проверяем, нет ли уже бренда с таким normalized_name
            existing = db.query(Brand).filter(
                Brand.normalized_name == new_norm,
                Brand.id != brand.id
            ).first()
            
            if existing:
                # Сливаем бренды: переносим товары к existing
                from src.models import Product
                products = db.query(Product).filter(Product.brand_id == brand.id).all()
                for p in products:
                    p.brand_id = existing.id
                db.delete(brand)
                changes += 1
                print(f"  Merged '{brand.name}' → '{existing.name}'")
            else:
                brand.normalized_name = new_norm
                changes += 1
                print(f"  '{brand.name}': {old_norm} → {new_norm}")
    
    db.commit()
    db.close()
    print(f"\n✅ {changes} brands normalized/merged")

if __name__ == "__main__":
    normalize_brands()
