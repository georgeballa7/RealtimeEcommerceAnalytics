-- Coordinate at-most-one active AWS sync worker per canonical customer.
-- Expired leases are reclaimable following task crashes. No external Shopify call
-- is part of this PostgreSQL transaction.
CREATE TABLE shopify_customer_sync_lease (
    shop_gid TEXT NOT NULL CHECK (shop_gid LIKE 'gid://shopify/Shop/%'),
    customer_id UUID NOT NULL,
    lease_token UUID NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_gid, customer_id)
);
