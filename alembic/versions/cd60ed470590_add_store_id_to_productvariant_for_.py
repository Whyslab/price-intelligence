"""add store_id to ProductVariant for store isolation
Revision ID: cd60ed470590
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session

revision: str = 'cd60ed470590'
down_revision: Union[str, None] = None

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Если БД пустая - создаем все таблицы по финальной схеме models.py
    if 'product_variants' not in inspector.get_table_names():
        from src.models import Base
        Base.metadata.create_all(bind)
        
    inspector = sa.inspect(bind)
    columns = [c['name'] for c in inspector.get_columns('product_variants')]
    if 'store_id' not in columns:
        op.add_column('product_variants', sa.Column('store_id', sa.Integer(), nullable=True))
        op.create_index('ix_product_variants_store_id', 'product_variants', ['store_id'], unique=False)
        
    session = Session(bind=bind)
    try:
        from src.models import ProductVariant, Offer, PriceChange, PriceHistory, ProductMatch
        unassigned = session.query(ProductVariant).filter(ProductVariant.store_id == None).all()
        for pv in unassigned:
            stores_offers = session.query(Offer.store_id).filter(Offer.variant_id == pv.id).distinct().all()
            stores_pc = session.query(PriceChange.store_id).filter(PriceChange.variant_id == pv.id).distinct().all()
            stores_ph = session.query(PriceHistory.store_id).filter(PriceHistory.variant_id == pv.id).distinct().all()
            all_stores = set([s[0] for s in stores_offers] + [s[0] for s in stores_pc] + [s[0] for s in stores_ph])
            if not all_stores:
                session.delete(pv)
                continue
            stores_list = list(all_stores)
            pv.store_id = stores_list[0]
            for store_id in stores_list[1:]:
                new_pv = ProductVariant(
                    product_id=pv.product_id, store_id=store_id, sku=pv.sku, ean=pv.ean,
                    external_product_id=pv.external_product_id, external_variant_id=pv.external_variant_id,
                    size=pv.size, color=pv.color, normalized_size=pv.normalized_size,
                    normalized_color=pv.normalized_color, normalized_gender_age=pv.normalized_gender_age,
                    attributes=pv.attributes
                )
                session.add(new_pv)
                session.flush()
                session.query(Offer).filter(Offer.variant_id == pv.id, Offer.store_id == store_id).update({'variant_id': new_pv.id})
                session.query(PriceChange).filter(PriceChange.variant_id == pv.id, PriceChange.store_id == store_id).update({'variant_id': new_pv.id})
                session.query(PriceHistory).filter(PriceHistory.variant_id == pv.id, PriceHistory.store_id == store_id).update({'variant_id': new_pv.id})
        session.commit()
    except Exception:
        session.rollback()
        
    try:
        op.alter_column('product_variants', 'store_id', existing_type=sa.Integer(), nullable=False)
        op.create_foreign_key('fk_product_variants_store_id', 'product_variants', 'stores', ['store_id'], ['id'], ondelete='CASCADE')
    except Exception:
        pass

def downgrade() -> None:
    pass
