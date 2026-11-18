"""Shopify webhook HTTP ingress with raw-body HMAC verification.

Runtime credentials and database connection details must be injected via
Secrets Manager-backed environment configuration. No customer payloads are logged.
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

from ecommerce.infrastructure.postgres.webhook_inbox import (
    IncomingWebhook,
    PostgresWebhookInbox,
)

MAX_BODY_BYTES = 1_048_576


def valid_hmac(raw_body: bytes, header: str | None, secret: str) -> bool:
    if not header or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, header)


def normalize_shop_domain(value: str | None) -> str:
    """Never use the untrusted shop-domain header to construct a request URL."""
    if not value or len(value) > 253 or "/" in value or ":" in value:
        raise ValueError("Invalid Shopify shop domain")
    hostname = urlsplit("https://" + value.lower()).hostname
    if hostname != value.lower() or not hostname.endswith(".myshopify.com"):
        raise ValueError("Invalid Shopify shop domain")
    return hostname


def create_app(
    *,
    webhook_secret: str | None = None,
    inbox: PostgresWebhookInbox | None = None,
) -> FastAPI:
    secret = webhook_secret if webhook_secret is not None else os.environ.get(
        "SHOPIFY_WEBHOOK_SECRET", ""
    )
    if not secret:
        raise RuntimeError("SHOPIFY_WEBHOOK_SECRET is required")

    if inbox is None:
        import psycopg

        dsn = os.environ.get("WEBHOOK_DATABASE_URL", "")
        if not dsn:
            raise RuntimeError("WEBHOOK_DATABASE_URL is required")
        inbox = PostgresWebhookInbox(lambda: psycopg.connect(dsn, connect_timeout=5))

    app = FastAPI(title="E-Commerce Shopify Webhook Ingress")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/shopify")
    async def shopify_webhook(request: Request) -> dict[str, str]:
        # Content-Length is only an early guard; the actual body is also checked.
        length_header = request.headers.get("content-length")
        if length_header:
            try:
                length = int(length_header)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
            if length > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Webhook payload too large")
        # Stream to enforce the size limit even when Content-Length is absent.
        chunks = []
        length = 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Webhook payload too large")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not valid_hmac(raw, request.headers.get("x-shopify-hmac-sha256"), secret):
            raise HTTPException(status_code=401, detail="Invalid Shopify signature")

        webhook_id = request.headers.get("x-shopify-webhook-id", "").strip()
        topic = request.headers.get("x-shopify-topic", "").strip()
        try:
            shop_domain = normalize_shop_domain(
                request.headers.get("x-shopify-shop-domain")
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not webhook_id or len(webhook_id) > 200 or not topic or len(topic) > 200:
            raise HTTPException(status_code=400, detail="Missing webhook identity")

        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected JSON object")

        event = IncomingWebhook(
            shop_domain=shop_domain,
            webhook_id=webhook_id,
            event_id=request.headers.get("x-shopify-event-id"),
            topic=topic,
            payload=payload,
        )
        try:
            created = await run_in_threadpool(inbox.accept, event)
        except Exception as exc:
            # Do not leak DB internals, connection strings or customer payloads.
            raise HTTPException(status_code=503, detail="Webhook storage unavailable") from exc
        return {"status": "accepted" if created else "duplicate"}

    return app


# Defer application initialization until the HTTP process boots.
# Use: uvicorn ecommerce.interfaces.http.shopify_webhooks:app --factory
def app() -> FastAPI:
    return create_app()
