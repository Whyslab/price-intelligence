"""add P0 constraints
Revision ID: b2a1c3d4e5f6
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'b2a1c3d4e5f6'
down_revision: Union[str, None] = 'cd60ed470590'

def upgrade() -> None:
    op.execute("UPDATE offers SET current_price = NULL WHERE current_price <= 0")
    op.execute("UPDATE price_changes SET price = NULL WHERE price <= 0")
    op.execute("UPDATE price_history SET price = NULL WHERE price <= 0")
    op.execute("ALTER TABLE offers DROP CONSTRAINT IF EXISTS check_price_positive")
    op.execute("ALTER TABLE offers ADD CONSTRAINT check_price_positive CHECK (current_price > 0) NOT VALID")

    op.execute("ALTER TABLE offers VALIDATE CONSTRAINT check_price_positive")
    op.execute("ALTER TABLE price_history DROP CONSTRAINT IF EXISTS check_price_history_positive")
    op.execute("ALTER TABLE price_history ADD CONSTRAINT check_price_history_positive CHECK (price > 0) NOT VALID")

    op.execute("ALTER TABLE price_history VALIDATE CONSTRAINT check_price_history_positive")
    op.execute("ALTER TABLE price_changes DROP CONSTRAINT IF EXISTS check_price_change_positive")
    op.execute("ALTER TABLE price_changes ADD CONSTRAINT check_price_change_positive CHECK (price > 0) NOT VALID")

    op.execute("ALTER TABLE price_changes VALIDATE CONSTRAINT check_price_change_positive")
    op.execute("ALTER TABLE product_matches DROP CONSTRAINT IF EXISTS check_confidence_range")
    op.execute("ALTER TABLE product_matches ADD CONSTRAINT check_confidence_range CHECK (confidence_score >= 0 AND confidence_score <= 1) NOT VALID")
    op.execute("ALTER TABLE product_matches DROP CONSTRAINT IF EXISTS check_no_self_match_and_cycles")
    op.execute("ALTER TABLE product_matches ADD CONSTRAINT check_no_self_match_and_cycles CHECK (canonical_variant_id < matched_variant_id) NOT VALID")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_store_external_variant ON product_variants (store_id, external_variant_id) WHERE external_variant_id IS NOT NULL AND external_variant_id != ''")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_store_external_product ON product_variants (store_id, external_product_id) WHERE external_product_id IS NOT NULL AND external_product_id != ''")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_product_match_reverse ON product_matches (matched_variant_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_product_match_direct ON product_matches (canonical_variant_id, matched_variant_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_store_variant_offer ON offers (store_id, variant_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_price_change_open_interval ON price_changes (variant_id, store_id) WHERE ended_at IS NULL")

def downgrade() -> None:
    pass
