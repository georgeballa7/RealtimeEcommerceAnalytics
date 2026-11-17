-- Execute using a least-privilege migration role (not automatically applied).
-- No fabricated Shopify IDs; each saved mapping is independently verified.
CREATE TABLE IF NOT EXISTS shopify_customer_identity (
    shop_gid TEXT NOT NULL CHECK (shop_gid LIKE 'gid://shopify/Shop/%'),
    customer_id UUID NOT NULL,
    source_system TEXT NOT NULL,
    source_customer_id TEXT NOT NULL,
    shopify_customer_gid TEXT NOT NULL CHECK (shopify_customer_gid LIKE 'gid://shopify/Customer/%'),
    status TEXT NOT NULL CHECK (status IN ('verified')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_gid, customer_id),
    UNIQUE (shop_gid, shopify_customer_gid),
    UNIQUE (shop_gid, source_system, source_customer_id)
);
