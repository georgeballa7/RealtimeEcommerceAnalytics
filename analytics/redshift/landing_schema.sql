-- Warehouse landing schema: source-shaped staging, not an assertion of Silver validation.
-- The loader creates the landing schema after checking pg_namespace.

CREATE TABLE IF NOT EXISTS landing.synthetic_orders (
  order_id VARCHAR(40), customer_id VARCHAR(40), source_system VARCHAR(40),
  synthetic BOOLEAN, event_type VARCHAR(40), event_id VARCHAR(40),
  event_time_utc TIMESTAMPTZ, currency VARCHAR(3), order_total DECIMAL(18,2),
  basket_mode VARCHAR(40), batch_id VARCHAR(100), policy_sha256 VARCHAR(64)
) DISTSTYLE AUTO SORTKEY(event_time_utc);

CREATE TABLE IF NOT EXISTS landing.synthetic_order_items (
  order_item_id VARCHAR(40), order_id VARCHAR(40), product_id VARCHAR(40),
  variant_id VARCHAR(40), source_stock_code VARCHAR(128), quantity INT,
  unit_price DECIMAL(18,2), line_amount DECIMAL(18,2), currency VARCHAR(3),
  currency_assumption VARCHAR(128), pricing_method VARCHAR(128),
  synthetic BOOLEAN, source_system VARCHAR(40), event_time_utc TIMESTAMPTZ
) DISTSTYLE AUTO SORTKEY(order_id);

CREATE TABLE IF NOT EXISTS landing.synthetic_cancellations (
  event_id VARCHAR(40), event_type VARCHAR(40), order_id VARCHAR(40),
  order_item_id VARCHAR(40), customer_id VARCHAR(40), product_id VARCHAR(40),
  variant_id VARCHAR(40), source_stock_code VARCHAR(128),
  cancelled_quantity INT, unit_price DECIMAL(18,2),
  cancelled_amount DECIMAL(18,2), financial_semantics VARCHAR(128),
  event_time_utc TIMESTAMPTZ, original_order_time_utc TIMESTAMPTZ,
  currency VARCHAR(3), synthetic BOOLEAN, source_system VARCHAR(40),
  provenance VARCHAR(128), batch_id VARCHAR(100), policy_sha256 VARCHAR(64)
) DISTSTYLE AUTO SORTKEY(event_time_utc);

CREATE TABLE IF NOT EXISTS landing.shopify_live_order (
  shop_domain VARCHAR(255), shopify_order_id BIGINT, shopify_customer_gid VARCHAR(255),
  currency VARCHAR(3), created_at_source TIMESTAMPTZ, updated_at_source TIMESTAMPTZ,
  cancelled_at TIMESTAMPTZ, financial_status VARCHAR(80),
  fulfillment_status VARCHAR(80), gross_total DECIMAL(18,2),
  current_total DECIMAL(18,2), synthetic BOOLEAN, ingested_at TIMESTAMPTZ
) DISTSTYLE AUTO SORTKEY(created_at_source);

CREATE TABLE IF NOT EXISTS landing.shopify_live_order_line (
  shop_domain VARCHAR(255), shopify_order_id BIGINT, shopify_line_id BIGINT,
  shopify_variant_id BIGINT, sku VARCHAR(128), quantity INT,
  unit_price DECIMAL(18,2), discount_total DECIMAL(18,2),
  updated_at_source TIMESTAMPTZ
) DISTSTYLE AUTO SORTKEY(shopify_order_id);

CREATE TABLE IF NOT EXISTS landing.shopify_live_refund (
  shop_domain VARCHAR(255), shopify_refund_id BIGINT, shopify_order_id BIGINT,
  created_at_source TIMESTAMPTZ, refund_line_subtotal DECIMAL(18,2),
  payload_sha256 VARCHAR(64)
) DISTSTYLE AUTO SORTKEY(shopify_order_id);

CREATE TABLE IF NOT EXISTS landing.shopify_live_refund_line (
  shop_domain VARCHAR(255), shopify_refund_id BIGINT, shopify_refund_line_id BIGINT,
  shopify_order_line_id BIGINT, quantity INT, subtotal DECIMAL(18,2)
) DISTSTYLE AUTO SORTKEY(shopify_refund_id);

CREATE TABLE IF NOT EXISTS landing.shopify_customer_identity (
  shop_gid VARCHAR(255), customer_id VARCHAR(40), source_system VARCHAR(80),
  source_customer_id VARCHAR(80), shopify_customer_gid VARCHAR(255),
  status VARCHAR(40), updated_at TIMESTAMPTZ
) DISTSTYLE AUTO SORTKEY(shopify_customer_gid);

CREATE TABLE IF NOT EXISTS landing.warehouse_load_ledger (
  load_id VARCHAR(256), manifest_key VARCHAR(1024), manifest_sha256 VARCHAR(64),
  source_type VARCHAR(40), loaded_at TIMESTAMPTZ DEFAULT GETDATE()
);
