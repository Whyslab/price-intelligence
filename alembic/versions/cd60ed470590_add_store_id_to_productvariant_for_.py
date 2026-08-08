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
