"""Durable event-index correctness without using AWS."""
from datetime import datetime, timezone
import pytest
from botocore.exceptions import ClientError
from ecommerce.infrastructure.aws.event_index import index_landed_event, signal_activity

class FakeIndex:
    def __init__(self): self.data = None
    def put_item(self, *, Item, ConditionExpression):
        assert ConditionExpression == 'attribute_not_exists(event_key)'
        if self.data is not None:
            raise ClientError({'Error':{'Code':'ConditionalCheckFailedException','Message':'already exists'}},'PutItem')
        self.data = dict(Item)
    def get_item(self, **kwargs): return {'Item':self.data}

class FakeControl:
    def __init__(self): self.updated = None
    def update_item(self, **kwargs): self.updated = kwargs

def test_duplicate_delivery_indexes_once():
    idx=FakeIndex()
    args=dict(shop_domain='realtime-ecommerce-analytics.myshopify.com',webhook_id='abc',
              bucket='bucket',s3_key='events/shopify/v1/x.json',checksum='0'*64,
              observed_at=datetime(2026,11,19,tzinfo=timezone.utc))
    assert index_landed_event(idx,**args)
    assert not index_landed_event(idx,**args)

def test_conflicting_duplicate_is_rejected():
    idx=FakeIndex()
    args=dict(shop_domain='test.myshopify.com',webhook_id='abc',
              bucket='bucket',s3_key='one',checksum='0'*64)
    assert index_landed_event(idx,**args)
    with pytest.raises(RuntimeError,match='Conflicting'):
        index_landed_event(idx,**(args|{'checksum':'1'*64}))

def test_activity_signal_cancels_old_idle_timer():
    control=FakeControl()
    signal_activity(control,datetime(2026,11,19,tzinfo=timezone.utc))
    assert 'REMOVE idle_since' in control.updated['UpdateExpression']
