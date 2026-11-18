"""Deterministic, source-scoped canonical IDs for Silver entities.

These are *our* identifiers, not Shopify GraphQL IDs. Once a Shopify object
exists, record its returned GID as an external identity mapping.
"""
from uuid import UUID, uuid5

# Fixed project-wide namespace; changing it would remap all existing identities.
CANONICAL_NAMESPACE = UUID('89127d7c-9518-4c2c-9749-6a2cbb76de42')
UCI_SOURCE = 'uci_online_retail_ii'


def canonical_id(entity: str, source_system: str, source_id: str) -> str:
    """Stable UUID from an entity, source namespace and nonempty source ID."""
    if not all(isinstance(value, str) and value.strip() for value in (entity, source_system, source_id)):
        raise ValueError('entity, source_system and source_id must be nonempty strings')
    # Normalize only surrounding whitespace; source identifiers are otherwise opaque.
    key = f'{entity.strip()}\x1f{source_system.strip()}\x1f{source_id.strip()}'
    return str(uuid5(CANONICAL_NAMESPACE, key))


def historical_customer_id(source_customer_id: str | None) -> str | None:
    """Keep anonymous UCI rows anonymous rather than fabricating a customer."""
    if source_customer_id is None or not source_customer_id.strip():
        return None
    return canonical_id('customer', UCI_SOURCE, source_customer_id)


def historical_order_id(invoice_id: str) -> str:
    """Invoice ID is source-scoped and includes cancellation invoice IDs."""
    return canonical_id('order', UCI_SOURCE, invoice_id)


def historical_product_id(stock_code: str) -> str:
    """Product identity follows UCI stock code, not mutable description."""
    return canonical_id('product', UCI_SOURCE, stock_code)


def historical_order_item_id(source_record_key: str) -> str:
    """Use the retained source row key, keeping repeated invoice lines distinct."""
    return canonical_id('order_item', UCI_SOURCE, source_record_key)
