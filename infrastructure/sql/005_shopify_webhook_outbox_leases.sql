-- Safe additive migration for lease-based at-least-once outbox publishing.
ALTER TABLE shopify_webhook_outbox
    ADD COLUMN IF NOT EXISTS claim_token UUID,
    ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_shopify_webhook_outbox_ready
    ON shopify_webhook_outbox (next_attempt_at, id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_shopify_webhook_outbox_expiring
    ON shopify_webhook_outbox (lease_until, id)
    WHERE status = 'publishing';
