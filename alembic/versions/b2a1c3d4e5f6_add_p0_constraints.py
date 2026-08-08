"""add P0 constraints: check price>0, no self-match, no duplicate mapping

Revision ID: b2a1c3d4e5f6
Revises: cd60ed470590
Create Date: 2026-08-08

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'b2a1c3d4e5f6'
down_revision: Union[str, None] = 'cd60ed470590'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # ========================================
    # STEP 1: Clean invalid data (price <= 0)
    # ========================================
    # Удаляем "мусор" — записи с некорректной ценой
    # Это нужно чтобы CHECK-констрейнты могли быть созданы
    op.execute("""
        DELETE FROM offers WHERE current_price <= 0
    """)
    op.execute("""
        DELETE FROM price_changes WHERE price <= 0
    """)
    op.execute("""
        DELETE FROM price_history WHERE price <= 0
    """)
    
    # Удаляем варианты, которые остались без offers после очистки
    op.execute("""
        DELETE FROM product_variants pv
        WHERE NOT EXISTS (
            SELECT 1 FROM offers o WHERE o.variant_id = pv.id
        )
        AND NOT EXISTS (
            SELECT 1 FROM price_changes pc WHERE pc.variant_id = pv.id
        )
    """)
    
    # ========================================
    # STEP 2: Add CHECK constraints (price > 0)
    # ========================================
    # Использовать IF NOT EXISTS-подобную логику нельзя, поэтому
    # делаем через DO-блок с EXCEPTION
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'check_price_positive'
            ) THEN
                ALTER TABLE offers ADD CONSTRAINT check_price_positive 
                    CHECK (current_price > 0);
            END IF;
        END $$;
    """)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'check_price_history_positive'
            ) THEN
                ALTER TABLE price_history ADD CONSTRAINT check_price_history_positive 
                    CHECK (price > 0);
            END IF;
        END $$;
    """)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'check_price_change_positive'
            ) THEN
                ALTER TABLE price_changes ADD CONSTRAINT check_price_change_positive 
                    CHECK (price > 0);
            END IF;
        END $$;
    """)
    
    # ========================================
    # STEP 3: ProductMatch constraints
    # ========================================
    # 3a. Confidence range
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'check_confidence_range'
            ) THEN
                ALTER TABLE product_matches ADD CONSTRAINT check_confidence_range 
                    CHECK (confidence_score >= 0 AND confidence_score <= 1);
            END IF;
        END $$;
    """)
    
    # 3b. No self-match
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'check_no_self_match'
            ) THEN
                ALTER TABLE product_matches ADD CONSTRAINT check_no_self_match 
                    CHECK (canonical_variant_id != matched_variant_id);
            END IF;
        END $$;
    """)
    
    # 3c. No duplicate mapping: matched_variant_id must be unique
    # (каждый matched_variant может иметь только один canonical)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'uq_product_match_reverse'
            ) THEN
                ALTER TABLE product_matches 
                    ADD CONSTRAINT uq_product_match_reverse 
                    UNIQUE (matched_variant_id);
            END IF;
        END $$;
    """)
    
    # 3d. Rename old direct unique constraint to match new name
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint 
                WHERE conname = 'product_matches_canonical_variant_id_matched_variant_id_key'
            ) THEN
                ALTER TABLE product_matches 
                    RENAME CONSTRAINT product_matches_canonical_variant_id_matched_variant_id_key 
                    TO uq_product_match_direct;
            END IF;
        END $$;
    """)
    
    # ========================================
    # STEP 4: Offer uniqueness per store+variant
    # ========================================
    # Сначала удаляем дубликаты offers (если есть)
    op.execute("""
        DELETE FROM offers a
        USING offers b
        WHERE a.ctid < b.ctid
          AND a.store_id = b.store_id
          AND a.variant_id = b.variant_id
    """)
    
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'uq_store_variant_offer'
            ) THEN
                ALTER TABLE offers 
                    ADD CONSTRAINT uq_store_variant_offer 
                    UNIQUE (store_id, variant_id);
            END IF;
        END $$;
    """)
    
    # ========================================
    # STEP 5: PriceChange — only ONE open interval
    # Partial unique index (ended_at IS NULL)
    # ========================================
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes WHERE indexname = 'uq_price_change_open_interval'
            ) THEN
                CREATE UNIQUE INDEX uq_price_change_open_interval 
                    ON price_changes (variant_id, store_id)
                    WHERE ended_at IS NULL;
            END IF;
        END $$;
    """)

def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_price_change_open_interval")
    op.execute("ALTER TABLE offers DROP CONSTRAINT IF EXISTS uq_store_variant_offer")
    op.execute("ALTER TABLE product_matches DROP CONSTRAINT IF EXISTS uq_product_match_reverse")
    op.execute("ALTER TABLE product_matches DROP CONSTRAINT IF EXISTS check_no_self_match")
    op.execute("ALTER TABLE product_matches DROP CONSTRAINT IF EXISTS check_confidence_range")
    op.execute("ALTER TABLE price_changes DROP CONSTRAINT IF EXISTS check_price_change_positive")
    op.execute("ALTER TABLE price_history DROP CONSTRAINT IF EXISTS check_price_history_positive")
    op.execute("ALTER TABLE offers DROP CONSTRAINT IF EXISTS check_price_positive")
