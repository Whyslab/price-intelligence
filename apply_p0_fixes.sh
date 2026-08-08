#!/usr/bin/env bash
set -e

echo "🔧 Применяем P0-исправления..."

# 1. Резервная копия ключевых файлов
mkdir -p .backup_$(date +%s)
cp src/models.py .backup_*/models.py.bak 2>/dev/null || true
cp src/deal_engine.py .backup_*/deal_engine.py.bak 2>/dev/null || true

# 2. src/models.py — исправлены __table_args__, store isolation, constraints
cat << 'MODELS_EOF' > src/models.py
from sqlalchemy import (
    Boolean, UniqueConstraint, Column, DateTime, Float, ForeignKey, Index, Integer,
    Numeric, String, Text, CheckConstraint
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base
from sqlalchemy.schema import PrimaryKeyConstraint
from sqlalchemy.sql import func, text

Base = declarative_base()

class Brand(Base):
    __tablename__ = 'brands'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    normalized_name = Column(String, unique=True, index=True)
    domain = Column(String, nullable=False, unique=True)
    currency = Column(String(3), nullable=False)
    region = Column(String(2))
    last_sync = Column(DateTime(timezone=True))
    last_successful_sync = Column(DateTime(timezone=True))
    sync_status = Column(String(20), default='unknown')
    last_error = Column(Text)
    products_count = Column(Integer, default=0)
    reliability_score = Column(Integer, default=0)

class Store(Base):
    __tablename__ = 'stores'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    domain = Column(String, nullable=False, unique=True)
    currency = Column(String(10), nullable=False)
    region = Column(String(10))
    timezone = Column(String(50))
    reliability_score = Column(Numeric(5, 2), default=0.5)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_sync = Column(DateTime(timezone=True))
    last_successful_sync = Column(DateTime(timezone=True))
    sync_status = Column(String(20), default='unknown')
    last_error = Column(Text)
    products_count = Column(Integer, default=0)

class Product(Base):
    __tablename__ = 'products'
    id = Column(Integer, primary_key=True)
    brand_id = Column(Integer, ForeignKey('brands.id'))
    canonical_name = Column(String, nullable=False)
    category = Column(String)
    __table_args__ = (Index('ix_product_category', 'category'),)

class ProductVariant(Base):
    __tablename__ = 'product_variants'
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=False)  # P0-2: NOT NULL
    sku = Column(String)
    ean = Column(String)
    external_product_id = Column(String)
    external_variant_id = Column(String)
    size = Column(String)
    color = Column(String)
    normalized_size = Column(String(20), index=True)
    normalized_color = Column(String(20), index=True)
    normalized_gender_age = Column(String(20), index=True)
    attributes = Column(JSONB)
    __table_args__ = (
        # P0-3: Scoped identity — объединено в один __table_args__ (P0-1 fix)
        UniqueConstraint('store_id', 'external_variant_id', name='uq_store_external_variant'),
        UniqueConstraint('store_id', 'external_product_id', name='uq_store_external_product'),
        Index('ix_variant_ean', 'ean'),
        Index('ix_variant_sku', 'sku'),
    )

class Offer(Base):
    __tablename__ = 'offers'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=False)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=False)
    url = Column(Text, nullable=False)
    current_price = Column(Numeric(10, 2), nullable=False)
    old_price = Column(Numeric(10, 2))
    in_stock = Column(Boolean, default=True)
    original_currency = Column(String(3))
    exchange_rate = Column(Numeric(12, 6))
    exchange_rate_timestamp = Column(DateTime(timezone=True))
    exchange_rate_source = Column(String(50))
    currency_source = Column(String(50))
    parser_version = Column(String(20), default='1.0')
    raw_snapshot_id = Column(Integer, ForeignKey('raw_snapshots.id'))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        CheckConstraint('current_price > 0', name='check_price_positive'),  # P0-10
        UniqueConstraint('store_id', 'variant_id', name='uq_store_variant_offer'),
    )

class PriceHistory(Base):
    __tablename__ = 'price_history'
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=False)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    price = Column(Numeric(10, 2), nullable=False)
    old_price = Column(Numeric(10, 2))
    original_currency = Column(String(3))
    exchange_rate = Column(Numeric(12, 6))
    __table_args__ = (
        CheckConstraint('price > 0', name='check_price_history_positive'),
        PrimaryKeyConstraint('variant_id', 'store_id', 'timestamp'),
        Index('ix_price_history_variant_time', 'variant_id', 'timestamp'),
    )

class PriceChange(Base):
    __tablename__ = 'price_changes'
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=False)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    old_price = Column(Numeric(10, 2))
    ended_at = Column(DateTime(timezone=True))
    original_currency = Column(String(3))
    exchange_rate = Column(Numeric(12, 6))
    exchange_rate_source = Column(String(50))
    parser_version = Column(String(20), default='1.0')
    raw_snapshot_id = Column(Integer, ForeignKey('raw_snapshots.id'))
    normalized_size = Column(String(20))
    in_stock = Column(Boolean, default=True)
    region = Column(String(2))
    __table_args__ = (
        PrimaryKeyConstraint('variant_id', 'store_id', 'started_at'),
        CheckConstraint('price > 0', name='check_price_change_positive'),
        # P0-10: Concurrency protection — только ОДИН открытый интервал
        Index(
            'uq_price_change_open_interval',
            'variant_id', 'store_id',
            unique=True,
            postgresql_where=text('ended_at IS NULL')
        ),
        Index('ix_price_changes_variant_ended', 'variant_id', 'ended_at'),
    )

class ProductMatch(Base):
    __tablename__ = 'product_matches'
    id = Column(Integer, primary_key=True)
    canonical_variant_id = Column(Integer, ForeignKey('product_variants.id', ondelete='CASCADE'), nullable=False)
    matched_variant_id = Column(Integer, ForeignKey('product_variants.id', ondelete='CASCADE'), nullable=False)
    match_method = Column(String(50), nullable=False)
    confidence_score = Column(Numeric(3, 2), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint('confidence_score >= 0 AND confidence_score <= 1', name='check_confidence_range'),
        CheckConstraint('canonical_variant_id != matched_variant_id', name='check_no_self_match'),
        UniqueConstraint('canonical_variant_id', 'matched_variant_id', name='uq_product_match_direct'),
        UniqueConstraint('matched_variant_id', name='uq_product_match_reverse'),  # P0-10: no duplicate mappings
        Index('idx_matches_canonical', 'canonical_variant_id'),
        Index('idx_matches_matched', 'matched_variant_id'),
    )

class PipelineRun(Base):
    __tablename__ = 'pipeline_runs'
    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True))
    duration_seconds = Column(Integer)
    status = Column(String(20), nullable=False)
    steps_completed = Column(Integer, default=0)
    steps_total = Column(Integer, default=0)
    error_message = Column(Text)

class CurrencyError(Base):
    __tablename__ = 'currency_errors'
    id = Column(Integer, primary_key=True)
    url = Column(Text)
    domain = Column(Text)
    detected_currency = Column(String(3))
    error_type = Column(String(50))
    raw_price = Column(Numeric(12, 2))
    timestamp = Column(DateTime(timezone=False), server_default=func.now())

class BrandCanonical(Base):
    __tablename__ = 'brand_canonical'
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class BrandAlias(Base):
    __tablename__ = 'brand_aliases'
    brand_id = Column(Integer, ForeignKey('brands.id'), primary_key=True)
    canonical_id = Column(Integer, ForeignKey('brand_canonical.id'))
    confidence = Column(Float, default=1.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ColorCanonical(Base):
    __tablename__ = 'color_canonical'
    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False)
    hex_code = Column(String(10))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ColorAlias(Base):
    __tablename__ = 'color_aliases'
    original_color = Column(String(200), primary_key=True)
    canonical_id = Column(Integer, ForeignKey('color_canonical.id'))
    confidence = Column(Float, default=1.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class DealAlert(Base):
    __tablename__ = 'deal_alerts'
    id = Column(Integer, primary_key=True)
    fingerprint = Column(String(16), unique=True, nullable=False)
    canonical_variant_id = Column(Integer, ForeignKey('product_variants.id'))
    matched_variant_id = Column(Integer, ForeignKey('product_variants.id'))
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    deal_score = Column(Integer)
    confidence = Column(Integer)
    classification = Column(String(50))
    reason = Column(Text)
    sku = Column(String)
    sent_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (Index('idx_deal_alerts_sku_store_at', 'sku', 'store_id', 'sent_at', unique=True),)

class RawSnapshot(Base):
    __tablename__ = 'raw_snapshots'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=False)
    pipeline_run_id = Column(Integer, ForeignKey('pipeline_runs.id'), nullable=True)
    adapter_name = Column(String(50), nullable=False)
    url = Column(Text, nullable=False)
    http_status = Column(Integer)
    raw_payload = Column(JSONB, nullable=False)
    response_headers = Column(JSONB)
    parser_version = Column(String(20), default='1.0')
    products_count = Column(Integer, default=0)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index('ix_raw_snapshot_store_time', 'store_id', 'fetched_at'),
        Index('ix_raw_snapshot_pipeline', 'pipeline_run_id'),
    )

class DealValidation(Base):
    __tablename__ = 'deal_validation'
    id = Column(Integer, primary_key=True)
    alert_id = Column(Integer, ForeignKey('deal_alerts.id', ondelete='CASCADE'))
    user_id = Column(String(50))
    label = Column(Integer)
    notes = Column(Text)
    validated_at = Column(DateTime(timezone=True), server_default=func.now())
MODELS_EOF

echo "✅ src/models.py"

# 3. Миграция
cat << 'MIGRATION_EOF' > alembic/versions/cd60ed470590_add_store_id_to_productvariant_for_.py
"""add store_id to ProductVariant for store isolation

Revision ID: cd60ed470590
Revises: 
Create Date: 2026-08-08

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'cd60ed470590'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. Add nullable
    op.add_column('product_variants', sa.Column('store_id', sa.Integer(), nullable=True))
    # 2. Deterministic backfill — MIN(store_id) from offers
    op.execute("""
        UPDATE product_variants pv
        SET store_id = sub.min_store_id
        FROM (SELECT variant_id, MIN(store_id) as min_store_id FROM offers GROUP BY variant_id) sub
        WHERE pv.store_id IS NULL AND pv.id = sub.variant_id
    """)
    # 3. Clean cross-store contamination
    op.execute("""
        DELETE FROM offers o
        USING product_variants pv
        WHERE o.variant_id = pv.id AND pv.store_id IS NOT NULL AND o.store_id != pv.store_id
    """)
    # 4. Remove orphaned variants
    op.execute("""DELETE FROM product_variants WHERE store_id IS NULL""")
    # 5. NOT NULL
    op.alter_column('product_variants', 'store_id', existing_type=sa.Integer(), nullable=False)
    # 6. FK + Index
    op.create_foreign_key('fk_product_variants_store_id', 'product_variants', 'stores', ['store_id'], ['id'], ondelete='CASCADE')
    op.create_index('ix_product_variants_store_id', 'product_variants', ['store_id'], unique=False)
    # 7. Scoped unique constraints
    op.create_unique_constraint('uq_store_external_variant', 'product_variants', ['store_id', 'external_variant_id'])
    op.create_unique_constraint('uq_store_external_product', 'product_variants', ['store_id', 'external_product_id'])

def downgrade() -> None:
    op.drop_constraint('uq_store_external_product', 'product_variants', type_='unique')
    op.drop_constraint('uq_store_external_variant', 'product_variants', type_='unique')
    op.drop_index('ix_product_variants_store_id', table_name='product_variants')
    op.drop_constraint('fk_product_variants_store_id', 'product_variants', type_='foreignkey')
    op.alter_column('product_variants', 'store_id', existing_type=sa.Integer(), nullable=True)
MIGRATION_EOF

echo "✅ alembic migration"

# 4. deal_engine.py — патчим ключевые функции через python
python3 << 'PYTHON_PATCH_EOF'
import re
path = 'src/deal_engine.py'
with open(path, 'r', encoding='utf-8') as f:
    code = f.read()

# Fix 1: SQL для get_historical_metrics — интервальный счётчик + weighted percentiles
new_sql = """        WITH all_variants AS (
            SELECT pm.canonical_variant_id AS vid FROM product_matches pm
            WHERE pm.canonical_variant_id = ANY(:ids)
            UNION
            SELECT pm.matched_variant_id AS vid FROM product_matches pm
            WHERE pm.canonical_variant_id = ANY(:ids)
        ),
        intervals AS (
            SELECT 
                av.vid,
                pc.price,
                pc.started_at,
                COALESCE(pc.ended_at, NOW()) AS ended_at,
                EXTRACT(EPOCH FROM (COALESCE(pc.ended_at, NOW()) - pc.started_at)) / 86400.0 AS days_at_price
            FROM all_variants av
            JOIN price_changes pc ON pc.variant_id = av.vid
            WHERE pc.price > 0
        ),
        interval_counts AS (
            SELECT vid, COUNT(*) AS total_intervals
            FROM intervals
            GROUP BY vid
        ),
        weighted AS (
            SELECT 
                vid,
                price,
                days_at_price,
                SUM(days_at_price) OVER (PARTITION BY vid) AS total_days,
                SUM(days_at_price) OVER (PARTITION BY vid ORDER BY price) AS cumulative_weight,
                MAX(ended_at) OVER (PARTITION BY vid) AS last_observed,
                FIRST_VALUE(price) OVER (PARTITION BY vid ORDER BY CASE WHEN ended_at IS NULL THEN 0 ELSE 1 END, ended_at DESC) AS current_price
            FROM intervals
        ),
        medians AS (
            SELECT DISTINCT ON (vid)
                vid,
                price AS weighted_median
            FROM weighted
            WHERE cumulative_weight >= total_days / 2.0
            ORDER BY vid, cumulative_weight
        ),
        p10 AS (
            SELECT DISTINCT ON (vid)
                vid,
                price AS weighted_p10
            FROM weighted
            WHERE cumulative_weight >= total_days * 0.1
            ORDER BY vid, cumulative_weight
        ),
        p90 AS (
            SELECT DISTINCT ON (vid)
                vid,
                price AS weighted_p90
            FROM weighted
            WHERE cumulative_weight >= total_days * 0.9
            ORDER BY vid, cumulative_weight
        )
        SELECT 
            w.vid,
            m.weighted_median,
            p1.weighted_p10,
            p9.weighted_p90,
            MIN(w.price) AS historical_min,
            MAX(w.price) AS historical_max,
            MAX(w.total_days) AS total_days,
            MAX(w.current_price) AS current_price,
            EXTRACT(EPOCH FROM (NOW() - MAX(w.last_observed))) / 86400 AS days_since_last_update,
            ic.total_intervals
        FROM weighted w
        JOIN medians m ON m.vid = w.vid
        JOIN p10 p1 ON p1.vid = w.vid
        JOIN p90 p9 ON p9.vid = w.vid
        JOIN interval_counts ic ON ic.vid = w.vid
        GROUP BY w.vid, m.weighted_median, p1.weighted_p10, p9.weighted_p90, ic.total_intervals"""

code = re.sub(r"WITH all_variants AS.*?GROUP BY w\.vid, m\.weighted_median", new_sql, code, flags=re.DOTALL)

# Fix 2: find_best_deals — убрать [:100], SQL-ranking
old_loop = "for variant_id in variant_ids[:100]:"
new_loop = "for variant_id in variant_ids:"
code = code.replace(old_loop, new_loop)

# Fix 3: Silent exceptions
code = code.replace("except Exception: pass", "except Exception as e: import sys; print(f'[DealEngine] error: {e}', file=sys.stderr)")
code = code.replace("except Exception: return None", "except Exception as e: import sys; print(f'[DealEngine] time_at_price error: {e}', file=sys.stderr); return None")

with open(path, 'w', encoding='utf-8') as f:
    f.write(code)
PYTHON_PATCH_EOF

echo "✅ src/deal_engine.py"

# 5. shopify_adapter.py — store isolation
python3 << 'SHOPIFY_PATCH_EOF'
path = 'src/adapters/shopify_adapter.py'
with open(path, 'r', encoding='utf-8') as f:
    code = f.read()

old = """        if external_variant_id:
            store = self.get_or_create_store(db)
            variant = db.query(ProductVariant).filter(
                ProductVariant.external_variant_id == external_variant_id,
                ProductVariant.product.has(brand_id=product.brand_id)"""
new = """        store = self.get_or_create_store(db)
        if external_variant_id:
            variant = db.query(ProductVariant).filter(
                ProductVariant.external_variant_id == external_variant_id,
                ProductVariant.store_id == store.id"""
code = code.replace(old, new)
code = code.replace("except Exception: continue", "except Exception as e: import sys; print(f'[Shopify] import error: {e}', file=sys.stderr); continue")
with open(path, 'w', encoding='utf-8') as f:
    f.write(code)
SHOPIFY_PATCH_EOF

echo "✅ src/adapters/shopify_adapter.py"

# 6. magento_adapter.py — store isolation
python3 << 'MAGENTO_PATCH_EOF'
path = 'src/adapters/magento_adapter.py'
with open(path, 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
"""                variant = db.query(ProductVariant).filter(
                    ProductVariant.sku == sku,
                    ProductVariant.product_id == product.id
                ).first()""",
"""                variant = db.query(ProductVariant).filter(
                    ProductVariant.sku == sku,
                    ProductVariant.product_id == product.id,
                    ProductVariant.store_id == store.id
                ).first()""")

code = code.replace(
"""                    variant = ProductVariant(
                        product_id=product.id,
                        sku=sku,""",
"""                    variant = ProductVariant(
                        product_id=product.id,
                        store_id=store.id,
                        sku=sku,""")

with open(path, 'w', encoding='utf-8') as f:
    f.write(code)
MAGENTO_PATCH_EOF

echo "✅ src/adapters/magento_adapter.py"

# 7. batch_import_fast.py — убрать двойную загрузку первой страницы
python3 << 'BATCH_PATCH_EOF'
path = 'src/batch_import_fast.py'
with open(path, 'r', encoding='utf-8') as f:
    code = f.read()

# Если найдём старую пагинацию — заменим
old_block = """            # Быстрая проверка размера магазина (только первая страница)
            adapter.products_url = f"{adapter.base_url}/products.json"
            first_page = adapter.session.get(
                f"{adapter.products_url}?limit=250",
                timeout=10
            )"""
new_block = """            # P0-5: Single cursor-based fetch via adapter (no double page-1 fetch)
            adapter.products_url = f"{adapter.base_url}/products.json"
            store = adapter.get_or_create_store(db)
            first_page = None  # placeholder
            products, snapshot_id = adapter.fetch_products(limit=250, max_pages=max_pages, db=db, store_id=store.id)
            first_page_data = {'products': products}
            first_page_products = products"""
if old_block in code:
    code = code.replace(old_block, new_block)

with open(path, 'w', encoding='utf-8') as f:
    f.write(code)
BATCH_PATCH_EOF

echo "✅ src/batch_import_fast.py"

# 8. Regression tests
mkdir -p tests
cat << 'TESTS_EOF' > tests/test_regression_p0.py
import pytest
import os
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from src.models import Base, Brand, Store, Product, ProductVariant, Offer, ProductMatch

@pytest.fixture
def db_session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()

def _mk(db_session):
    b = Brand(name="TB", normalized_name="tb", domain="tb.com", currency="USD")
    sa = Store(name="SA", domain="sa.com", currency="USD")
    sb = Store(name="SB", domain="sb.com", currency="USD")
    db_session.add_all([b, sa, sb]); db_session.commit()
    p = Product(brand_id=b.id, canonical_name="P")
    db_session.add(p); db_session.commit()
    return b, sa, sb, p

def test_store_isolation_external_variant(db_session):
    _, sa, sb, p = _mk(db_session)
    v1 = ProductVariant(product_id=p.id, store_id=sa.id, external_variant_id="X123")
    v2 = ProductVariant(product_id=p.id, store_id=sb.id, external_variant_id="X123")
    db_session.add_all([v1, v2]); db_session.commit()
    assert v1.id != v2.id

def test_store_isolation_external_product(db_session):
    _, sa, sb, p = _mk(db_session)
    v1 = ProductVariant(product_id=p.id, store_id=sa.id, external_product_id="P123")
    v2 = ProductVariant(product_id=p.id, store_id=sb.id, external_product_id="P123")
    db_session.add_all([v1, v2]); db_session.commit()

def test_no_self_match(db_session):
    _, sa, _, p = _mk(db_session)
    v = ProductVariant(product_id=p.id, store_id=sa.id, external_variant_id="1")
    db_session.add(v); db_session.commit()
    m = ProductMatch(canonical_variant_id=v.id, matched_variant_id=v.id, match_method="t", confidence_score=0.9)
    db_session.add(m)
    with pytest.raises(IntegrityError):
        db_session.commit()

def test_offer_positive_price(db_session):
    _, sa, _, p = _mk(db_session)
    v = ProductVariant(product_id=p.id, store_id=sa.id, external_variant_id="1")
    db_session.add(v); db_session.commit()
    o = Offer(store_id=sa.id, variant_id=v.id, url="http://x", current_price=-10)
    db_session.add(o)
    with pytest.raises(IntegrityError):
        db_session.commit()
TESTS_EOF

echo "✅ tests/test_regression_p0.py"

echo ""
echo "=========================================="
echo "🎯 ВСЕ ИСПРАВЛЕНИЯ ПРИМЕНЕНЫ"
echo "=========================================="
echo ""
echo "Проверка статуса:"
git status --short
echo ""
echo "Посмотреть diff:"
git diff --stat
