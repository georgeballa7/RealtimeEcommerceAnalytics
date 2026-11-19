"""Set the Phase 5 readiness flag only after private schema task succeeds."""
from __future__ import annotations
import argparse
import boto3

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--profile",default="ecommerce-admin")
    parser.add_argument("--region",default="eu-central-1")
    parser.add_argument("--environment",default="demo")
    parser.add_argument("--webhooks-ready",action="store_true")
    args=parser.parse_args()
    session=boto3.Session(profile_name=args.profile,region_name=args.region)
    table=session.resource("dynamodb").Table(f"ecommerce-{args.environment}-phase5-controller")
    if args.webhooks_ready:
        table.update_item(
            Key={"id":"phase5-controller"},
            UpdateExpression="SET webhooks_ready = :ready",
            ConditionExpression="schema_ready = :ready",
            ExpressionAttributeValues={":ready":True},
        )
        print("Project-app Shopify webhook readiness recorded")
    else:
        table.update_item(
            Key={"id":"phase5-controller"},
            UpdateExpression="SET schema_ready = :ready",
            ExpressionAttributeValues={":ready":True},
        )
        print("Phase 5 schema readiness recorded after successful migration")

if __name__=="__main__":main()
