from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from relay.api import create_app
from relay.evidence import EvidenceStore
from relay.execution import PlanError
from test_monitoring import submitted

ROOT = Path(__file__).resolve().parents[1]
REPORT = b'<robot><suite name="API"><test id="s1-t1" name="Login"><status status="PASS"/></test></suite></robot>'


def setup(tmp_path):
    app = create_app(ROOT / 'relay.example.json', tmp_path, monitoring=False)
    plan = submitted(app.state.execution)
    return app, plan['id'], EvidenceStore(app.state.execution, app.state.monitor)


def test_upload_identity_evidence_and_scope(tmp_path):
    app, run_id, store = setup(tmp_path)
    other = submitted(app.state.execution)['id']
    with TestClient(app) as client:
        url = f'/api/remote-runs/{run_id}/evidence'
        assert client.get(url).json()['artifacts'][0]['availability'] == 'missing'
        result = client.post(url + '?report_path=output.xml', content=REPORT)
        assert result.status_code == 200
        artifact = result.json()
        summary = client.get(url).json()
        assert summary['test_status'] == 'passed'
        assert summary['tests'][0]['artifact_ids'] == [artifact['id']]
        assert summary['analysis_ready'] is True
        download = client.get(url + '/' + artifact['id'])
        assert download.content == REPORT
        assert download.headers['content-type'] == 'application/octet-stream'
        assert download.headers['content-security-policy'].startswith('sandbox')
        assert client.get(f'/api/remote-runs/{other}/evidence/' + artifact['id']).status_code == 404
        assert client.post(url + '?report_path=../../secret', content=REPORT).status_code == 422
        assert client.post(url + '?report_path=output.xml', content=REPORT).json()['id'] == artifact['id']
        assert client.post(url + '?report_path=output.xml', content=REPORT + b' ').status_code == 409
    restored = EvidenceStore(app.state.execution, app.state.monitor)
    assert restored.summary(run_id)['test_status'] == 'passed'


@pytest.mark.parametrize('body', [b'<robot>', b'<robot/>', b'<!DOCTYPE robot><robot/>'])
def test_failed_parse_retains_original_evidence(tmp_path, body):
    _, run_id, store = setup(tmp_path)
    artifact = store.upload(run_id, 'output.xml', body)
    assert artifact['parse_status'] == 'failed'
    assert store.summary(run_id)['test_status'] == 'incomplete'
    assert store.download(run_id, artifact['id']) == body


def test_expired_report_does_not_continue_claiming_pass(tmp_path):
    app, run_id, store = setup(tmp_path)
    artifact = store.upload(run_id, 'output.xml', REPORT)
    with app.state.execution.connect() as db:
        db.execute('UPDATE evidence SET expires_at=0')
    assert store.summary(run_id)['test_status'] == 'incomplete'
    assert store.summary(run_id)['artifacts'][0]['availability'] == 'expired'
    with pytest.raises(PlanError) as error:
        store.download(run_id, artifact['id'])
    assert error.value.status == 410


@pytest.mark.parametrize('counter', ['tests="2" failures="1"', 'failures="1"', 'errors="1"', 'skipped="1"', 'tests="-1"', 'tests="1.0"'])
def test_inconsistent_junit_cannot_claim_pass(tmp_path, counter):
    app = create_app(ROOT / 'relay.example.json', tmp_path, monitoring=False)
    app.state.registry.projects['api'].reports[0].format = 'junit_xml'
    run_id = submitted(app.state.execution)['id']
    store = EvidenceStore(app.state.execution, app.state.monitor)
    body = f'<testsuite {counter}><testcase name="test_ok"/></testsuite>'.encode()
    artifact = store.upload(run_id, 'output.xml', body)
    assert artifact['parse_status'] == 'failed'
    assert store.summary(run_id)['test_status'] == 'incomplete'
    assert store.summary(run_id)['tests'] == []
    assert store.download(run_id, artifact['id']) == body


def test_pytest_setup_and_teardown_errors_are_failed_evidence(tmp_path):
    app = create_app(ROOT / 'relay.example.json', tmp_path, monitoring=False)
    app.state.registry.projects['api'].reports[0].format = 'junit_xml'
    run_id = submitted(app.state.execution)['id']
    store = EvidenceStore(app.state.execution, app.state.monitor)
    body = b'<testsuites><testsuite tests="1" errors="2"><testcase name="example"><error message="failed on setup">setup error</error><error message="failed on teardown">teardown error</error></testcase></testsuite></testsuites>'
    artifact = store.upload(run_id, 'output.xml', body)
    result = store.summary(run_id)
    assert artifact['parse_status'] == 'parsed'
    assert result['test_status'] == 'failed'
    assert result['tests'][0]['status'] == 'error'
    assert result['tests'][0]['artifact_ids'] == [artifact['id']]
    assert all(message in result['tests'][0]['message'] for message in ['setup error', 'teardown error'])
    assert store.download(run_id, artifact['id']) == body


def test_robot_error_log_is_diagnostic_not_failed_test(tmp_path):
    _, run_id, store = setup(tmp_path)
    body = b'<robot><suite name="API"><test id="s1-t1" name="ok"><status status="PASS"/></test><status status="PASS"/></suite><errors><msg level="ERROR">diagnostic only</msg></errors></robot>'
    artifact = store.upload(run_id, 'output.xml', body)
    result = store.summary(run_id)
    assert result['test_status'] == 'passed'
    assert len(result['tests']) == 1
    assert result['tests'][0]['status'] == 'pass'
    assert store.download(run_id, artifact['id']) == body


def test_results_from_multiple_reports_have_distinct_ids(tmp_path):
    from relay.contracts import Report
    app = create_app(ROOT / 'relay.example.json', tmp_path, monitoring=False)
    app.state.registry.projects['api'].reports = [Report(format='junit_xml', path=p) for p in ['android.xml', 'ios.xml']]
    run_id = submitted(app.state.execution)['id']
    store = EvidenceStore(app.state.execution, app.state.monitor)
    body = b'<testsuite name="smoke"><testcase classname="tests.Login" name="test_login"/></testsuite>'
    artifacts = [store.upload(run_id, p, body) for p in ['android.xml', 'ios.xml']]
    result = store.summary(run_id)
    assert len(result['tests']) == len({t['id'] for t in result['tests']}) == 2
    assert {t['artifact_ids'][0] for t in result['tests']} == {a['id'] for a in artifacts}
    original_ids = [t['id'] for t in result['tests']]
    assert store.upload(run_id, 'android.xml', body)['id'] == artifacts[0]['id']
    restored = EvidenceStore(app.state.execution, app.state.monitor)
    assert [t['id'] for t in restored.summary(run_id)['tests']] == original_ids


def test_saved_robot_report_search_reuses_parser_with_run_scope_and_expiry(tmp_path):
    app,run_id,store=setup(tmp_path)
    other=submitted(app.state.execution)['id']
    report=b'<robot><suite name="API"><test name="Login"><msg>timeout</msg><status status="FAIL"/></test></suite></robot>'
    artifact=store.upload(run_id,'output.xml',report)
    with TestClient(app) as client:
        url=f"/api/remote-runs/{run_id}/evidence/{artifact['id']}/robot-report"
        listing=client.get(url).json()
        assert listing['scope']=='saved_run' and listing['association']=='operator_asserted'
        assert listing['items'][0]['suite_path']=='API'
        match=client.post(url+'/search',json={'query':'timeout'}).json()['items'][0]
        assert match['section_id']=='0' and match['test_id']=='0'
        assert client.get(url,params={'test_id':'0'}).json()['items'][0]['text']=='timeout'
        assert client.get(url.replace(run_id,other)).status_code==404
        with app.state.execution.connect() as db: db.execute('UPDATE evidence SET expires_at=0')
        assert client.get(url).status_code==410


def test_saved_robot_report_ai_requires_matching_run_and_rechecks_retention(tmp_path,monkeypatch):
    from relay.assistant import Answer
    monkeypatch.setenv('RELAY_AI_ENABLED','1')
    app,run_id,store=setup(tmp_path)
    other=submitted(app.state.execution)['id']
    body=b'<robot><suite name="API"><test name="Login"><msg>timeout</msg><status status="FAIL"/></test></suite></robot>'
    artifact=store.upload(run_id,'output.xml',body)
    class Provider:
        calls=0
        async def answer(self,context):
            self.calls+=1
            if self.calls==1:
                assert context['auto_report']['association']=='operator_asserted'
                return Answer.model_validate({'action':'answer','message':'Investigate','selection':None,'hypotheses':[], 'bug_report_draft':None,
                    'report_operations':[{'operation':'read_test','test_id':'0','offset':0}]})
            assert context['report_attachment']['items'][0]['text']=='timeout'
            return Answer.model_validate({'action':'answer','message':'Observed','selection':None,'hypotheses':[], 'bug_report_draft':None,
                'report_citations':[{'section_id':'0','claim':'timeout'}]})
    with TestClient(app) as client:
        scope=client.post('/api/conversations').json()['id']
        ref={'conversation_id':scope,'run_id':run_id,'artifact_id':artifact['id'],'sha256':artifact['sha256'],'share':True}
        app.state.assistant.provider=Provider()
        result=client.post('/api/assistant/chat',json={'message':'Investigate','share_context':True,'run_id':run_id,'auto_report':ref})
        assert result.status_code==200,result.text
        assert result.json()['report_shared']['association']=='operator_asserted'
        assert client.post('/api/assistant/chat',json={'message':'Investigate','share_context':True,'run_id':other,'auto_report':ref}).status_code==422
        assert client.post('/api/assistant/chat',json={'message':'Investigate','share_context':True,'run_id':run_id,'auto_report':{**ref,'sha256':'0'*64}}).status_code==409
        with app.state.execution.connect() as db: db.execute('UPDATE evidence SET expires_at=0')
        assert client.post('/api/assistant/chat',json={'message':'Investigate','share_context':True,'run_id':run_id,'auto_report':ref}).status_code==410


def test_saved_run_comparison_discloses_metadata_and_operator_association(tmp_path):
    app,first_run,store=setup(tmp_path)
    second_run=submitted(app.state.execution)['id']
    first=store.upload(first_run,'output.xml',b'<robot><suite name="API"><test name="Login"><status status="FAIL"/></test></suite></robot>')
    second=store.upload(second_run,'output.xml',b'<robot><suite name="API"><test name="Login"><status status="PASS"/></test></suite></robot>')
    result=store.compare_saved_reports(first_run,first['id'],first['sha256'],second_run,second['id'],second['sha256'])
    assert result['counts']=={'fail_to_pass':1}
    assert result['comparability']['first_association']=='operator_asserted'
    assert result['comparability']['status'] in ('matching_metadata_unverified','unknown')
    assert any('not proof of flakiness' in item for item in result['limitations'])
    with app.state.execution.connect() as db:
        import json
        row=db.execute('SELECT record FROM plans WHERE id=?',(second_run,)).fetchone()
        data=json.loads(row[0]);data['commit']='f'*40
        db.execute('UPDATE plans SET record=? WHERE id=?',(json.dumps(data),second_run))
    changed=store.compare_saved_reports(first_run,first['id'],first['sha256'],second_run,second['id'],second['sha256'])
    assert changed['comparability']['status']=='different' and 'commit' in changed['comparability']['different_fields']


def test_saved_run_comparison_for_ai_requires_scope_and_approval(tmp_path,monkeypatch):
    from relay.assistant import Answer
    monkeypatch.setenv('RELAY_AI_ENABLED','1')
    app,first_run,store=setup(tmp_path)
    second_run=submitted(app.state.execution)['id']
    first=store.upload(first_run,'output.xml',b'<robot><suite name="API"><test name="Login"><status status="FAIL"/></test></suite></robot>')
    second=store.upload(second_run,'output.xml',b'<robot><suite name="API"><test name="Login"><status status="PASS"/></test></suite></robot>')
    class Provider:
        async def answer(self,context):
            assert context['comparison']['comparability']['first_association']=='operator_asserted'
            return Answer.model_validate({'action':'answer','message':'Observed','selection':None,'hypotheses':[],'bug_report_draft':None,
                'comparison_citations':[{'pair_id':'0','claim':'Observed fail to pass'}]})
    app.state.assistant.provider=Provider()
    with TestClient(app) as client:
        scope=client.post('/api/conversations').json()['id']
        ref={'conversation_id':scope,'first_run_id':first_run,'first_id':first['id'],'first_sha256':first['sha256'],
             'second_run_id':second_run,'second_id':second['id'],'second_sha256':second['sha256'],'share':True}
        body={'message':'Compare','share_context':True,'run_id':first_run,'comparison':ref}
        response=client.post('/api/assistant/chat',json=body)
        assert response.status_code==200,response.text
        assert response.json()['comparison_shared']['counts']=={'fail_to_pass':1}
        assert client.post('/api/assistant/chat',json={**body,'run_id':'a'*32}).status_code==422
        assert client.post('/api/assistant/chat',json={**body,'comparison':{**ref,'share':False}}).status_code==422


def test_identical_report_bytes_can_be_compared_across_distinct_runs(tmp_path):
    app,first_run,store=setup(tmp_path)
    second_run=submitted(app.state.execution)['id']
    first=store.upload(first_run,'output.xml',REPORT)
    second=store.upload(second_run,'output.xml',REPORT)
    result=store.compare_saved_reports(first_run,first['id'],first['sha256'],second_run,second['id'],second['sha256'])
    assert result['counts']=={'unchanged':1}
    with pytest.raises(PlanError): store.compare_saved_reports(first_run,first['id'],first['sha256'],first_run,first['id'],first['sha256'])


def test_saved_run_comparison_api_checks_both_run_scopes(tmp_path):
    app,run1,store=setup(tmp_path)
    run2=submitted(app.state.execution)['id'];other=submitted(app.state.execution)['id']
    first=store.upload(run1,'output.xml',b'<robot><suite name="API"><test name="Login"><status status="FAIL"/></test></suite></robot>')
    second=store.upload(run2,'output.xml',b'<robot><suite name="API"><test name="Login"><status status="PASS"/></test></suite></robot>')
    body={'first_sha256':first['sha256'],'second_run_id':run2,'second_id':second['id'],'second_sha256':second['sha256']}
    with TestClient(app) as client:
        url=f"/api/remote-runs/{run1}/evidence/{first['id']}/robot-report/compare"
        assert client.post(url,json=body).json()['counts']=={'fail_to_pass':1}
        assert client.post(url,json={**body,'second_run_id':other}).status_code==404
        assert client.post(url,json={**body,'second_sha256':'0'*64}).status_code==409
