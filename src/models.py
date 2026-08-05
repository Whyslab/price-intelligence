from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Numeric, Boolean, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func
from sqlalchemy.schema import PrimaryKeyConstraint

Base = declarative_base()

class Brand(Base):
    __tablename__ = 'brands'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    normalized_name = Column(String, index=True)

class Store(Base):
    __tablename__ = 'stores'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    domain = Column(String, nullable=False)
    currency = Column(String(3), nullable=False)
    region = Column(String(2))

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
    sku = Column(String, index=True)
    ean = Column(String, index=True)
    size = Column(String)
    color = Column(String)
    attributes = Column(JSONB)
    __table_args__ = (
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
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        Index('ix_offer_store_variant', 'store_id', 'variant_id', unique=True),
    )

class PriceHistory(Base):
    __tablename__ = 'price_history'
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=False)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    price = Column(Numeric(10, 2), nullable=False)
    old_price = Column(Numeric(10, 2))
    __table_args__ = (
        PrimaryKeyConstraint('variant_id', 'store_id', 'timestamp'),
        Index('ix_price_history_variant_time', 'variant_id', 'timestamp'),
    )