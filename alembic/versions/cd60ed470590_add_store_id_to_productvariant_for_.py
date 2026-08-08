"""add store_id to ProductVariant for store isolation

Revision ID: cd60ed470590
Revises: 
Create Date: 2026-08-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cd60ed470590'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # P0-2: Add store_id column to ProductVariant for proper store isolation
    op.add_column('product_variants', 
                  sa.Column('store_id', sa.Integer(), nullable=True))
    
    # Add foreign key constraint
    op.create_foreign_key(
        'fk_product_variants_store_id',
        'product_variants', 'stores',
        ['store_id'], ['id'],
        ondelete='SET NULL'
    )
    
    # Create index for query performance
    op.create_index(
        'ix_product_variants_store_id',
        'product_variants', ['store_id'],
        unique=False
    )
    
    # Backfill store_id from existing offers (where available)
    op.execute("""
        UPDATE product_variants pv
        SET store_id = o.store_id
        FROM offers o
        WHERE pv.store_id IS NULL
          AND o.variant_id = pv.id
    """)


def downgrade() -> None:
    op.drop_index('ix_product_variants_store_id', table_name='product_variants')
    op.drop_constraint('fk_product_variants_store_id', 'product_variants', type_='foreignkey')
    op.drop_column('product_variants', 'store_id')
