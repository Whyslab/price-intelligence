"""
Data Provenance Layer (P0-69/70/72): централизованная работа с raw snapshots.
Отвечает за:
- Сохранение оригинальных HTTP responses в БД
- Версионирование парсеров
- Привязку цен к конкретным snapshots
"""
from sqlalchemy.orm import Session

from src.models import RawSnapshot

PARSER_VERSION = "1.0"  # P0-70: версия парсера, обновляем при изменениях логики


def save_raw_snapshot(
    db: Session,
    store_id: int,
    adapter_name: str,
    url: str,
    http_status: int,
    payload: dict | list,
    response_headers: dict = None,
    pipeline_run_id: int = None
) -> int:
    """
    Сохраняет raw snapshot в БД. Возвращает ID созданной записи.
    
    Args:
        db: SQLAlchemy session
        store_id: ID магазина
        adapter_name: 'shopify' | 'magento'
        url: URL запроса
        http_status: HTTP status code
        payload: Оригинальный JSON response
        response_headers: HTTP headers (опционально)
        pipeline_run_id: ID запуска pipeline (опционально)
    
    Returns:
        int: ID созданного snapshot
    """
    products_count = 0
    if isinstance(payload, dict):
        products_count = payload.get('total_products', len(payload.get('products', [])))
    elif isinstance(payload, list):
        products_count = len(payload)
    
    snapshot = RawSnapshot(
        store_id=store_id,
        pipeline_run_id=pipeline_run_id,
        adapter_name=adapter_name,
        url=url,
        http_status=http_status,
        raw_payload=payload,
        response_headers=response_headers,
        parser_version=PARSER_VERSION,
        products_count=products_count
    )
    db.add(snapshot)
    db.flush()
    return snapshot.id


def get_provenance_metadata(snapshot_id: int = None) -> dict:
    """
    Возвращает словарь с provenance полями для Offer/PriceChange.
    
    Usage:
        provenance = get_provenance_metadata(snapshot_id=123)
        offer = Offer(..., **provenance)
    """
    return {
        'parser_version': PARSER_VERSION,
        'raw_snapshot_id': snapshot_id,
        'exchange_rate_source': 'fixer_io',  # будет переопределяться в currency_normalizer
    }
