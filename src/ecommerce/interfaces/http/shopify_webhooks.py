"""Shopify HMAC-verified, RDS-independent FastAPI webhook ingress.

Acknowledge only after SQS confirms the event; RDS can remain stopped.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ecommerce.infrastructure.aws.webhook_queue import (
    IncomingWebhook, SqsWebhookQueue, WebhookQueueError,
)

MAX_BODY_BYTES = 1_000_000
TOPICS = frozenset({
    "orders/create", "orders/updated", "orders/cancelled",
    "refunds/create", "orders/paid", "orders/fulfilled",
})


def valid_hmac(raw_body: bytes, header: str | None, secret: str) -> bool:
    if not header or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, header)


def normalize_shop_domain(value: str | None) -> str:
    if not value or len(value) > 253 or "/" in value or ":" in value:
        raise ValueError("Invalid Shopify shop domain")
    hostname = urlsplit("https://" + value.lower()).hostname
    if hostname != value.lower() or not hostname.endswith(".myshopify.com"):
        raise ValueError("Invalid Shopify shop domain")
    return hostname


def create_app(*, webhook_secret: str | None = None,
               queue: SqsWebhookQueue | None = None,
               expected_shop: str | None = None) -> FastAPI:
    secret = webhook_secret if webhook_secret is not None else os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError("SHOPIFY_WEBHOOK_SECRET is required")
    shop = expected_shop if expected_shop is not None else os.environ.get("SHOPIFY_SHOP_DOMAIN", "")
    if not shop:
        raise RuntimeError("SHOPIFY_SHOP_DOMAIN is required")
    shop = normalize_shop_domain(shop)
    if queue is None:
        import boto3
        queue = SqsWebhookQueue(boto3.client("sqs"), os.environ.get("EVENT_QUEUE_URL", ""))

    app = FastAPI(title="Shopify Live Event Ingestion")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/shopify")
    async def shopify_webhook(request: Request) -> dict[str, str]:
        length_header = request.headers.get("content-length")
        if length_header:
            try:
                length = int(length_header)
            except ValueError as exc:
                raise HTTPException(400, "Invalid Content-Length") from exc
            if length > MAX_BODY_BYTES:
                raise HTTPException(413, "Webhook payload too large")
        chunks = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise HTTPException(413, "Webhook payload too large")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not valid_hmac(raw, request.headers.get("x-shopify-hmac-sha256"), secret):
            raise HTTPException(401, "Invalid Shopify signature")
        webhook_id = (request.headers.get("x-shopify-webhook-id") or "").strip()
        topic = (request.headers.get("x-shopify-topic") or "").strip()
        try:
            source_shop = normalize_shop_domain(request.headers.get("x-shopify-shop-domain"))
        except ValueError as exc:
            raise HTTPException(400, "Invalid Shopify shop identity") from exc
        if source_shop != shop:
            raise HTTPException(403, "Unexpected Shopify shop")
        if not webhook_id or len(webhook_id) > 200 or topic not in TOPICS:
            raise HTTPException(400, "Unsupported Shopify event identity or topic")
        event_id = request.headers.get("x-shopify-event-id")
        if event_id is not None and len(event_id) > 200:
            raise HTTPException(400, "Invalid Shopify event ID")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "Expected JSON object")
        event = IncomingWebhook(source_shop, webhook_id, event_id, topic, payload)
        try:
            await run_in_threadpool(queue.accept, event)
        except ValueError as exc:
            raise HTTPException(413, "Webhook envelope exceeds transport limit") from exc
        except WebhookQueueError as exc:
            raise HTTPException(503, "Webhook queue unavailable") from exc
        return {"status": "queued"}

    return app


def app() -> FastAPI:
    """ASGI factory for uvicorn; Lambda handler wraps the same FastAPI app."""
    return create_app()
