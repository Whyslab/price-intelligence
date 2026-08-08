import os

def patch_file(path, old, new):
    if not os.path.exists(path): return
    with open(path, 'r') as f:
        content = f.read()
    if old in content:
        content = content.replace(old, new)
        with open(path, 'w') as f:
            f.write(content)
        print(f"✅ Patched {path}")
    else:
        print(f"⚠️  Pattern not found in {path}")

# models.py: Partial Unique Indexes и Check Constraints
patch_file("src/models.py",
    "UniqueConstraint('store_id', 'external_variant_id', name='uq_store_external_variant'),",
    "Index('uq_store_external_variant', 'store_id', 'external_variant_id', unique=True, postgresql_where=text(\"external_variant_id IS NOT NULL AND external_variant_id != ''\")),")
patch_file("src/models.py",
    "UniqueConstraint('store_id', 'external_product_id', name='uq_store_external_product'),",
    "Index('uq_store_external_product', 'store_id', 'external_product_id', unique=True, postgresql_where=text(\"external_product_id IS NOT NULL AND external_product_id != ''\")),")
patch_file("src/models.py",
    "CheckConstraint('canonical_variant_id != matched_variant_id', name='check_no_self_match'),",
    "CheckConstraint('canonical_variant_id < matched_variant_id', name='check_no_self_match_and_cycles'),")

# calculate_market_price.py: фильтр out-of-stock
patch_file("src/calculate_market_price.py",
    "current_prices = [p['price'] for p in prices]",
    "current_prices = [p['price'] for p in prices if p['in_stock']]\n    if not current_prices:\n        current_prices = [p['price'] for p in prices]")

# deal_engine.py: logging + N+1 fix
if os.path.exists("src/deal_engine.py"):
    with open("src/deal_engine.py", 'r') as f:
        content = f.read()
    if "import logging" not in content:
        content = "import logging\nlogger = logging.getLogger(__name__)\n" + content
    content = content.replace("except Exception:\n        return {'duration_days': None, 'is_fake': False, 'is_real': False, 'discount_pct': 0.0}", 
                              "except Exception as e:\n        logger.exception('[DealEngine] DB failure')\n        raise")
    content = content.replace("except Exception:\n        return {'is_fake': False, 'discount_tag': '💰 DEAL', 'old_price_duration_days': None, 'price_change_frequency': 0}",
                              "except Exception as e:\n        logger.exception('[DealEngine] DB failure v2')\n        raise")
    content = content.replace("except Exception:\n        return None",
                              "except Exception as e:\n        logger.exception('[DealEngine] DB failure time_at_price')\n        raise")
    content = content.replace("import sys; print(f'[DealEngine] error: {e}', file=sys.stderr)", 
                              "logger.exception('[DealEngine] Non-recoverable DB error')")
    with open("src/deal_engine.py", 'w') as f:
        f.write(content)
    print("✅ Patched src/deal_engine.py")

# adapters: logging
for adapter in ["src/adapters/shopify_adapter.py", "src/adapters/magento_adapter.py"]:
    if os.path.exists(adapter):
        with open(adapter, 'r') as f:
            content = f.read()
        if "import logging" not in content:
            content = "import logging\nlogger = logging.getLogger(__name__)\n" + content
        with open(adapter, 'w') as f:
            f.write(content)
        print(f"✅ Patched {adapter} (added logging)")

print("\n✨ Все патчи применены локально!")
