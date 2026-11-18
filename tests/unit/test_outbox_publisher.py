import json
from ecommerce.application.use_cases.outbox_publisher import (
    ClaimedEvent, SqsOutboxPublisher, PostgresOutbox,
)


def item():
    return ClaimedEvent(
        outbox_id=7, webhook_id='hook-7', shop_domain='example.myshopify.com',
        topic='orders/create', event_id='evt-7', payload={'id': 123},
        claim_token='00000000-0000-4000-8000-000000000001',
    )


class FakeRepo:
    def __init__(self, event):
        self.event = event
        self.published = []
        self.retried = []
        self.accept_lease = True

    def claim(self):
        result, self.event = self.event, None
        return result

    def mark_published(self, event):
        self.published.append(event)
        return self.accept_lease

    def schedule_retry(self, event):
        self.retried.append(event)
        return True


class FakeSqs:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def send_message(self, **kwargs):
        if self.fail:
            raise RuntimeError('secret error text')
        self.messages.append(kwargs)
        return {'MessageId': 'sqs-7'}


def test_sqs_publish_includes_dedup_key():
    repo, sqs = FakeRepo(item()), FakeSqs()
    result = SqsOutboxPublisher(repo, sqs, 'https://sqs.example.com/q').publish_one()
    assert result == 'published'
    assert repo.published and not repo.retried
    body = json.loads(sqs.messages[0]['MessageBody'])
    assert body['webhook_id'] == 'hook-7'
    assert body['shop_domain'] == 'example.myshopify.com'
    assert body['payload'] == {'id': 123}


def test_sqs_failure_retries_without_ack():
    repo = FakeRepo(item())
    result = SqsOutboxPublisher(repo, FakeSqs(fail=True), 'https://sqs.example.com/q').publish_one()
    assert result == 'failed'
    assert repo.retried and not repo.published


def test_no_claim_means_no_send():
    sqs = FakeSqs()
    result = SqsOutboxPublisher(FakeRepo(None), sqs, 'https://sqs.example.com/q').publish_one()
    assert result == 'idle'
    assert not sqs.messages


def test_lost_lease_returns_warning_state():
    repo, sqs = FakeRepo(item()), FakeSqs()
    repo.accept_lease = False
    assert SqsOutboxPublisher(repo, sqs, 'https://sqs.example.com/q').publish_one() == 'lease_lost'
    assert len(sqs.messages) == 1


def test_invalid_queue_url():
    try:
        SqsOutboxPublisher(FakeRepo(None), FakeSqs(), 'invalid')
    except ValueError:
        pass
    else:
        raise AssertionError('Invalid SQS URL accepted')


def test_minimum_lease_validation():
    try:
        PostgresOutbox(lambda: None).claim(lease_seconds=1)
    except ValueError:
        pass
    else:
        raise AssertionError('Too-short lease accepted')
