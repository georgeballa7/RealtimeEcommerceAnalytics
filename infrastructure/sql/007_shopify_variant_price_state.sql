-- Verified operational GBP state; do not alter historical transactions.
-- Requires migration 006. Only successful Shopify read-back may populate this table.
CREATE TABLE shopify_variant_price_state (
    shop_gid TEXT NOT NULL,
    variant_id UUID NOT NULL,
    price_gbp NUMERIC(12,2) NOT NULL CHECK (price_gbp > 0),
    currency CHAR(3) NOT NULL DEFAULT 'GBP' CHECK (currency = 'GBP'),
    shopify_verified_at TIMESTAMPTZ NOT NULL,
    manifest_sha256 CHAR(64) NOT NULL CHECK (manifest_sha256 ~ '^[a-f0-9]{64}$'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_gid, variant_id),
    FOREIGN KEY (shop_gid, variant_id)
        REFERENCES shopify_variant_identity (shop_gid, variant_id)
);
