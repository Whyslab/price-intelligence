"""add scoped identity constraints
Revision ID: c3d4e5f6a7b8
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2a1c3d4e5f6'

def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_store_external_variant")
    op.execute("DROP INDEX IF EXISTS uq_store_external_product")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_store_external_variant ON product_variants (store_id, external_variant_id) WHERE external_variant_id IS NOT NULL AND external_variant_id != ''")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_store_external_product ON product_variants (store_id, external_product_id) WHERE external_product_id IS NOT NULL AND external_product_id != ''")

def downgrade() -> None:
    pass
