"""Real Glue Silver manifest catch-up, full-snapshot and idempotency tests."""
import gzip
import io
import json
import pytest
from scripts.redshift import load_shopify_silver as x

BUCKET="test-bucket-analytics"
ROLE="arn:aws:iam::825414820081:role/warehouse"
BATCHES=["20261120T112657Z","20261120T173000Z"]
TABLES=x.TABLE_KEYS

class FakeS3:
    def __init__(self, objects):
        self.objects=objects
    def get_object(self, *, Bucket, Key):
        assert Bucket==BUCKET
        return {"Body": io.BytesIO(self.objects[Key])}
    def get_paginator(self, name):
        assert name=="list_objects_v2"
        return self
    def paginate(self, *, Bucket, Prefix):
        assert Bucket==BUCKET
        yield {"Contents":[{"Key":k} for k in self.objects if k.startswith(Prefix)]}

def make_batch(batch, orders):
    root=f"{x.PREFIX}{batch}/"
    rows={
        "shopify_customer_identity":[{"shop_gid":"s","customer_id":"1","shopify_customer_gid":"c"}],
        "shopify_live_order":[{"shop_domain":"s","shopify_order_id":i} for i in range(orders)],
        "shopify_live_order_line":[{"shop_domain":"s","shopify_order_id":i,"shopify_line_id":10+i} for i in range(orders)],
        "shopify_live_refund":[],
        "shopify_live_refund_line":[],
    }
    counts={k:{"source":len(v),"silver":len(v),"quarantined":0} for k,v in rows.items()}
    parts={k:[f"{root}silver/{k}/part-00000.json.gz"] for k in rows}
    manifest={"batch_id":batch,"schema_version":x.SCHEMA_VERSION,
        "source":"rds_jdbc_full_snapshot","counts":counts,"files":{"silver":parts}}
    objects={root+"silver_manifest.json":json.dumps(manifest,sort_keys=True).encode()}
    for k,v in rows.items():
        objects[parts[k][0]]=gzip.compress(("\n".join(json.dumps(z) for z in v)+("\n" if v else "")).encode())
    return objects

class FakeDb:
    def __init__(self):
        self.loaded=[]
    def query(self,sql):
        if "pg_namespace" in sql:return [["silver_staging"]]
        if "manifest_key, manifest_sha256" in sql and "LIMIT 1" in sql:
            return [[k,d] for k,d in self.loaded[-1:]]
        if "manifest_key, manifest_sha256" in sql:
            return [[k,d] for k,d in self.loaded]
        raise AssertionError(sql)

@pytest.fixture
def staged(monkeypatch):
    s3=FakeS3(make_batch(BATCHES[0],1)|make_batch(BATCHES[1],2))
    db=FakeDb()
    monkeypatch.setattr(x,"query",lambda client,*,sql,**kw:db.query(sql))
    monkeypatch.setattr(x,"execute_snapshot",lambda client,*,snapshot,**kw:db.loaded.append((snapshot.key,snapshot.digest)) or "statement")
    monkeypatch.setattr(x,"verify_warehouse",lambda client,*,snapshot,**kw:snapshot.counts)
    return s3,db

def call(s3,db,apply=True):
    keys=x.discover_manifests(s3,bucket=BUCKET)
    return x.process_published_snapshots(s3=s3,data=db,bucket=BUCKET,role_arn=ROLE,
        workgroup="demo",database="analytics",manifest_keys=keys,apply=apply)

def test_catchup_loads_both_old_then_new_and_never_historical(staged):
    s3,db=staged
    results=call(s3,db)
    assert [r["status"] for r in results]==["LOADED","LOADED"]
    assert [r["manifest"] for r in results]==[x.PREFIX+k+"/silver_manifest.json" for k in BATCHES]
    assert len(db.loaded)==2
    sql=x.load_sql(x.inspect_snapshot(s3,bucket=BUCKET,manifest_key=db.loaded[1][0]),bucket=BUCKET,role_arn=ROLE)
    assert not any("synthetic_orders" in s for s in sql)
    assert sql[-1].startswith("INSERT INTO silver_staging.warehouse_load_ledger")

def test_rerun_skips_existing_and_new_batch_only(staged):
    s3,db=staged
    call(s3,db)
    assert [r["status"] for r in call(s3,db)]==["ALREADY_LOADED","ALREADY_LOADED"]
    assert len(db.loaded)==2
    s3.objects.update(make_batch("20261120T183000Z",3))
    assert [r["status"] for r in call(s3,db)]==["ALREADY_LOADED","ALREADY_LOADED","LOADED"]
    assert len(db.loaded)==3

def test_preview_does_not_apply(staged):
    s3,db=staged
    assert [r["status"] for r in call(s3,db,apply=False)]==["PENDING","PENDING"]
    assert not db.loaded

def test_reject_mutated_committed_manifest(staged):
    s3,db=staged
    call(s3,db)
    old=x.PREFIX+BATCHES[0]+"/silver_manifest.json"
    doc=json.loads(s3.objects[old]);doc["published_at_utc"]="modified"
    s3.objects[old]=json.dumps(doc).encode()
    with pytest.raises(RuntimeError,match="changed"):call(s3,db)

def test_reject_reconciliation_or_corrupted_gzip(staged):
    s3,db=staged
    key=x.PREFIX+BATCHES[0]+"/silver_manifest.json"
    doc=json.loads(s3.objects[key]);doc["counts"]["shopify_live_order"]["source"]=99
    s3.objects[key]=json.dumps(doc).encode()
    with pytest.raises(ValueError,match="Unreconciled"):call(s3,db)
    s3.objects.update(make_batch(BATCHES[0],1))
    part=doc["files"]["silver"]["shopify_live_order"][0]
    s3.objects[part]=b"invalid gzip"
    with pytest.raises((gzip.BadGzipFile,EOFError,OSError)):call(s3,db)

def test_reject_duplicate_business_keys(staged):
    s3,db=staged
    key=x.PREFIX+BATCHES[0]+"/silver_manifest.json"
    doc=json.loads(s3.objects[key]);doc["counts"]["shopify_live_order"]={"source":2,"silver":2,"quarantined":0}
    s3.objects[key]=json.dumps(doc).encode()
    part=doc["files"]["silver"]["shopify_live_order"][0]
    row={"shop_domain":"s","shopify_order_id":1}
    s3.objects[part]=gzip.compress((json.dumps(row)+"\n"+json.dumps(row)+"\n").encode())
    with pytest.raises(ValueError,match="Duplicate"):call(s3,db)
