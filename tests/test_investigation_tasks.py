import json
import os
import stat
import time
import pytest
from fastapi.testclient import TestClient
from relay.api import create_app
from relay.assistant import Answer
from relay.investigation_tasks import TaskDecision


def result(**kwargs):
    return Answer.model_validate({'action':'answer','message':'Review the report','selection':None,'hypotheses':[],'bug_report_draft':None}|kwargs)


@pytest.fixture
def harness(tmp_path,monkeypatch):
    monkeypatch.setenv('RELAY_AI_ENABLED','1')
    monkeypatch.setenv('RELAY_AI_BASE_URL','http://localhost:20128/v1')
    monkeypatch.setenv('RELAY_AI_MODEL','synthetic-model')
    app=create_app(data=tmp_path,monitoring=False)
    with TestClient(app) as client:
        scope=client.post('/api/conversations').json()['id']
        body=b'<robot><suite><test name="failure"><msg>PRIVATE_REPORT_CONTENT</msg><status status="FAIL"/></test></suite></robot>'
        item=client.post(f'/api/conversations/{scope}/attachments',params={'filename':'output.xml'},content=body).json()
        yield app,client,scope,item,monkeypatch


class Provider:
    def __init__(self,item): self.item=item;self.calls=[]
    async def answer(self,context):
        self.calls.append(json.loads(json.dumps(context)))
        if context.get('task_planning'):
            assert 'PRIVATE_REPORT_CONTENT' not in json.dumps(context)
            return result(access_requests=[{'kind':'report','resource_ids':[self.item['id']],'purpose':'Inspect failure',
                'search_all':True,'include_suites':True}])
        return result()


def start(client,scope):
    return client.post(f'/api/conversations/{scope}/tasks',json={'message':'Investigate','share_context':True}).json()


def plan(client,scope,task):
    return client.post(f"/api/conversations/{scope}/tasks/{task['id']}/plan").json()


def test_metadata_first_approval_is_scoped_resumable_and_idempotent(harness):
    app,client,scope,item,_=harness;provider=Provider(item);app.state.assistant.provider=provider
    assert client.post(f'/api/conversations/{scope}/tasks',json={'message':'x','share_context':False}).status_code==422
    task=start(client,scope);assert task['status']=='draft'
    pending=plan(client,scope,task)
    assert pending['status']=='awaiting_approval' and len(provider.calls)==1
    assert pending['capabilities'][0]['resources'][0]['sha256']==item['sha256']
    assert client.get(f"/api/conversations/{scope}/tasks/{task['id']}").json()['approval_hash']==pending['approval_hash']
    other=client.post('/api/conversations').json()['id']
    assert client.get(f"/api/conversations/{other}/tasks/{task['id']}").status_code==404
    url=f"/api/conversations/{scope}/tasks/{task['id']}/approve"
    assert client.post(url,json={'approval_hash':'0'*64}).status_code==409
    completed=client.post(url,json={'approval_hash':pending['approval_hash']}).json()
    assert completed['status']=='completed' and len(provider.calls)==2
    assert client.post(url,json={'approval_hash':pending['approval_hash']}).json()['status']=='completed'
    assert len(provider.calls)==2
    assert stat.S_IMODE(os.stat(app.state.execution.path).st_mode)==0o600


def test_denial_and_deleted_resource_never_resume(harness):
    app,client,scope,item,_=harness;provider=Provider(item);app.state.assistant.provider=provider
    task=start(client,scope);pending=plan(client,scope,task)
    client.delete(f"/api/conversations/{scope}/attachments/{item['id']}")
    url=f"/api/conversations/{scope}/tasks/{task['id']}/approve"
    assert client.post(url,json={'approval_hash':pending['approval_hash']}).status_code==409
    assert len(provider.calls)==1
    denied=client.post(f"/api/conversations/{scope}/tasks/{task['id']}/deny",json={'approval_hash':pending['approval_hash']}).json()
    assert denied['status']=='denied'
    assert client.post(url,json={'approval_hash':pending['approval_hash']}).json()['status']=='denied'
    assert len(provider.calls)==1


def test_unapproved_model_resource_and_provider_change_are_rejected(harness):
    app,client,scope,item,monkeypatch=harness
    class Invalid:
        async def answer(self,context):
            return result(access_requests=[{'kind':'report','resource_ids':['f'*32],'purpose':'invented','search_all':False,'include_suites':False}])
    app.state.assistant.provider=Invalid()
    task=start(client,scope)
    assert plan(client,scope,task)['error']['code']=='task_plan_invalid'
    provider=Provider(item);app.state.assistant.provider=provider
    task=start(client,scope);pending=plan(client,scope,task)
    monkeypatch.setenv('RELAY_AI_MODEL','different-model')
    assert client.post(f"/api/conversations/{scope}/tasks/{task['id']}/approve",json={'approval_hash':pending['approval_hash']}).status_code==409
    assert len(provider.calls)==1


def test_revocation_during_model_call_discards_answer(harness):
    app,client,scope,item,_=harness
    class Revoke(Provider):
        task=None;approval=None
        async def answer(self,context):
            if context.get('task_planning'): return await super().answer(context)
            app.state.investigation_tasks.deny(scope,self.task,TaskDecision(approval_hash=self.approval))
            return result()
    provider=Revoke(item);app.state.assistant.provider=provider
    task=start(client,scope);pending=plan(client,scope,task)
    provider.task=task['id'];provider.approval=pending['approval_hash']
    outcome=client.post(f"/api/conversations/{scope}/tasks/{task['id']}/approve",json={'approval_hash':pending['approval_hash']}).json()
    assert outcome['status']=='revoked' and outcome['answer'] is None


def test_expired_and_cancelled_tasks_do_not_run(harness,monkeypatch):
    app,client,scope,item,_=harness;provider=Provider(item);app.state.assistant.provider=provider
    task=start(client,scope);pending=plan(client,scope,task)
    with app.state.execution.connect() as db:
        record=app.state.investigation_tasks._get(scope,task['id'],db)
        record['expires_at']=time.time()-1
        app.state.investigation_tasks._save(db,record)
    assert client.post(f"/api/conversations/{scope}/tasks/{task['id']}/approve",json={'approval_hash':pending['approval_hash']}).json()['status']=='expired'
    task=start(client,scope)
    assert client.post(f"/api/conversations/{scope}/tasks/{task['id']}/cancel").json()['status']=='cancelled'
    assert plan(client,scope,task)['status']=='cancelled'
    assert len(provider.calls)==1


def test_interrupted_work_is_never_replayed_automatically(harness):
    app,client,scope,item,_=harness;provider=Provider(item);app.state.assistant.provider=provider
    task=start(client,scope);pending=plan(client,scope,task)
    with app.state.execution.connect() as db:
        record=app.state.investigation_tasks._get(scope,task['id'],db)
        record['status']='running';record['lease_until']=time.time()-1
        app.state.investigation_tasks._save(db,record)
    url=f"/api/conversations/{scope}/tasks/{task['id']}"
    assert client.get(url).json()['status']=='interrupted'
    assert client.post(url+'/approve',json={'approval_hash':pending['approval_hash']}).json()['status']=='interrupted'
    assert len(provider.calls)==1


def test_no_access_task_retains_safe_answer_shape(harness):
    app,client,scope,item,_=harness
    class NoAccess:
        async def answer(self,context): return result()
    app.state.assistant.provider=NoAccess()
    task=start(client,scope)
    done=plan(client,scope,task)
    assert done['status']=='completed'
    assert done['answer']['answer']['hypotheses']==[]
    assert done['answer']['facts'] is None


def test_concurrent_approvals_do_not_duplicate_execution(harness):
    import asyncio
    import threading
    app,client,scope,item,_=harness
    entered=threading.Event();release=threading.Event()
    class Blocking(Provider):
        async def answer(self,context):
            if context.get('task_planning'): return await super().answer(context)
            self.calls.append(context)
            entered.set()
            await asyncio.to_thread(release.wait,10)
            return result()
    provider=Blocking(item);app.state.assistant.provider=provider
    task=start(client,scope);pending=plan(client,scope,task)
    url=f"/api/conversations/{scope}/tasks/{task['id']}/approve"
    results=[]
    worker=threading.Thread(target=lambda:results.append(client.post(url,json={'approval_hash':pending['approval_hash']})))
    worker.start()
    try:
        assert entered.wait(5)
        second=client.post(url,json={'approval_hash':pending['approval_hash']})
        assert second.json()['status']=='running'
        assert len(provider.calls)==2
    finally:
        release.set();worker.join(timeout=10)
    assert not worker.is_alive() and results[0].json()['status']=='completed'
    assert len(provider.calls)==2


def test_task_saved_run_is_bound_to_evidence_and_run_identity(tmp_path,monkeypatch):
    from relay.evidence import EvidenceStore
    from test_monitoring import submitted
    monkeypatch.setenv('RELAY_AI_ENABLED','1')
    monkeypatch.setenv('RELAY_AI_BASE_URL','http://localhost:20128/v1')
    monkeypatch.setenv('RELAY_AI_MODEL','synthetic-model')
    app=create_app(data=tmp_path,monitoring=False)
    run_id=submitted(app.state.execution)['id']
    store=EvidenceStore(app.state.execution,app.state.monitor)
    item=store.upload(run_id,'output.xml',b'<robot><suite name="Root"><test name="fail"><msg>PRIVATE_SAVED_TEXT</msg><status status="FAIL"/></test></suite></robot>')
    class SavedProvider:
        calls=[]
        async def answer(self,context):
            self.calls.append(json.loads(json.dumps(context)))
            if context.get('task_planning'):
                assert 'PRIVATE_SAVED_TEXT' not in json.dumps(context)
                assert context['task_planning']['saved_reports'][0]['id']==item['id']
                return result(access_requests=[{'kind':'saved_report','resource_ids':[item['id']],'purpose':'Investigate saved report','search_all':True,'include_suites':True}])
            assert context['auto_report']['report_scope']=='saved_run'
            assert context['auto_report']['association']=='operator_asserted'
            return result()
    provider=SavedProvider();app.state.assistant.provider=provider
    with TestClient(app) as client:
        scope=client.post('/api/conversations').json()['id']
        task=client.post(f'/api/conversations/{scope}/tasks',json={'message':'Investigate','share_context':True,'run_id':run_id}).json()
        pending=plan(client,scope,task)
        assert pending['status']=='awaiting_approval'
        assert pending['capabilities'][0]['kind']=='saved_report'
        done=client.post(f"/api/conversations/{scope}/tasks/{task['id']}/approve",json={'approval_hash':pending['approval_hash']}).json()
        assert done['status']=='completed' and len(provider.calls)==2
        task=client.post(f'/api/conversations/{scope}/tasks',json={'message':'Investigate','share_context':True,'run_id':run_id}).json()
        pending=plan(client,scope,task)
        with app.state.execution.connect() as db:
            saved=app.state.execution.get(run_id)
            saved['commit']='f'*40
            db.execute('UPDATE plans SET record=? WHERE id=?',(json.dumps(saved),run_id))
        stale=client.post(f"/api/conversations/{scope}/tasks/{task['id']}/approve",json={'approval_hash':pending['approval_hash']})
        assert stale.status_code==409
        assert len(provider.calls)==3


def test_task_can_approve_two_related_saved_run_reports_and_detect_stale_run(tmp_path,monkeypatch):
    from relay.evidence import EvidenceStore
    from test_monitoring import submitted
    monkeypatch.setenv('RELAY_AI_ENABLED','1');monkeypatch.setenv('RELAY_AI_BASE_URL','http://localhost:20128/v1');monkeypatch.setenv('RELAY_AI_MODEL','synthetic-model')
    app=create_app(data=tmp_path,monitoring=False)
    first_run=submitted(app.state.execution)['id'];second_run=submitted(app.state.execution)['id']
    store=EvidenceStore(app.state.execution,app.state.monitor)
    first=store.upload(first_run,'output.xml',b'<robot><suite name="Root"><test name="login"><status status="FAIL"/></test></suite></robot>')
    second=store.upload(second_run,'output.xml',b'<robot><suite name="Root"><test name="login"><status status="PASS"/></test></suite></robot>')
    class Provider:
        calls=[]
        async def answer(self,context):
            self.calls.append(json.loads(json.dumps(context)))
            if context.get('task_planning'):
                related=context['task_planning']['related_saved_reports']
                assert {first['id'],second['id']} <= {item['id'] for item in related}
                return result(access_requests=[{'kind':'saved_comparison','resource_ids':[first['id'],second['id']],'purpose':'Compare runs','search_all':False,'include_suites':False}])
            assert context['comparison']['counts']=={'fail_to_pass':1}
            assert context['comparison']['comparability']['first_association']=='operator_asserted'
            return result(comparison_citations=[{'pair_id':'0','claim':'Observed difference'}])
    provider=Provider();app.state.assistant.provider=provider
    with TestClient(app) as client:
        scope=client.post('/api/conversations').json()['id']
        payload={'message':'Compare runs','share_context':True,'run_id':first_run}
        task=client.post(f'/api/conversations/{scope}/tasks',json=payload).json();pending=plan(client,scope,task)
        assert pending['status']=='awaiting_approval'
        assert {r['run_id'] for r in pending['capabilities'][0]['resources']}=={first_run,second_run}
        done=client.post(f"/api/conversations/{scope}/tasks/{task['id']}/approve",json={'approval_hash':pending['approval_hash']}).json()
        assert done['status']=='completed' and done['answer']['answer']['comparison_citations']
        task=client.post(f'/api/conversations/{scope}/tasks',json=payload).json();pending=plan(client,scope,task)
        saved=app.state.execution.get(second_run);saved['commit']='f'*40
        with app.state.execution.connect() as db: db.execute('UPDATE plans SET record=? WHERE id=?',(json.dumps(saved),second_run))
        response=client.post(f"/api/conversations/{scope}/tasks/{task['id']}/approve",json={'approval_hash':pending['approval_hash']})
        assert response.status_code==409
        assert len(provider.calls)==3


def test_tampered_attachment_bytes_invalidate_task_before_model(harness):
    app,client,scope,item,_=harness;provider=Provider(item);app.state.assistant.provider=provider
    task=start(client,scope);pending=plan(client,scope,task)
    with app.state.execution.connect() as db:
        db.execute('UPDATE conversation_files SET body=? WHERE id=?',(b'<robot><suite><test name="tampered"/></suite></robot>',item['id']))
    result=client.post(f"/api/conversations/{scope}/tasks/{task['id']}/approve",json={'approval_hash':pending['approval_hash']})
    assert result.status_code==409 and len(provider.calls)==1
