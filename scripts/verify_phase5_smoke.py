"""End-to-end check for one scheduled synthetic Shopify smoke order.

The event index transitions to processed only after PostgreSQL transaction
commit. Match the generated order's actual ID in the S3 event envelope.
"""
from __future__ import annotations
import argparse
import json
import re
import time
import boto3
from boto3.dynamodb.conditions import Key

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--profile",default="ecommerce-admin")
    p.add_argument("--region",default="eu-central-1")
    p.add_argument("--environment",default="demo")
    p.add_argument("--run-id",required=True)
    p.add_argument("--bucket",required=True)
    p.add_argument("--timeout-seconds",type=int,default=900)
    a=p.parse_args()
    if not re.fullmatch(r"smoke-[a-z0-9-]{4,58}",a.run_id):
        p.error("Invalid smoke run ID")
    session=boto3.Session(profile_name=a.profile,region_name=a.region)
    s3=session.client("s3")
    index=session.resource("dynamodb").Table(f"ecommerce-{a.environment}-phase5-events")
    key=f"simulation/phase5/live/v1/{a.run_id}/00000.json"
    record=json.loads(s3.get_object(Bucket=a.bucket,Key=key)["Body"].read())
    gid=record.get("order_id","")
    if record.get("status")!="complete" or not gid.startswith("gid://shopify/Order/"):
        raise RuntimeError("Smoke order not completed in Shopify")
    order_id=int(gid.rsplit("/",1)[-1])
    end=time.monotonic()+a.timeout_seconds
    while time.monotonic()<end:
        cursor=None
        inspected=0
        while True:
            params={"IndexName":"by_state",
                    "KeyConditionExpression":Key("state").eq("processed"),
                    "Limit":100}
            if cursor:params["ExclusiveStartKey"]=cursor
            response=index.query(**params)
            for item in response.get("Items",[]):
                inspected+=1
                if inspected>3000:
                    raise RuntimeError("Too many indexed events for bounded smoke verification")
                if item.get("bucket")!=a.bucket:continue
                envelope=json.loads(s3.get_object(Bucket=a.bucket,Key=item["s3_key"])["Body"].read())
                payload=envelope.get("payload",{})
                if envelope.get("topic") in ("orders/create","orders/updated") and payload.get("id")==order_id:
                    print(f"PASS: Shopify order {order_id} landed in S3 and committed to RDS")
                    return
            cursor=response.get("LastEvaluatedKey")
            if not cursor:break
        time.sleep(20)
    raise RuntimeError("Smoke order not processed into RDS before deadline")

if __name__=="__main__":main()
