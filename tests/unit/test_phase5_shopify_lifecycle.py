from datetime import datetime, timedelta, timezone
import pytest
from ecommerce.application.use_cases.shopify_order_lifecycle import (
    LifecycleError, Operation, advance, cancellation_plan, stable_key, REFUND_MUTATION,
)

NOW = datetime(2026,11,19,12,tzinfo=timezone.utc)
ORDER = 'gid://shopify/Order/123'
LINE = 'gid://shopify/LineItem/456'

class Journal:
    def __init__(self): self.records={}; self.writes=0
    def get(self,k): return self.records.get(k)
    def put_new(self,k,r):
        if k in self.records: raise AssertionError('duplicate checkpoint')
        self.records[k]=dict(r); self.writes+=1
    def put(self,k,r): self.records[k]=dict(r); self.writes+=1

class Shopify:
    def __init__(self, *, cancelled=False, refunded=False, available=3):
        self.cancelled=cancelled;self.refunded=refunded;self.available=available;self.mutations=[]
    def query(self,doc,vars,mutation=False):
        if mutation:
            self.mutations.append(vars)
            if 'orderCancel' in doc: return {'orderCancel': {'job': {'id':'gid://shopify/Job/456', 'done':False},'orderCancelUserErrors':[]}}
            if 'refundCreate' in doc: return {'refundCreate': {'refund':{'id':'gid://shopify/Refund/345'},'userErrors':[]}}
        if 'Phase5CancellationJob' in doc: return {'job':{'id':'gid://shopify/Job/456','done':True}}
        return {'order':{'id':ORDER,'currencyCode':'GBP',
                         'cancelledAt':'2026-11-19T12:00:00Z' if self.cancelled else None,
                         'lineItems':{'pageInfo':{'hasNextPage':False},'nodes':[
                             {'id':LINE,'quantity':5,'refundableQuantity':self.available}]},
                         'refunds':[{'id':'gid://shopify/Refund/345','note':'phase5-refund:'+stable_key('run001',ORDER,'refund')}] if self.refunded else []}}

def op(kind, when=NOW):
    return Operation('run001',ORDER,kind,when.isoformat(),LINE if kind=='refund' else None,
                     1 if kind=='refund' else None)

def test_plan_deterministic_and_due_within_policy():
    a=cancellation_plan('run001', ORDER, NOW,seed=20261118)
    b=cancellation_plan('run001', ORDER, NOW,seed=20261118)
    assert a==b
    due=datetime.fromisoformat(a['due_at'])
    assert NOW<=due<=NOW+timedelta(days=14)
    assert a['reason']=='OTHER'

def test_not_due_and_readonly_no_mutations():
    g=Shopify();j=Journal()
    assert advance(g,j,op('cancel',NOW+timedelta(days=1)),now=NOW,allow_mutations=True)=='not_due'
    assert advance(g,j,op('cancel'),now=NOW,allow_mutations=False)=='preview'
    assert not g.mutations and j.writes==0

def test_cancel_reserve_then_job_then_state():
    g=Shopify();j=Journal();o=op('cancel')
    assert advance(g,j,o,now=NOW,allow_mutations=True)=='pending'
    assert j.get(o.key)['job_id']=='gid://shopify/Job/456'
    assert advance(g,j,o,now=NOW,allow_mutations=True)=='awaiting_shopify_state'
    g.cancelled=True
    assert advance(g,j,o,now=NOW,allow_mutations=True)=='cancelled'
    assert advance(g,j,o,now=NOW,allow_mutations=True)=='already_complete'
    assert len(g.mutations)==1

def test_ambiguous_cancel_never_reissues():
    g=Shopify();j=Journal();o=op('cancel')
    j.put_new(o.key,{**o.as_record(),'state':'reserved'})
    with pytest.raises(LifecycleError,match='Ambiguous'):
        advance(g,j,o,now=NOW,allow_mutations=True)
    assert not g.mutations

def test_refund_with_idempotency_key_and_replay():
    g=Shopify();j=Journal();o=op('refund')
    assert advance(g,j,o,now=NOW,allow_mutations=True)=='refunded'
    assert g.mutations[0]['key']==o.key
    assert '@idempotent' in REFUND_MUTATION
    assert advance(g,j,o,now=NOW,allow_mutations=True)=='already_complete'
    assert len(g.mutations)==1

def test_refund_rejects_excess_or_non_currency():
    g=Shopify(available=0);j=Journal()
    with pytest.raises(LifecycleError,match='exceeds'):
        advance(g,j,op('refund'),now=NOW,allow_mutations=True)
    assert not g.mutations and j.writes==0

def test_refund_resume_keeps_idempotency_key():
    g=Shopify();j=Journal();o=op('refund')
    j.put_new(o.key,{**o.as_record(),'state':'refund_requested','mutation_first_attempt_at':NOW.isoformat()})
    assert advance(g,j,o,now=NOW+timedelta(minutes=5),allow_mutations=True)=='refunded'
    assert g.mutations[0]['key']==o.key

def test_refund_old_ambiguous_state_needs_manual_review():
    g=Shopify();j=Journal();o=op('refund')
    j.put_new(o.key,{**o.as_record(),'state':'refund_requested','mutation_first_attempt_at':(NOW-timedelta(hours=13)).isoformat()})
    with pytest.raises(LifecycleError,match='expired'):
        advance(g,j,o,now=NOW,allow_mutations=True)
    assert not g.mutations

def test_cancellation_does_not_implicitly_refund():
    g=Shopify();j=Journal()
    advance(g,j,op('cancel'),now=NOW,allow_mutations=True)
    assert all('refundCreate' not in str(x) for x in g.mutations)
