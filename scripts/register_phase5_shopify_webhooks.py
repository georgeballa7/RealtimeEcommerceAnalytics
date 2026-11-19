"""Idempotently register HTTPS subscriptions on the project's Shopify app.

Use the same client_id/client_secret credentials used by the ECS generator
and the FastAPI HMAC validator. Never use the connected ChatGPT app's identity.
Run after API Gateway deployment AND successful migration 008.
"""
from __future__ import annotations
import argparse
import json
from urllib.parse import urlsplit
import boto3
import httpx

TOPICS = (
    "ORDERS_CREATE", "ORDERS_UPDATED", "ORDERS_CANCELLED",
    "ORDERS_PAID", "ORDERS_FULFILLED", "REFUNDS_CREATE",
)
LIST = """query($after:String) {
  webhookSubscriptions(first:100,after:$after) {
    nodes { id topic uri }
    pageInfo { hasNextPage endCursor }
  }
}"""
CREATE = """mutation($topic:WebhookSubscriptionTopic!,$input:WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic:$topic, webhookSubscription:$input) {
    webhookSubscription { id topic uri }
    userErrors { field message }
  }
}"""
UPDATE = """mutation($id:ID!,$input:WebhookSubscriptionInput!) {
  webhookSubscriptionUpdate(id:$id, webhookSubscription:$input) {
    webhookSubscription { id topic uri }
    userErrors { field message }
  }
}"""

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--url",required=True)
    parser.add_argument("--profile",default="ecommerce-admin")
    parser.add_argument("--region",default="eu-central-1")
    parser.add_argument("--secret-id",default="ecommerce/demo/shopify/client-credentials")
    parser.add_argument("--shop",default="realtime-ecommerce-analytics.myshopify.com")
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    url=urlsplit(args.url)
    if url.scheme!="https" or not url.hostname or url.path!="/webhooks/shopify" or url.username or url.password or url.fragment:
        parser.error("Expected deployed HTTPS /webhooks/shopify endpoint")
    session=boto3.Session(profile_name=args.profile,region_name=args.region)
    secret=json.loads(session.client("secretsmanager").get_secret_value(SecretId=args.secret_id)["SecretString"])
    client_id,client_secret=secret["client_id"],secret["client_secret"]
    with httpx.Client(timeout=30) as client:
        res=client.post(f"https://{args.shop}/admin/oauth/access_token",
            data={"grant_type":"client_credentials","client_id":client_id,"client_secret":client_secret})
        res.raise_for_status()
        token=res.json()["access_token"]
        endpoint=f"https://{args.shop}/admin/api/2026-07/graphql.json"
        def gql(query,variables=None):
            r=client.post(endpoint,json={"query":query,"variables":variables or {}},
                headers={"X-Shopify-Access-Token":token})
            r.raise_for_status()
            result=r.json()
            if result.get("errors"):
                raise RuntimeError("Shopify GraphQL rejected webhook operation: "+
                                   ",".join(str(e.get("extensions",{}).get("code","UNKNOWN"))
                                   for e in result["errors"]))
            return result["data"]
        subscriptions=[]
        cursor=None
        for _ in range(30):
            page=gql(LIST,{"after":cursor})["webhookSubscriptions"]
            subscriptions.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:break
            cursor=page["pageInfo"]["endCursor"]
        else:raise RuntimeError("Webhook pagination limit exceeded")
        for topic in TOPICS:
            rows=[s for s in subscriptions if s["topic"]==topic]
            if len(rows)>1:
                raise RuntimeError(f"Duplicate {topic} webhook subscriptions: resolve before deploying")
            if rows and rows[0]["uri"]==args.url:
                print(topic+": already configured")
                continue
            action="update" if rows else "create"
            print(topic+": "+action+(" (preview)" if not args.apply else ""))
            if not args.apply:continue
            params={"input":{"uri":args.url,"format":"JSON"}}
            if rows:params["id"]=rows[0]["id"]
            else:params["topic"]=topic
            response=gql(UPDATE if rows else CREATE,params)
            result=response["webhookSubscriptionUpdate" if rows else "webhookSubscriptionCreate"]
            if result["userErrors"] or not result["webhookSubscription"]:
                raise RuntimeError(f"Shopify {topic} webhook {action} failed; check scopes / subscription restrictions")
            created=result["webhookSubscription"]
            if created["topic"]!=topic or created["uri"]!=args.url:
                raise RuntimeError(f"Shopify {topic} webhook verification mismatch")
        print("Shopify project-app webhook registrations reconciled")

if __name__=="__main__":main()
