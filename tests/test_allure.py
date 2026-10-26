import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from relay.report_evidence import parse_report
from relay.api import create_app
from relay.contracts import Report
from test_monitoring import submitted

BASE = dict(uuid='test-uuid', name='Login', fullName='web.Login', status='failed', stage='finished', statusDetails={'message':'HTTP 503', 'trace':'Traceback: setup failed'}, attachments=[{'source':'../../secret'}])

@pytest.mark.parametrize('status,expected',[('passed','pass'),('failed','fail'),('broken','error'),('skipped','skip'),('unknown','unknown')])
def test_allure_statuses(status, expected):
    result = parse_report(json.dumps(BASE | {'status':status}).encode(), 'allure_json', 'artifact')[0]
    assert result['status'] == expected
    assert result['name'] == 'web.Login'
    assert result['message'] == 'HTTP 503\nTraceback: setup failed'
    assert result['attempts'] is None
    assert result['artifact_ids'] == ['artifact']
    assert 'attachments' not in result

@pytest.mark.parametrize('body',[b'[]',b'{}',b'{', b'{"uuid":"a","uuid":"b"}',b'null',json.dumps(BASE | {'status':'bogus'}).encode(),json.dumps(BASE | {'statusDetails':[]}).encode(),json.dumps(BASE | {'name':3}).encode()])
def test_allure_rejects_bad_files(body):
    with pytest.raises(ValueError):
        parse_report(body,'allure_json','artifact')


def test_unfinished_result_is_unknown():
    result = parse_report(json.dumps(BASE | {'status':'passed','stage':'running'}).encode(),'allure_json','artifact')
    assert result[0]['status'] == 'unknown'

@pytest.mark.parametrize('status,outcome',[('failed','failed'),('passed','incomplete')])
def test_allure_upload_partial_readiness_and_raw_download(tmp_path,monkeypatch,status,outcome):
    app = create_app(Path(__file__).resolve().parents[1]/'relay.example.json',tmp_path,monitoring=False)
    app.state.execution.registry.projects['api'].reports = [Report(format='allure_json',path='allure-result.json')]
    run = submitted(app.state.execution)
    from test_assistant import Fake, answer
    monkeypatch.setenv('RELAY_AI_ENABLED','1')
    fake = Fake(answer(action='analyze'))
    app.state.assistant.provider = fake
    body = json.dumps(BASE | {'status':status}).encode()
    with TestClient(app) as client:
        url = f"/api/remote-runs/{run['id']}/evidence"
        uploaded = client.post(url+'?report_path=allure-result.json',content=body)
        assert uploaded.status_code == 200
        assert uploaded.json()['parse_status'] == 'parsed'
        result = client.get(url).json()
        assert result['test_status'] == outcome
        assert result['analysis_ready'] is True
        assert result['evidence_complete'] is False
        assert any('individual test' in v for v in result['evidence_limitations'])
        assert client.get(url+'/'+uploaded.json()['id']).content == body

        response = client.post('/api/assistant/chat',json={'message':'analyze','share_context':True,'include_failure_details':True,'run_id':run['id']})
        assert response.status_code == 200
        facts = fake.calls[-1]['selected_run']
        assert facts['evidence_complete'] is False
        if status == 'failed':
            assert facts['failures'][0]['message'] == 'HTTP 503\nTraceback: setup failed'
