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
        assert summary['analysis_ready'] is False
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
