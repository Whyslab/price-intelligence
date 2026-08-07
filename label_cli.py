import sys, webbrowser
from sqlalchemy import create_engine, text
from src.config import DATABASE_URL

engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT match_id, stratum, sku, name_a, name_b, brand_a, brand_b,
               store_a, store_b, size_a, color_a, size_b, color_b,
               price_a, price_b, url_a, url_b
        FROM match_validation WHERE label IS NULL
        ORDER BY stratum, match_id
    """)).fetchall()

total = len(rows)
print(f"Remaining unlabeled: {total}")
done = 0
with engine.connect() as conn:
    for i, r in enumerate(rows, 1):
        print(f"\n[{i}/{total}] id={r.match_id} stratum={r.stratum} sku={r.sku}")
        print(f"  A: {r.name_a} | {r.brand_a} | {r.store_a} | size={r.size_a} color={r.color_a} price={r.price_a}")
        print(f"  B: {r.name_b} | {r.brand_b} | {r.store_b} | size={r.size_b} color={r.color_b} price={r.price_b}")
        print(f"  url_a: {r.url_a}")
        print(f"  url_b: {r.url_b}")
        while True:
            a = input("  1=match 0=not u=unsure o=open q=exit: ").strip().lower()
            if a == 'o':
                webbrowser.open(r.url_a); webbrowser.open(r.url_b); continue
            if a in ('1', '0'):
                conn.execute(text("UPDATE match_validation SET label=:l WHERE match_id=:id"),
                             {'l': int(a), 'id': r.match_id})
                conn.commit(); done += 1; break
            if a == 'u':
                break
            if a == 'q':
                print(f"\nSaved. Labeled this session: {done}"); sys.exit(0)
print(f"\nAll done. Labeled this session: {done}")
