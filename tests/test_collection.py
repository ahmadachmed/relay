from pathlib import Path
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from relay.api import create_app
from relay.contracts import Report
from relay.evidence import EvidenceStore
from test_monitoring import submitted

ROOT = Path(__file__).resolve().parents[1]
BODY = b'<robot><suite name="API"><test name="ok"><status status="PASS"/></test></suite></robot>'


def test_artifact_handoff_and_manual_upload(tmp_path):
    app = create_app(ROOT / 'relay.example.json', tmp_path, monitoring=False)
    plan = submitted(app.state.execution)
    url = f"/api/remote-runs/{plan['id']}/evidence"
    with TestClient(app) as client:
        response = client.post(url + '/collect')
        assert response.status_code == 200
        result = response.json()
        assert result['source'] == 'bitbucket_pipeline_artifacts'
        assert result['automatic_download_supported'] is False
        assert result['status'] == 'manual_upload_required'
        assert result['pipeline_url'] == plan['pipeline']['url']
        assert client.get(url + '/collection').json() == result
        artifact = client.post(url + '?report_path=output.xml', content=BODY).json()
        assert artifact['source'] == 'manual_upload'
        assert artifact['association'] == 'operator_asserted'
        assert client.post(url + '/collect').json()['reports'][0]['artifact_id'] == artifact['id']
        assert client.get(url).json()['test_status'] == 'passed'
        assert client.get(url + '/' + artifact['id']).content == BODY
        assert client.post('/api/remote-runs/missing/evidence/collect').status_code == 404


def test_downloads_mapping_is_no_longer_supported():
    with pytest.raises(ValidationError):
        Report(format='robot_xml', path='output.xml', download_name='output-{pipeline_uuid}.xml')


def test_retention_sweep_without_evidence_reads(tmp_path):
    app = create_app(ROOT / 'relay.example.json', tmp_path, monitoring=False)
    plan = submitted(app.state.execution)
    store = EvidenceStore(app.state.execution, app.state.monitor)
    store.upload(plan['id'], 'output.xml', BODY)
    with app.state.execution.connect() as db:
        db.execute('UPDATE evidence SET expires_at=0')
    with TestClient(app) as client:
        for _ in range(50):
            client.get('/api/remote-runs')
            with app.state.execution.connect() as db:
                if db.execute('SELECT body FROM evidence').fetchone()[0] is None:
                    break
        else:
            pytest.fail('Retention sweep did not expire evidence')
