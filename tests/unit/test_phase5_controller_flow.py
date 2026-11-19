"""Controller tests with inert fake AWS clients (no billable calls)."""
from datetime import datetime, timedelta, timezone
from ecommerce.application.use_cases.rds_controller import RdsController, ProjectionFailed

T0 = datetime(2026,11,19,20,0,tzinfo=timezone.utc)

class FakeControl:
    def __init__(self): self.record = {}
    def update_item(self, **kw):
        expr = kw['UpdateExpression']
        vals = kw.get('ExpressionAttributeValues', {})
        names = kw.get('ExpressionAttributeNames', {})
        if 'lease_until=:expiry' in expr:
            if self.record.get('lease_until', 0) >= vals[':now']:
                from botocore.exceptions import ClientError
                raise ClientError({'Error': {'Code':'ConditionalCheckFailedException', 'Message':'busy'}}, 'UpdateItem')
            self.record['lease_token'] = vals[':token']
            self.record['lease_until'] = vals[':expiry']
        elif expr == 'REMOVE lease_token, lease_until':
            assert self.record['lease_token'] == vals[':token']
            self.record.pop('lease_token');self.record.pop('lease_until')
        elif expr.startswith('REMOVE '):
            for k in names.values(): self.record.pop(k, None)
        else:
            for n, k in names.items():
                i = n[2:]
                self.record[k] = vals[':v'+i]
    def get_item(self, **kw): return {'Item': self.record.copy()}

class FakeIndex:
    def __init__(self): self.pending = False
    def query(self, **kw): return {'Items': [{'event_key':'x'}] if self.pending else []}

class FakeDB:
    def __init__(self): self.status='stopped';self.starts=0;self.stops=0
    def describe_db_instances(self, **kw):return {'DBInstances':[{'DBInstanceStatus':self.status}]}
    def start_db_instance(self, **kw):self.starts+=1;self.status='starting'
    def stop_db_instance(self, **kw):self.stops+=1;self.status='stopping'

class FakeECS:
    def __init__(self):self.calls=[];self.status='RUNNING';self.exit=0
    def run_task(self, **kw):
        self.calls.append(kw)
        return {'tasks':[{'taskArn':'arn:aws:ecs:eu-central-1:123:task/xyz'}], 'failures':[]}
    def list_tasks(self, **kw): return {'taskArns': []}
    def describe_tasks(self, **kw):
        return {'tasks':[{'lastStatus':self.status, 'containers':[{'exitCode':self.exit}]}]}

def setup():
    control,index,db,ecs=FakeControl(),FakeIndex(),FakeDB(),FakeECS()
    control.record["schema_ready"] = True
    x=RdsController(db=db,ecs=ecs,index=index,control=control,
                    db_identifier='test',cluster='test-cluster', task_definition='test-def',
                    subnets=['a'],security_groups=['s'], automatic_stop_enabled=True)
    return x,control,index,db,ecs

def test_pending_event_wakes_rds_only_once():
    c,state,events,db,ecs=setup()
    assert c.advance(T0)=='rds_stopped_idle'
    events.pending=True
    assert c.advance(T0+timedelta(minutes=1))=='rds_start_requested'
    assert c.advance(T0+timedelta(minutes=2))=='rds_transitioning'
    assert db.starts==1

def test_projector_launch_and_checkpointed_idle_shutdown():
    c,state,events,db,ecs=setup()
    db.status='available';events.pending=True
    assert c.advance(T0)=='worker_launched'
    assert len(ecs.calls)==1 and 'clientToken' in ecs.calls[0]
    assert c.advance(T0+timedelta(minutes=1))=='worker_running'
    events.pending=False
    ecs.status='STOPPED'
    assert c.advance(T0+timedelta(minutes=2))=='idle_window_started'
    assert c.advance(T0+timedelta(minutes=20))=='idle_grace'
    assert c.advance(T0+timedelta(minutes=33))=='rds_stop_requested'
    assert db.stops==1

def test_new_activity_blocks_idle_stop():
    c,state,events,db,ecs=setup()
    db.status='available'
    assert c.advance(T0)=='idle_window_started'
    state.record['last_event_at']=(T0+timedelta(minutes=29)).isoformat()
    assert c.advance(T0+timedelta(minutes=31))=='idle_grace'
    assert db.stops==0

def test_failed_worker_never_stops_rds():
    import pytest
    c,state,events,db,ecs=setup()
    db.status='available';events.pending=True
    assert c.advance(T0)=='worker_launched'
    ecs.status='STOPPED';ecs.exit=1
    with pytest.raises(ProjectionFailed): c.advance(T0+timedelta(minutes=1))
    assert state.record['error_hold']=='projection_failure'
    assert c.advance(T0+timedelta(hours=1))=='error_hold'
    assert db.stops==0


def test_default_does_not_automatically_stop_shared_rds():
    control,index,db,ecs=FakeControl(),FakeIndex(),FakeDB(),FakeECS()
    db.status='available'
    control.record["schema_ready"] = True
    c=RdsController(db=db,ecs=ecs,index=index,control=control,
                    db_identifier='test',cluster='test',task_definition='def',
                    subnets=['a'],security_groups=['s'])
    assert c.advance(T0) == 'idle_window_started'
    assert c.advance(T0+timedelta(hours=1)) == 'automatic_stop_disabled'
    assert db.stops == 0

def test_pending_event_prevents_automatic_stop_even_after_idle():
    c,state,events,db,ecs=setup()
    db.status='available'
    assert c.advance(T0) == 'idle_window_started'
    events.pending=True
    assert c.advance(T0+timedelta(hours=1)) == 'worker_launched'
    assert db.stops == 0

def test_any_other_ecs_task_blocks_stop():
    c,state,events,db,ecs=setup()
    db.status='available'
    assert c.advance(T0) == 'idle_window_started'
    ecs.list_tasks=lambda **kw: {'taskArns':['another-task']}
    assert c.advance(T0+timedelta(hours=1)) == 'other_ecs_workloads_running'
    assert db.stops == 0


def test_schema_gate_prevents_processing_before_migration():
    c,state,events,db,ecs=setup()
    state.record["schema_ready"] = False
    events.pending = True
    assert c.advance(T0) == "schema_not_ready"
    assert db.starts == 0
    state.record["schema_ready"] = True
    assert c.advance(T0+timedelta(minutes=1)) == "rds_start_requested"
