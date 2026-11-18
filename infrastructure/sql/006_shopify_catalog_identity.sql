-- Catalog identity ledger. Run only via the reviewed versioned migration process.
-- Product and variant GIDs are separate, Shopify-returned identities.
-- No inferred variant IDs; no mutation of canonical historical Silver.
CREATE TABLE shopify_product_identity (
    shop_gid TEXT NOT NULL CHECK (shop_gid LIKE 'gid://shopify/Shop/%'),
    product_id UUID NOT NULL,
    source_system TEXT NOT NULL,
    source_stock_code TEXT NOT NULL,
    shopify_product_gid TEXT NOT NULL CHECK (shopify_product_gid LIKE 'gid://shopify/Product/%'),
    status TEXT NOT NULL DEFAULT 'verified' CHECK (status = 'verified'),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_gid, product_id),
    UNIQUE (shop_gid, source_system, source_stock_code),
    UNIQUE (shop_gid, shopify_product_gid),
    UNIQUE (shop_gid, product_id, shopify_product_gid)
);

CREATE TABLE shopify_variant_identity (
    shop_gid TEXT NOT NULL CHECK (shop_gid LIKE 'gid://shopify/Shop/%'),
    variant_id UUID NOT NULL,
    product_id UUID NOT NULL,
    shopify_product_gid TEXT NOT NULL CHECK (shopify_product_gid LIKE 'gid://shopify/Product/%'),
    sku TEXT NOT NULL,
    shopify_variant_gid TEXT NOT NULL CHECK (shopify_variant_gid LIKE 'gid://shopify/ProductVariant/%'),
    status TEXT NOT NULL DEFAULT 'verified' CHECK (status = 'verified'),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_gid, variant_id),
    UNIQUE (shop_gid, sku),
    UNIQUE (shop_gid, shopify_variant_gid),
    FOREIGN KEY (shop_gid, product_id, shopify_product_gid)
        REFERENCES shopify_product_identity (shop_gid, product_id, shopify_product_gid)
);

-- Claim leasing prevents two workers from creating the same catalog product.
-- Leases are reclaimed only after expiry; the worker must check Shopify before
-- any mutation and reconcile afterward. Never rely on the lease for exactly-once.
CREATE TABLE shopify_catalog_sync_lease (
    shop_gid TEXT NOT NULL CHECK (shop_gid LIKE 'gid://shopify/Shop/%'),
    product_id UUID NOT NULL,
    lease_token UUID NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_gid, product_id)
);
