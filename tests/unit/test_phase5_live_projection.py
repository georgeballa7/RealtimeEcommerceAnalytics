import hashlib
import json
from datetime import timezone
from decimal import Decimal
import pytest
from ecommerce.application.use_cases.live_order_projection import (
    positive_id, money, timestamp, order_details, InvalidShopifyEvent,
)


def test_order_details_source_timestamps_currency_and_tags():
    record = {"id":123, "customer":{"id":77}, "currency":"GBP",
              "created_at":"2026-11-19T08:00:00Z", "updated_at":"2026-11-19T08:02:00Z",
              "total_price":"20.00","current_total_price":"18.00",
              "tags":"ecommerce-synthetic, phase5-live", "line_items":[]}
    o = order_details(record)
    assert o["id"] == 123 and o["current"] == Decimal("18.00")
    assert o["synthetic"] is True and o["updated"].tzinfo is not None


def test_refuses_invalid_ids_money_and_timestamps():
    for x in [None, 0, -1, True, "ABC"]:
        with pytest.raises(InvalidShopifyEvent): positive_id(x,"id")
    for x in ["-1", "NaN", "1.234"]:
        with pytest.raises(InvalidShopifyEvent): money(x,"price")
    for x in [None,"2026-11-19T09:00:00"]:
        with pytest.raises(InvalidShopifyEvent): timestamp(x,"time")


def test_refuses_unexpected_currency():
    with pytest.raises(InvalidShopifyEvent,match="currency"):
        order_details({"id":1,"currency":"USD"})
