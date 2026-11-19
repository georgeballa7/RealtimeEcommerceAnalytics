"""Shopify FastAPI on Lambda; load HMAC secret securely at cold start."""
import os

import boto3
from mangum import Mangum

from ecommerce.interfaces.http.shopify_webhooks import create_app


def load_webhook_secret():
    arn = os.environ["SHOPIFY_WEBHOOK_SECRET_ARN"]
    response = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
    value = response.get("SecretString")
    if not value:
        raise RuntimeError("Missing Shopify HMAC secret")
    return value


handler = Mangum(create_app(webhook_secret=load_webhook_secret()), lifespan="off")
