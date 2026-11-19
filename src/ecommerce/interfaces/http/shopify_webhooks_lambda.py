"""FastAPI Lambda entrypoint using the project's existing Shopify app secret.

Shopify webhook HMAC is the app client secret, not the OAuth access token.
The secret may be a JSON object in Secrets Manager.
"""
from __future__ import annotations
import json
import os
import boto3
from mangum import Mangum
from ecommerce.interfaces.http.shopify_webhooks import create_app

def load_webhook_secret() -> str:
    arn = os.environ["SHOPIFY_WEBHOOK_SECRET_ARN"]
    response = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
    value = response.get("SecretString")
    if not value:
        raise RuntimeError("Missing Shopify HMAC secret")
    try:
        document = json.loads(value)
    except json.JSONDecodeError:
        document = value
    if isinstance(document, dict):
        value = document.get("client_secret")
    else:
        value = document
    if not isinstance(value, str) or not value:
        raise RuntimeError("Shopify app client_secret unavailable")
    return value

handler = Mangum(create_app(webhook_secret=load_webhook_secret()), lifespan="off")
