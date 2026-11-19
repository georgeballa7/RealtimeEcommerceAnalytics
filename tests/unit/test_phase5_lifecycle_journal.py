import io
import hashlib
import pytest
from botocore.exceptions import ClientError
from ecommerce.infrastructure.aws.lifecycle_journal import S3LifecycleJournal

KEY = '614e41ba-04d8-5aa2-8b7d-1d0dd8b2a4f0'

class FakeS3:
    def __init__(self): self.stored = {}; self.calls=[]
    def get_object(self, **kw):
        k=kw['Key']
        if k not in self.stored:
            raise ClientError({'Error':{'Code':'NoSuchKey','Message':'missing'}},'GetObject')
        return {'Body': io.BytesIO(self.stored[k]), 'ETag': self.etag(k)}
    def etag(self, key):
        return '"' + hashlib.md5(self.stored[key]).hexdigest() + '"'
    def put_object(self, **kw):
        self.calls.append(kw)
        if kw.get('IfMatch') and (kw['Key'] not in self.stored or kw['IfMatch'] != self.etag(kw['Key'])):
            raise ClientError({'Error':{'Code':'PreconditionFailed'}},'PutObject')
        if kw.get('IfNoneMatch')=='*' and kw['Key'] in self.stored:
            raise ClientError({'Error':{'Code':'PreconditionFailed'}},'PutObject')
        self.stored[kw['Key']]=kw['Body']
        return {'ETag': self.etag(kw['Key'])}

def test_new_operation_reserved_once():
    s3=FakeS3();j=S3LifecycleJournal(s3,'landing')
    assert j.get(KEY) is None
    j.put_new(KEY, {'state':'reserved'})
    assert j.get(KEY)=={'state':'reserved'}
    assert s3.calls[-1]['IfNoneMatch']=='*'
    with pytest.raises(RuntimeError,match='already reserved'):
        j.put_new(KEY, {'state':'reserved'})

def test_state_transition_written_and_read_back():
    s3=FakeS3();j=S3LifecycleJournal(s3,'landing')
    j.put_new(KEY, {'state':'reserved'})
    j.put(KEY, {'state':'complete','refund_id':'gid://shopify/Refund/1'})
    assert j.get(KEY)['state']=='complete'

def test_key_rejects_untrusted_path():
    with pytest.raises(ValueError):
        S3LifecycleJournal.path('../../evil')


def test_stale_etag_refuses_concurrent_update():
    s3=FakeS3();first=S3LifecycleJournal(s3,'landing')
    second=S3LifecycleJournal(s3,'landing')
    first.put_new(KEY, {'state':'reserved'})
    assert second.get(KEY)['state']=='reserved'
    first.put(KEY, {'state':'cancel_requested'})
    with pytest.raises(RuntimeError, match='Concurrent operation update'):
        second.put(KEY, {'state':'complete'})
    assert first.get(KEY)['state']=='cancel_requested'
