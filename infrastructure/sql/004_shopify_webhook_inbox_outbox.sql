-- Apply with the approved migration role only after review.
-- Inbox/outbox records are committed atomically before any webhook is acknowledged.
CREATE TABLE IF NOT EXISTS shopify_webhook_inbox (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shop_domain TEXT NOT NULL,
    webhook_id TEXT NOT NULL,
    event_id TEXT,
    topic TEXT NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT webhook_shop_domain_not_blank CHECK (length(shop_domain) > 0),
    CONSTRAINT webhook_id_not_blank CHECK (length(webhook_id) > 0),
    CONSTRAINT webhook_topic_not_blank CHECK (length(topic) > 0),
    UNIQUE (shop_domain, webhook_id)
);

CREATE TABLE IF NOT EXISTS shopify_webhook_outbox (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inbox_id BIGINT NOT NULL UNIQUE REFERENCES shopify_webhook_inbox(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'publishing', 'published')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_shopify_webhook_outbox_pending
    ON shopify_webhook_outbox (id)
    WHERE status = 'pending';
