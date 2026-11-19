-- Phase 5 operational projection. Historical Silver is immutable.
-- Apply through existing versioned SQL migration runner, never at app startup.
CREATE TABLE IF NOT EXISTS shopify_live_order (
    shop_domain TEXT NOT NULL,
    shopify_order_id BIGINT NOT NULL,
    shopify_customer_gid TEXT,
    currency CHAR(3) NOT NULL CHECK (currency='GBP'),
    created_at_source TIMESTAMPTZ NOT NULL,
    updated_at_source TIMESTAMPTZ NOT NULL,
    cancelled_at TIMESTAMPTZ,
    financial_status TEXT,
    fulfillment_status TEXT,
    gross_total NUMERIC(16,2) NOT NULL,
    current_total NUMERIC(16,2) NOT NULL,
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_domain, shopify_order_id)
);
CREATE TABLE IF NOT EXISTS shopify_live_order_line (
    shop_domain TEXT NOT NULL,
    shopify_order_id BIGINT NOT NULL,
    shopify_line_id BIGINT NOT NULL,
    shopify_variant_id BIGINT,
    sku TEXT,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    unit_price NUMERIC(16,2) NOT NULL,
    discount_total NUMERIC(16,2) NOT NULL DEFAULT 0,
    updated_at_source TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (shop_domain, shopify_order_id, shopify_line_id),
    FOREIGN KEY (shop_domain, shopify_order_id)
      REFERENCES shopify_live_order (shop_domain, shopify_order_id)
);
CREATE TABLE IF NOT EXISTS shopify_live_refund (
    shop_domain TEXT NOT NULL,
    shopify_refund_id BIGINT NOT NULL,
    shopify_order_id BIGINT NOT NULL,
    created_at_source TIMESTAMPTZ NOT NULL,
    refund_line_subtotal NUMERIC(16,2) NOT NULL DEFAULT 0,
    payload_sha256 CHAR(64) NOT NULL,
    PRIMARY KEY (shop_domain, shopify_refund_id)
);
CREATE TABLE IF NOT EXISTS shopify_live_refund_line (
    shop_domain TEXT NOT NULL,
    shopify_refund_id BIGINT NOT NULL,
    shopify_refund_line_id BIGINT NOT NULL,
    shopify_order_line_id BIGINT,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    subtotal NUMERIC(16,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (shop_domain, shopify_refund_id, shopify_refund_line_id),
    FOREIGN KEY (shop_domain, shopify_refund_id)
      REFERENCES shopify_live_refund (shop_domain, shopify_refund_id)
);
CREATE TABLE IF NOT EXISTS shopify_live_event_ledger (
    shop_domain TEXT NOT NULL,
    webhook_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    source_event_id TEXT,
    s3_key TEXT NOT NULL,
    object_sha256 CHAR(64) NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_domain, webhook_id)
);
CREATE TABLE IF NOT EXISTS shopify_simulation_run (
    run_id TEXT PRIMARY KEY,
    scheduled_slot TEXT NOT NULL,
    expected_orders INTEGER NOT NULL CHECK(expected_orders BETWEEN 0 AND 100),
    completed_orders INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK(status IN ('running','complete','failed'))
);
CREATE INDEX IF NOT EXISTS idx_live_order_created ON shopify_live_order(created_at_source);
CREATE INDEX IF NOT EXISTS idx_live_event_processed ON shopify_live_event_ledger(processed_at);

-- Refund notifications may precede order/create delivery on a standard SQS queue.
-- Do not drop or fail the refund on missing order; expose unresolved references
-- for later reconciliation rather than claiming a cash refund.
CREATE OR REPLACE VIEW shopify_live_refund_reconciliation AS
SELECT r.shop_domain, r.shopify_refund_id, r.shopify_order_id,
       r.refund_line_subtotal,
       (o.shopify_order_id IS NOT NULL) AS order_present,
       r.created_at_source
FROM shopify_live_refund AS r
LEFT JOIN shopify_live_order AS o
  ON o.shop_domain = r.shop_domain
 AND o.shopify_order_id = r.shopify_order_id;
