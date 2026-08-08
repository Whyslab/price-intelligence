import pytest
import os
from decimal import Decimal

os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

# Импортируем Base и только нужные модели
from src.models import (
    Base, Brand, Store, Product, ProductVariant,
    Offer, ProductMatch, PriceHistory, PriceChange
)

# Только таблицы, нужные для тестов (без RawSnapshot с JSONB)
TEST_TABLES = [
    Brand.__table__,
    Store.__table__,
    Product.__table__,
    ProductVariant.__table__,
    Offer.__table__,
    ProductMatch.__table__,
    PriceHistory.__table__,
    PriceChange.__table__,
]

@pytest.fixture
def db_session():
    engine = create_engine('sqlite:///:memory:')
    # Создаём только нужные таблицы
    Base.metadata.create_all(engine, tables=TEST_TABLES)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()

def _mk(db_session):
    """Хелпер: создаёт бренд, 2 магазина и продукт"""
    b = Brand(name="TestBrand", normalized_name="testbrand",
              domain="testbrand.com", currency="USD")
    sa = Store(name="StoreA", domain="storea.com", currency="USD")
    sb = Store(name="StoreB", domain="storeb.com", currency="USD")
    db_session.add_all([b, sa, sb])
    db_session.commit()
    p = Product(brand_id=b.id, canonical_name="Test Product")
    db_session.add(p)
    db_session.commit()
    return b, sa, sb, p


def test_store_isolation_external_variant(db_session):
    """P0-2: Один external_variant_id в разных магазинах = разные записи"""
    _, sa, sb, p = _mk(db_session)
    v1 = ProductVariant(product_id=p.id, store_id=sa.id, external_variant_id="X123")
    v2 = ProductVariant(product_id=p.id, store_id=sb.id, external_variant_id="X123")
    db_session.add_all([v1, v2])
    db_session.commit()
    assert v1.id != v2.id


def test_store_isolation_external_product(db_session):
    """P0-3: Один external_product_id в разных магазинах = разные записи"""
    _, sa, sb, p = _mk(db_session)
    v1 = ProductVariant(product_id=p.id, store_id=sa.id, external_product_id="P123")
    v2 = ProductVariant(product_id=p.id, store_id=sb.id, external_product_id="P123")
    db_session.add_all([v1, v2])
    db_session.commit()
    assert v1.id != v2.id


def test_no_self_match(db_session):
    """P0-10: ProductMatch не должен ссылаться сам на себя"""
    _, sa, _, p = _mk(db_session)
    v = ProductVariant(product_id=p.id, store_id=sa.id, external_variant_id="1")
    db_session.add(v)
    db_session.commit()
    m = ProductMatch(
        canonical_variant_id=v.id, matched_variant_id=v.id,
        match_method="test", confidence_score=Decimal("0.9")
    )
    db_session.add(m)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_offer_positive_price(db_session):
    """P0-10: Offer с current_price <= 0 отклоняется"""
    _, sa, _, p = _mk(db_session)
    v = ProductVariant(product_id=p.id, store_id=sa.id, external_variant_id="1")
    db_session.add(v)
    db_session.commit()
    o = Offer(store_id=sa.id, variant_id=v.id, url="http://x", current_price=Decimal("-10"))
    db_session.add(o)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_no_duplicate_mapping(db_session):
    """P0-10: Matched variant может иметь только один canonical"""
    _, sa, _, p = _mk(db_session)
    v_canonical = ProductVariant(product_id=p.id, store_id=sa.id, external_variant_id="C")
    v_matched = ProductVariant(product_id=p.id, store_id=sa.id, external_variant_id="M")
    db_session.add_all([v_canonical, v_matched])
    db_session.commit()
    m1 = ProductMatch(
        canonical_variant_id=v_canonical.id, matched_variant_id=v_matched.id,
        match_method="a", confidence_score=Decimal("0.9")
    )
    db_session.add(m1)
    db_session.commit()
    # Пытаемся создать второй матч с тем же matched_variant_id
    m2 = ProductMatch(
        canonical_variant_id=v_canonical.id, matched_variant_id=v_matched.id,
        match_method="b", confidence_score=Decimal("0.8")
    )
    db_session.add(m2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
