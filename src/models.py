from sqlalchemy import (
    Boolean,
    UniqueConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
CheckConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base
from sqlalchemy.schema import PrimaryKeyConstraint
from sqlalchemy.sql import func

Base = declarative_base()

class Brand(Base):
    __tablename__ = 'brands'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    normalized_name = Column(String, unique=True, index=True)  # P0-12: UNIQUE на normalized_name


    name = Column(String, unique=True, nullable=False)
    domain = Column(String, nullable=False, unique=True)  # P0-12: UNIQUE constraint
    currency = Column(String(3), nullable=False)
    region = Column(String(2))
    last_sync = Column(DateTime(timezone=True))
    last_successful_sync = Column(DateTime(timezone=True))
    sync_status = Column(String(20), default='unknown')
    last_error = Column(Text)
    products_count = Column(Integer, default=0)
    reliability_score = Column(Integer, default=0)

class Product(Base):
    __tablename__ = 'products'
    id = Column(Integer, primary_key=True)
    brand_id = Column(Integer, ForeignKey('brands.id'))
    canonical_name = Column(String, nullable=False)
    category = Column(String)
    __table_args__ = (
        
        Index('ix_product_category', 'category'),)

class ProductVariant(Base):
    __tablename__ = 'product_variants'
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    sku = Column(String, index=True)
    ean = Column(String, index=True)
    external_product_id = Column(String, index=True)  # P0-11: Shopify/Magento product ID
    external_variant_id = Column(String, index=True)
    
    __table_args__ = (
        # P0-3: Composite unique constraint for scoped identity
        ('store_id', 'external_variant_id', {'name': 'uq_store_external_variant'}),
    )
    size = Column(String)
    color = Column(String)
    normalized_size = Column(String(20), index=True)
    normalized_color = Column(String(20), index=True)
    normalized_gender_age = Column(String(20), index=True)
    attributes = Column(JSONB)
    __table_args__ = (
        Index('ix_variant_ean', 'ean'),
        Index('ix_variant_sku', 'sku'),
    )

class Offer(Base):
    __tablename__ = 'offers'
    id = Column(Integer, primary_key=True)
    __table_args__ = (
        # P0-17: CHECK constraint for positive price
        CheckConstraint('current_price > 0', name='check_price_positive'),
    )
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=False)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=False)
    url = Column(Text, nullable=False)
    current_price = Column(Numeric(10, 2), nullable=False)
    old_price = Column(Numeric(10, 2))
    in_stock = Column(Boolean, default=True)
    original_currency = Column(String(3))
    exchange_rate = Column(Numeric(12, 6))
    exchange_rate_timestamp = Column(DateTime(timezone=True))
    exchange_rate_source = Column(String(50))  # P0-72: 'fixer_io', 'fallback', 'api'
    currency_source = Column(String(50))  # P0-68: 'api', 'domain', 'manual'
    parser_version = Column(String(20), default='1.0')  # P0-70
    raw_snapshot_id = Column(Integer, ForeignKey('raw_snapshots.id'))  # P0-69
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (
          # P0-12: UNIQUE constraint
    )

class PriceHistory(Base):
    __tablename__ = 'price_history'
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=False)
    __table_args__ = (
        # P0-17: CHECK constraint for positive price
        CheckConstraint('price > 0', name='check_price_history_positive'),
    )
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    price = Column(Numeric(10, 2), nullable=False)
    old_price = Column(Numeric(10, 2))
    original_currency = Column(String(3))
    exchange_rate = Column(Numeric(12, 6))
    __table_args__ = (
        PrimaryKeyConstraint('variant_id', 'store_id', 'timestamp'),
        Index('ix_price_history_variant_time', 'variant_id', 'timestamp'),
    )

class PriceChange(Base):
    __tablename__ = 'price_changes'
    variant_id = Column(Integer, ForeignKey('product_variants.id'), primary_key=True)
    __table_args__ = (
        # P0-17: CHECK constraint for positive price
        CheckConstraint('price > 0', name='check_price_change_positive'),
    )
    store_id = Column(Integer, ForeignKey('stores.id'), primary_key=True)
    started_at = Column(DateTime(timezone=True), primary_key=True)
    price = Column(Numeric(10, 2), nullable=False)
    old_price = Column(Numeric(10, 2))
    ended_at = Column(DateTime(timezone=True))
    original_currency = Column(String(3))
    exchange_rate = Column(Numeric(12, 6))
    exchange_rate_source = Column(String(50))  # P0-72
    parser_version = Column(String(20), default='1.0')  # P0-70
    raw_snapshot_id = Column(Integer, ForeignKey('raw_snapshots.id'))  # P0-69
    # P1-24/25: Контекстные поля для точной истории
    normalized_size = Column(String(20))  # P1-24: размер варианта в момент записи
    in_stock = Column(Boolean, default=True)  # P1-25: был ли товар в наличии
    region = Column(String(2))  # P1-26: регион магазина (EU/US/UK/...)

class ProductMatch(Base):
    __tablename__ = 'product_matches'
    id = Column(Integer, primary_key=True)
    __table_args__ = (
        # P0-18: CHECK constraint for confidence range
        CheckConstraint('confidence_score >= 0 AND confidence_score <= 1', name='check_confidence_range'),
        UniqueConstraint('canonical_variant_id', 'matched_variant_id', name='uq_product_match'),
        CheckConstraint('canonical_variant_id != matched_variant_id', name='check_no_self_match'),
    )
    canonical_variant_id = Column(Integer, ForeignKey('product_variants.id', ondelete='CASCADE'), nullable=False)
    matched_variant_id = Column(Integer, ForeignKey('product_variants.id', ondelete='CASCADE'), nullable=False)
    match_method = Column(String(50), nullable=False)
    confidence_score = Column(Numeric(3, 2), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
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
    
    __table_args__ = (
        Index('idx_deal_alerts_sku_store_at', 'sku', 'store_id', 'sent_at', unique=True),
    )


class RawSnapshot(Base):
    """P0-69: Raw Data Layer — сохраняет оригинальный HTTP response для дебага."""
    __tablename__ = 'raw_snapshots'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=False)
    pipeline_run_id = Column(Integer, ForeignKey('pipeline_runs.id'), nullable=True)
    adapter_name = Column(String(50), nullable=False)  # 'shopify', 'magento'
    url = Column(Text, nullable=False)
    http_status = Column(Integer)
    raw_payload = Column(JSONB, nullable=False)  # Оригинальный JSON
    response_headers = Column(JSONB)  # HTTP headers (для debug rate limits)
    parser_version = Column(String(20), default='1.0')
    products_count = Column(Integer, default=0)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index('ix_raw_snapshot_store_time', 'store_id', 'fetched_at'),
        Index('ix_raw_snapshot_pipeline', 'pipeline_run_id'),
    )
