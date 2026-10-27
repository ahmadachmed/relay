import pytest
from relay.execution import PlanError
from relay.report_compare import compare_robot


def report(tests):
    return ('<robot><suite name="Root">'+''.join(f'<test name="{name}"><status status="{status}"/></test>' for name,status in tests)+'</suite></robot>').encode()


def test_comparison_uses_unique_suite_test_names_and_never_claims_flaky():
    a=report([('one','FAIL'),('dup','FAIL'),('dup','FAIL'),('only-old','FAIL')])
    b=report([('one','PASS'),('dup','PASS'),('only-new','PASS')])
    result=compare_robot(a,b,'a'*64,'b'*64)
    assert result['counts']=={'fail_to_pass':1} and result['total']==1
    assert result['items'][0]['first_test_id']=='0' and result['items'][0]['second_test_id']=='0'
    assert result['ambiguous_first_tests']==2
    assert any('not proof of flakiness' in item for item in result['limitations'])


def test_comparison_refuses_same_report_and_unsafe_xml():
    a=report([('one','FAIL')])
    with pytest.raises(PlanError): compare_robot(a,a,'a'*64,'a'*64)
    with pytest.raises(PlanError): compare_robot(b'<!DOCTYPE robot><robot/>',a,'b'*64,'a'*64)


def test_comparison_scope_hash_and_deletion(tmp_path):
    from fastapi.testclient import TestClient
    from relay.api import create_app
    with TestClient(create_app(data=tmp_path,monitoring=False)) as client:
        a,b=[client.post('/api/conversations').json()['id'] for _ in range(2)]
        path=f'/api/conversations/{a}/attachments'
        first=client.post(path,params={'filename':'first.xml'},content=report([('one','FAIL')])).json()
        second=client.post(path,params={'filename':'second.xml'},content=report([('one','PASS')])).json()
        query={'first_id':first['id'],'first_sha256':first['sha256'],'second_id':second['id'],'second_sha256':second['sha256']}
        url=f'/api/conversations/{a}/robot-reports/compare'
        assert client.post(url,json=query).json()['counts']=={'fail_to_pass':1}
        assert client.post(url.replace(a,b),json=query).status_code==404
        assert client.post(url,json={**query,'first_sha256':'0'*64}).status_code==409
        client.delete(f"{path}/{first['id']}")
        assert client.post(url,json=query).status_code==404


def test_ai_comparison_requires_consent_and_validates_citations(tmp_path,monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from relay.api import create_app
    from relay.assistant import Answer
    monkeypatch.setenv('RELAY_AI_ENABLED','1')
    app=create_app(data=tmp_path,monitoring=False)
    class Fake:
        calls=[]
        async def answer(self,context):
            self.calls.append(context)
            return Answer.model_validate({'action':'answer','message':'Observed change','selection':None,'hypotheses':[],
                'bug_report_draft':None,'comparison_citations':[{'pair_id':'0','claim':'Observed fail to pass'}]})
    fake=Fake();app.state.assistant.provider=fake
    with TestClient(app) as client:
        scope=client.post('/api/conversations').json()['id']
        base=f'/api/conversations/{scope}/attachments'
        first=client.post(base,params={'filename':'first.xml'},content=report([('one','FAIL')])).json()
        second=client.post(base,params={'filename':'second.xml'},content=report([('one','PASS')])).json()
        ref={'conversation_id':scope,'first_id':first['id'],'first_sha256':first['sha256'],'second_id':second['id'],'second_sha256':second['sha256'],'share':True}
        body={'message':'compare','share_context':True,'comparison':ref}
        result=client.post('/api/assistant/chat',json=body)
        assert result.status_code==200,result.text
        assert result.json()['comparison_shared']['counts']=={'fail_to_pass':1}
        assert 'not proof of flakiness' in json.dumps(fake.calls[-1]['comparison']['limitations'])
        assert client.post('/api/assistant/chat',json={**body,'comparison':{**ref,'share':False}}).status_code==422
        assert client.post('/api/assistant/chat',json={'message':'compare','share_context':True}).status_code==502


def test_comparison_deletion_or_redaction_change_discards_ai_response(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from relay.api import create_app
    from relay.assistant import Answer
    monkeypatch.setenv('RELAY_AI_ENABLED','1')
    app=create_app(data=tmp_path,monitoring=False)
    with TestClient(app) as client:
        scope=client.post('/api/conversations').json()['id']
        base=f'/api/conversations/{scope}/attachments'
        first=client.post(base,params={'filename':'first.xml'},content=report([('private-comparison-secret','FAIL')])).json()
        second=client.post(base,params={'filename':'second.xml'},content=report([('private-comparison-secret','PASS')])).json()
        ref={'conversation_id':scope,'first_id':first['id'],'first_sha256':first['sha256'],'second_id':second['id'],'second_sha256':second['sha256'],'share':True}
        class Provider:
            async def answer(self,context):
                monkeypatch.setenv('RELAY_COMPARE_SECRET','private-comparison-secret')
                return Answer.model_validate({'action':'answer','message':'Observed','selection':None,'hypotheses':[],'bug_report_draft':None})
        app.state.assistant.provider=Provider()
        assert client.post('/api/assistant/chat',json={'message':'compare','share_context':True,'comparison':ref}).status_code==409
        monkeypatch.delenv('RELAY_COMPARE_SECRET')
        class Delete:
            async def answer(self,context):
                app.state.assistant.conversation_files.delete(scope,first['id'])
                return Answer.model_validate({'action':'answer','message':'Observed','selection':None,'hypotheses':[],'bug_report_draft':None})
        app.state.assistant.provider=Delete()
        assert client.post('/api/assistant/chat',json={'message':'compare','share_context':True,'comparison':ref}).status_code==404
