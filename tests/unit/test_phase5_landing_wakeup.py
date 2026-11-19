"""Local tests for durable S3 landing and asynchronous controller wakeup."""
import json
from types import SimpleNamespace

from ecommerce.interfaces.workers import shopify_s3_lambda


class FakeLanding:
    def __init__(self, **kwargs):
        self.saved = []
    def persist(self, envelope):
        self.saved.append(envelope)
        return 'created'


class FakeTable:
    def __init__(self):
        self.writes = 0
        self.item = {}
    def put_item(self, **kwargs):
        self.writes += 1
        self.item = kwargs['Item']
    def update_item(self, **kwargs):
        self.writes += 1
    def get_item(self, **kwargs):
        return {'Item': self.item}


class FakeInvoker:
    def __init__(self, status):
        self.calls = []
        self.status = status
    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {'StatusCode': self.status}


def event():
    body = {'schema_version': 1, 'source': 'shopify_webhook',
            'shop_domain': 'example.myshopify.com', 'webhook_id': 'webhook-1',
            'event_id': 'event-1', 'topic': 'orders/create',
            'payload': {'id': 123}}
    return {'Records': [{'body': json.dumps(body), 'messageId': 'sqs-msg-1'}]}


def prepare(monkeypatch, status):
    storage, control = FakeTable(), FakeTable()
    invoker = FakeInvoker(status)
    tables = {'events': storage, 'control': control}
    class Resource:
        def Table(self, name):
            return tables[name]
    def fake_client(service):
        return invoker if service == 'lambda' else SimpleNamespace()
    monkeypatch.setattr(shopify_s3_lambda.boto3, 'client', fake_client)
    monkeypatch.setattr(shopify_s3_lambda.boto3, 'resource', lambda service: Resource())
    monkeypatch.setattr(shopify_s3_lambda, 'SqsToS3LandingConsumer', FakeLanding)
    monkeypatch.setenv('S3_LANDING_BUCKET', 'landing')
    monkeypatch.setenv('PENDING_EVENTS_TABLE', 'events')
    monkeypatch.setenv('CONTROLLER_TABLE', 'control')
    monkeypatch.setenv('EVENT_QUEUE_URL', 'https://sqs.example/queue')
    monkeypatch.setenv('RDS_CONTROLLER_FUNCTION', 'rds-controller')
    return storage, control, invoker


def test_landed_event_wakes_controller(monkeypatch):
    storage, control, invoker = prepare(monkeypatch, 202)
    assert shopify_s3_lambda.handler(event(), None) == {'batchItemFailures': []}
    assert storage.item['state'] == 'pending'
    assert control.writes == 1
    assert len(invoker.calls) == 1
    assert invoker.calls[0]['InvocationType'] == 'Event'
    assert invoker.calls[0]['FunctionName'] == 'rds-controller'


def test_failed_controller_wakeup_returns_sqs_retry(monkeypatch):
    storage, control, invoker = prepare(monkeypatch, 500)
    response = shopify_s3_lambda.handler(event(), None)
    assert response == {'batchItemFailures': [{'itemIdentifier': 'sqs-msg-1'}]}
    assert storage.item['state'] == 'pending'
    assert len(invoker.calls) == 1


def test_exception_waking_controller_returns_sqs_retry(monkeypatch):
    storage, control, invoker = prepare(monkeypatch, 202)
    def fail(**kwargs):
        raise RuntimeError('Temporary Lambda invoke failure')
    invoker.invoke = fail
    assert shopify_s3_lambda.handler(event(), None) == {
        'batchItemFailures': [{'itemIdentifier': 'sqs-msg-1'}]}
    assert storage.item['state'] == 'pending'


def test_unsupported_envelope_does_not_wake_controller(monkeypatch):
    storage, control, invoker = prepare(monkeypatch, 202)
    malformed = event()
    malformed['Records'][0]['body'] = '{"schema_version": 99}'
    assert shopify_s3_lambda.handler(malformed, None) == {
        'batchItemFailures': [{'itemIdentifier': 'sqs-msg-1'}]}
    assert len(invoker.calls) == 0
    assert storage.writes == 0
    assert control.writes == 0
