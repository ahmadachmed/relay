import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from relay.api import create_app
from relay.contracts import PlanRequest
from relay.execution import ExecutionService, PlanError
from relay.providers import ProviderError
from relay.providers.bitbucket import BitbucketCatalogProvider
from relay.registry import load_registry

ROOT = Path(__file__).resolve().parents[1]
COMMIT = 'a' * 40
UUID = '{12345678-1234-1234-1234-123456789abc}'


def service(tmp_path):
    return ExecutionService(tmp_path / 'remote.sqlite3', load_registry(ROOT / 'projects.example.json'))


class Fake:
    calls = 0

    async def catalog(self, project):
        return {'commit': COMMIT, 'pipelines': [{'id': 'smoke', 'available': True}]}

    async def trigger(self, record):
        self.calls += 1
        await asyncio.sleep(0)
        return {'uuid': UUID, 'url': 'https://bitbucket.org/example/repo/pipelines/results/1'}


def preview(s, provider=None, **changes):
    return asyncio.run(s.preview(PlanRequest(project_id='api', pipeline_id='smoke', **changes), provider or Fake()))


def test_preview_and_duplicate_execute_survive_restart(tmp_path):
    s, p = service(tmp_path), Fake()
    record = preview(s, p, branch='develop')
    assert p.calls == 0
    assert record['plan']['parameters']['ENV'] == 'uat'
    assert record['commit'] == COMMIT
    async def double_click():
        return await asyncio.gather(s.execute(record['id'], p), s.execute(record['id'], p))
    asyncio.run(double_click())
    assert p.calls == 1
    restored = service(tmp_path)
    assert asyncio.run(restored.execute(record['id'], p))['status'] == 'submitted'
    assert p.calls == 1


def test_claim_is_atomic_across_connections(tmp_path):
    s = service(tmp_path)
    record = preview(s)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: service(tmp_path).claim(record['id'])[1], range(8)))
    assert sum(claims) == 1
    assert service(tmp_path).get(record['id'])['status'] == 'unknown'


@pytest.mark.parametrize('code,expected', [('trigger_unknown', 'unknown'), ('trigger_rejected', 'rejected')])
def test_errors_never_retry_and_projects_are_independent(tmp_path, code, expected):
    class Fail(Fake):
        async def trigger(self, record):
            self.calls += 1
            raise ProviderError(code, 'Sanitized failure')
    s, p = service(tmp_path), Fail()
    record = preview(s)
    assert asyncio.run(s.execute(record['id'], p))['status'] == expected
    assert asyncio.run(service(tmp_path).execute(record['id'], p))['status'] == expected
    assert p.calls == 1
    other = preview(s)
    assert asyncio.run(s.execute(other['id'], Fake()))['status'] == 'submitted'


@pytest.mark.parametrize('change,code', [('expiry', 'expired_plan'), ('registry', 'stale_plan')])
def test_invalidated_preview_does_not_trigger(tmp_path, change, code):
    s, p = service(tmp_path), Fake()
    record = preview(s)
    if change == 'expiry':
        record['expires_at'] = 0
        s.save(record)
    else:
        s.registry.projects['api'].pipelines['smoke'].selector = 'changed'
    with pytest.raises(PlanError) as caught:
        asyncio.run(s.execute(record['id'], p))
    assert caught.value.code == code
    assert p.calls == 0


def test_invalid_parameter_and_unavailable_selector(tmp_path):
    s = service(tmp_path)
    with pytest.raises(ValueError):
        preview(s, parameters={'ENV': 'prod'})
    class Missing(Fake):
        async def catalog(self, project):
            return {'commit': COMMIT, 'pipelines': []}
    with pytest.raises(PlanError, match='unavailable'):
        preview(s, Missing())


def test_trigger_posts_pinned_target_and_curated_variables(tmp_path):
    record = preview(service(tmp_path))
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.method == 'POST'
        assert request.url.host == 'api.bitbucket.org'
        assert body['target'] == {'type': 'pipeline_ref_target', 'ref_type': 'branch',
            'ref_name': 'main', 'commit': {'type': 'commit', 'hash': COMMIT},
            'selector': {'type': 'custom', 'pattern': 'smoke-tests'}}
        assert {v['key']: v['value'] for v in body['variables']} == record['plan']['parameters']
        return httpx.Response(201, json={'uuid': UUID, 'build_number': 7, 'secret': 'NEVER EXPOSE'})
    p = BitbucketCatalogProvider('email', 'TOKEN', httpx.MockTransport(handler))
    result = asyncio.run(p.trigger(record))
    assert result['uuid'] == UUID
    assert result['url'].endswith('/pipelines/results/7')
    assert 'NEVER EXPOSE' not in json.dumps(result)
    assert len(calls) == 1


@pytest.mark.parametrize('outcome,code', [(401, 'trigger_rejected'), (429, 'trigger_rejected'),
    (500, 'trigger_unknown'), (302, 'trigger_unknown'), (201, 'trigger_unknown'), ('timeout', 'trigger_unknown')])
def test_trigger_failures_do_not_retry_or_leak(tmp_path, outcome, code):
    calls = []
    def handler(request):
        calls.append(request)
        if outcome == 'timeout':
            raise httpx.ReadTimeout('SECRET')
        return httpx.Response(outcome, text='SECRET', headers={'Location': 'https://other.example'})
    p = BitbucketCatalogProvider('email', 'TOKEN', httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(p.trigger(preview(service(tmp_path))))
    assert caught.value.code == code
    assert 'SECRET' not in str(caught.value)
    assert len(calls) == 1


def test_api_requires_exact_confirmation_and_no_editable_payload(tmp_path, monkeypatch):
    p = Fake()
    monkeypatch.setattr(BitbucketCatalogProvider, 'from_environment', classmethod(lambda cls: p))
    app = create_app(ROOT / 'relay.example.json', tmp_path)
    with TestClient(app) as client:
        record = client.post('/api/plans/preview', json={'project_id': 'api', 'pipeline_id': 'smoke'}).json()
        url = f"/api/plans/{record['id']}/execute"
        for body in [{}, {'confirmed': 1}, {'confirmed': False}, {'confirmed': 'true'}, {'confirmed': True, 'parameters': {'ENV': 'prod'}}]:
            assert client.post(url, json=body).status_code == 422
        assert p.calls == 0
        assert client.post(url, json={'confirmed': True}).json()['status'] == 'submitted'
        assert client.post(url, json={'confirmed': True}).json()['status'] == 'submitted'
        assert p.calls == 1
        assert client.get(f"/api/plans/{record['id']}").json()['pipeline']['uuid'] == UUID
        assert client.get('/api/plans/missing').status_code == 404


@pytest.mark.parametrize('mismatch', [False, True])
def test_manual_reconciliation_is_get_only_and_target_checked(tmp_path, mismatch):
    s = service(tmp_path)
    record = preview(s)
    record, _ = s.claim(record['id'])
    record['attempted_at'] = 0
    s.save(record)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.path.endswith('/pipelines/' + UUID)
        return httpx.Response(200, json={'uuid': UUID, 'build_number': 8, 'target': {
            'ref_name': 'other' if mismatch else 'main', 'ref_type': 'branch',
            'commit': {'hash': COMMIT}, 'selector': {'type': 'custom', 'pattern': 'smoke-tests'}}})
    p = BitbucketCatalogProvider('email', 'TOKEN', httpx.MockTransport(handler))
    if mismatch:
        with pytest.raises(ProviderError, match='Could not verify'):
            asyncio.run(s.reconcile(record['id'], UUID, p))
        assert s.get(record['id'])['status'] == 'unknown'
    else:
        assert asyncio.run(s.reconcile(record['id'], UUID, p))['reconciled']
        assert asyncio.run(s.execute(record['id'], Fake()))['pipeline']['uuid'] == UUID
    assert len(calls) == 1


def test_reconciliation_waits_for_pending_attempt(tmp_path):
    s = service(tmp_path)
    record = preview(s)
    with pytest.raises(PlanError, match='Only an unknown'):
        asyncio.run(s.reconcile(record['id'], UUID, Fake()))
    s.claim(record['id'])
    with pytest.raises(PlanError, match='70 seconds'):
        asyncio.run(s.reconcile(record['id'], UUID, Fake()))


def test_reconciliation_requires_operator_attestation(tmp_path, monkeypatch):
    monkeypatch.setattr(BitbucketCatalogProvider, 'from_environment', classmethod(lambda cls: Fake()))
    with TestClient(create_app(ROOT / 'relay.example.json', tmp_path)) as client:
        for payload in [{'confirmed': True, 'pipeline_uuid': UUID},
                        {'confirmed': True, 'pipeline_uuid': UUID, 'verified_parameters': False},
                        {'confirmed': True, 'pipeline_uuid': 'bad', 'verified_parameters': True}]:
            assert client.post('/api/plans/missing/reconcile', json=payload).status_code == 422


@pytest.mark.parametrize('pipeline_uuid', [UUID, UUID.strip('{}').upper()])
def test_reconciliation_http_accepts_uuid_string_and_persists_result(tmp_path, monkeypatch, pipeline_uuid):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.path.endswith('/pipelines/' + UUID)
        return httpx.Response(200, json={'uuid': UUID, 'build_number': 8, 'target': {
            'ref_name': 'main', 'ref_type': 'branch', 'commit': {'hash': COMMIT},
            'selector': {'type': 'custom', 'pattern': 'smoke-tests'}}})

    provider = BitbucketCatalogProvider('email', 'TOKEN', httpx.MockTransport(handler))
    monkeypatch.setattr(BitbucketCatalogProvider, 'from_environment', classmethod(lambda cls: provider))
    app = create_app(ROOT / 'relay.example.json', tmp_path)
    s = app.state.execution
    record = preview(s)
    record, _ = s.claim(record['id'])
    record['attempted_at'] = 0
    s.save(record)
    with TestClient(app) as client:
        response = client.post(f"/api/plans/{record['id']}/reconcile", json={
            'confirmed': True, 'verified_parameters': True, 'pipeline_uuid': pipeline_uuid})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'submitted'
        assert response.json()['reconciled'] is True
        assert response.json()['pipeline']['uuid'] == UUID
        assert client.get(f"/api/plans/{record['id']}").json() == response.json()
        assert client.post(f"/api/plans/{record['id']}/execute", json={
            'confirmed': True}).json() == response.json()
    assert len(calls) == 1
    assert service(tmp_path).get(record['id']) == response.json()


@pytest.mark.parametrize('field,value', [
    ('pipeline_uuid', 'invalid'), ('pipeline_uuid', 123), ('pipeline_uuid', None),
    ('confirmed', False), ('confirmed', 'true'), ('confirmed', 1),
    ('verified_parameters', False), ('verified_parameters', 'true'), ('verified_parameters', 1),
])
def test_reconciliation_http_rejects_invalid_uuid_and_non_boolean_attestation(tmp_path, monkeypatch, field, value):
    def unexpected_provider(cls):
        pytest.fail('Invalid payload must not reach the provider')
    monkeypatch.setattr(BitbucketCatalogProvider, 'from_environment', classmethod(unexpected_provider))
    payload = {'pipeline_uuid': UUID, 'confirmed': True, 'verified_parameters': True, field: value}
    with TestClient(create_app(ROOT / 'relay.example.json', tmp_path)) as client:
        assert client.post('/api/plans/missing/reconcile', json=payload).status_code == 422


def test_recovery_expiration_only_for_unclaimed_preview(tmp_path):
    s = service(tmp_path)
    record = preview(s)
    assert s.recovery_snapshot(record['id'])['expired_unclaimed'] is False
    record['expires_at'] = 0
    s.save(record)
    assert s.recovery_snapshot(record['id'])['expired_unclaimed'] is True
    record['status'] = 'unknown'
    s.save(record)
    assert not s.recovery_snapshot(record['id']).get('expired_unclaimed', False)


def test_recovery_waits_for_claim_across_expiration(tmp_path, monkeypatch):
    import threading
    import time
    from relay.contracts import ExecutionPlan
    s = service(tmp_path)
    record = preview(s)
    deadline = record['expires_at']
    reached, resume, reading = threading.Event(), threading.Event(), threading.Event()
    original = ExecutionPlan.model_validate_json
    def paused(*args, **kwargs):
        reached.set()
        assert resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(ExecutionPlan, 'model_validate_json', paused)
    other = service(tmp_path)
    def read():
        reading.set()
        return other.recovery_snapshot(record['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        claim = pool.submit(s.claim, record['id'])
        try:
            assert reached.wait(3)
            monkeypatch.setattr(time, 'time', lambda: deadline + 1)
            snapshot = pool.submit(read)
            assert reading.wait(3)
            from concurrent.futures import TimeoutError
            with pytest.raises(TimeoutError):
                snapshot.result(timeout=0.1)
        finally:
            resume.set()
        assert claim.result()[1] is True
        value = snapshot.result()
        assert value['status'] == 'unknown'
        assert not value.get('expired_unclaimed', False)
