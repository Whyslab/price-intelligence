"""add scoped identity constraints for external_variant_id and external_product_id

Revision ID: c3d4e5f6a7b8
Revises: b2a1c3d4e5f6
Create Date: 2026-08-08

"""
from typing import Sequence, Union
from alembic import op

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2a1c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # Scoped unique: (store_id, external_variant_id)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint 
                WHERE conname = 'uq_store_external_variant'
            ) THEN
                ALTER TABLE product_variants 
                    ADD CONSTRAINT uq_store_external_variant 
                    UNIQUE (store_id, external_variant_id);
            END IF;
        END $$;
    """)
    
    # Scoped unique: (store_id, external_product_id)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint 
                WHERE conname = 'uq_store_external_product'
            ) THEN
                ALTER TABLE product_variants 
                    ADD CONSTRAINT uq_store_external_product 
                    UNIQUE (store_id, external_product_id);
            END IF;
        END $$;
    """)

def downgrade() -> None:
    op.execute("ALTER TABLE product_variants DROP CONSTRAINT IF EXISTS uq_store_external_product")
    op.execute("ALTER TABLE product_variants DROP CONSTRAINT IF EXISTS uq_store_external_variant")
