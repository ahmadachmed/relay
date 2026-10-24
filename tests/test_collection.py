import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from relay.api import create_app
from relay.contracts import Report
from relay.providers import ProviderError
from relay.providers.bitbucket_evidence import BitbucketEvidenceProvider
from test_monitoring import submitted, provider as status_provider, remote_response

ROOT = Path(__file__).resolve().parents[1]
BODY = b'<robot><suite name="API"><test id="s1-t1" name="ok"><status status="PASS"/></test></suite></robot>'


def app_run(tmp_path, mapped=True):
    app = create_app(ROOT / 'relay.example.json', tmp_path, monitoring=False)
    if mapped:
        app.state.registry.projects['api'].reports[0].download_name = 'output-{pipeline_uuid}.xml'
    plan = submitted(app.state.execution)
    app.state.monitor.provider_factory = lambda: status_provider(lambda r: remote_response(r, 'COMPLETED', 'SUCCESSFUL'))
    asyncio.run(app.state.monitor.refresh(plan['id']))
    return app, plan


def transport(handler, hosts=None):
    return BitbucketEvidenceProvider('email', 'SECRET', httpx.MockTransport(handler), download_hosts=hosts or [])


@pytest.mark.parametrize('template', ['latest.xml', '../{pipeline_uuid}.xml', '{pipeline_uuid}/{commit}.xml', '{unknown}-{pipeline_uuid}.xml', '{pipeline_uuid}:file'])
def test_invalid_template_is_rejected(template):
    with pytest.raises(ValidationError):
        Report(format='robot_xml', path='output.xml', download_name=template)


def test_collection_http_imports_and_does_not_refetch(tmp_path):
    app, plan = app_run(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.path.endswith('/downloads/output-12345678123412341234123456789abc.xml')
        return httpx.Response(200, content=BODY)
    app.state.collector.provider_factory = lambda: transport(handler)
    url = f"/api/remote-runs/{plan['id']}/evidence"
    with TestClient(app) as client:
        result = client.post(url + '/collect')
        assert result.status_code == 200
        assert result.json()['reports'][0]['status'] == 'imported'
        summary = client.get(url).json()
        assert summary['test_status'] == 'passed'
        assert summary['artifacts'][0]['source'] == 'bitbucket_downloads'
        assert summary['artifacts'][0]['association'] == 'pipeline_uuid_namespace'
        assert client.post(url + '/collect').json()['reports'][0]['status'] == 'available'
        assert client.get(url + '/collection').json()['status'] == 'finished'
    assert len(calls) == 1


def test_redirect_has_no_bitbucket_credentials(tmp_path):
    _, plan = app_run(tmp_path)
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == 'api.bitbucket.org':
            assert request.headers.get('authorization', '').startswith('Basic ')
            return httpx.Response(302, headers={'Location': 'https://storage.example/file?signature=PRIVATE'})
        assert 'authorization' not in request.headers
        return httpx.Response(200, content=BODY)
    spec = plan['plan']['project']['reports'][0]
    assert asyncio.run(transport(handler, ['storage.example']).fetch_report(plan, spec)) == BODY
    assert len(requests) == 2


@pytest.mark.parametrize('destination', ['http://storage.example/file', 'https://evil.example/file',
    'https://user:SECRET@storage.example/file', 'https://storage.example:444/file', 'https://storage.example/file#fragment'])
def test_unapproved_redirect_is_not_followed(tmp_path, destination):
    _, plan = app_run(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location': destination})
    with pytest.raises(ProviderError) as caught:
        asyncio.run(transport(handler, ['storage.example']).fetch_report(plan, plan['plan']['project']['reports'][0]))
    assert len(calls) == 1
    assert 'SECRET' not in str(caught.value)


def test_missing_report_is_retryable_and_not_a_pass(tmp_path):
    app, plan = app_run(tmp_path)
    app.state.collector.provider_factory = lambda: transport(lambda r: httpx.Response(404, text='SECRET'))
    result = asyncio.run(app.state.collector.collect(plan['id']))
    assert result['reports'][0]['status'] == 'missing'
    assert result['reports'][0]['error']['code'] == 'missing_evidence'
    app.state.collector.provider_factory = lambda: transport(lambda r: httpx.Response(200, content=BODY))
    assert asyncio.run(app.state.collector.collect(plan['id']))['reports'][0]['status'] == 'imported'


def test_unconfigured_source_does_not_contact_provider(tmp_path):
    app, plan = app_run(tmp_path, mapped=False)
    def unexpected():
        pytest.fail('Manual source must not contact Bitbucket')
    app.state.collector.provider_factory = unexpected
    assert asyncio.run(app.state.collector.collect(plan['id']))['reports'][0]['status'] == 'not_configured'


def test_concurrent_collection_downloads_once(tmp_path):
    app, plan = app_run(tmp_path)
    calls = []
    class Slow:
        async def fetch_report(self, record, spec):
            calls.append(1)
            await asyncio.sleep(0)
            return BODY
    app.state.collector.provider_factory = Slow
    async def run():
        return await asyncio.gather(app.state.collector.collect(plan['id']), app.state.collector.collect(plan['id']))
    asyncio.run(run())
    assert len(calls) == 1


def test_retention_runs_on_lifespan_without_evidence_read(tmp_path):
    from relay.evidence import EvidenceStore
    app, plan = app_run(tmp_path)
    store = EvidenceStore(app.state.execution, app.state.monitor)
    store.upload(plan['id'], 'output.xml', BODY)
    with app.state.execution.connect() as db:
        db.execute('UPDATE evidence SET expires_at=0')
    with TestClient(app) as client:
        for _ in range(50):
            client.get('/api/remote-runs')
            with app.state.execution.connect() as db:
                body = db.execute('SELECT body FROM evidence').fetchone()[0]
            if body is None:
                break
        else:
            pytest.fail('Scheduled retention did not clear expired evidence')
