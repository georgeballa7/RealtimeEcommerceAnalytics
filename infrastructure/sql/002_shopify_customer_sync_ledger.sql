-- Phase 3: append-only customer synchronization attempt audit.
-- This migration creates persistence only. The worker integration remains a later step.
-- Attempt number is unique within (shop, customer); Shopify operations never run in
-- a PostgreSQL transaction and must be reconciled by custom ID after uncertainty.
CREATE TABLE shopify_customer_sync_attempt (
    shop_gid TEXT NOT NULL CHECK (shop_gid LIKE 'gid://shopify/Shop/%'),
    customer_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    phase TEXT NOT NULL CHECK (phase IN ('lookup', 'upsert', 'reconcile', 'persist')),
    outcome TEXT NOT NULL CHECK (outcome IN
        ('started', 'verified', 'not_found', 'retryable', 'ambiguous', 'failed')),
    shopify_customer_gid TEXT CHECK
        (shopify_customer_gid IS NULL OR shopify_customer_gid LIKE 'gid://shopify/Customer/%'),
    failure_code TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_gid, customer_id, attempt_number)
);

CREATE INDEX shopify_sync_attempt_latest_idx
    ON shopify_customer_sync_attempt (shop_gid, customer_id, recorded_at DESC);
