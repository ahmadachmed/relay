import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from relay.api import create_app
from relay.contracts import PlanRequest
from relay.execution import ExecutionService
from relay.monitoring import RunMonitor, MONITOR_WINDOW
from relay.providers import ProviderError
from relay.providers.bitbucket_runs import BitbucketRunProvider
from relay.registry import load_registry

ROOT = Path(__file__).resolve().parents[1]
UUID = '{12345678-1234-1234-1234-123456789abc}'
STEP = '{87654321-4321-4321-4321-cba987654321}'


def submitted(execution):
    class Submit:
        async def catalog(self, project):
            return {'commit': 'a' * 40, 'pipelines': [{'id': 'smoke', 'available': True}]}
        async def trigger(self, record):
            return {'uuid': UUID, 'url': 'https://bitbucket.org/example/api-automation/pipelines/results/1'}
    async def create():
        p = await execution.preview(PlanRequest(project_id='api', pipeline_id='smoke'), Submit())
        return await execution.execute(p['id'], Submit())
    return asyncio.run(create())


def setup(tmp_path, factory=None):
    execution = ExecutionService(tmp_path / 'remote.sqlite3', load_registry(ROOT / 'projects.example.json'))
    plan = submitted(execution)
    return execution, plan, RunMonitor(execution, factory)


def provider(handler):
    return BitbucketRunProvider('email', 'SECRET_TOKEN', httpx.MockTransport(handler))


def remote_response(request, state='IN_PROGRESS', result=None):
    status = {'name': state}
    if result:
        status['result'] = {'name': result}
    if '/steps' in request.url.path:
        return httpx.Response(200, json={'values': [{'uuid': STEP, 'name': 'Smoke', 'state': status,
            'script_commands': ['SECRET'], 'started_on': '2026-10-24T00:00:00Z'}]})
    return httpx.Response(200, json={'uuid': UUID, 'state': status, 'variables': ['SECRET'],
        'created_on': '2026-10-24T00:00:00Z', 'completed_on': None})


@pytest.mark.parametrize('state,result,expected', [
    ('PENDING', None, 'pending'), ('IN_PROGRESS', None, 'running'),
    ('HALTED', None, 'pending'), ('NEW_STATE', None, 'unknown'),
    ('COMPLETED', 'SUCCESSFUL', 'successful'), ('COMPLETED', 'FAILED', 'failed'),
    ('COMPLETED', 'ERROR', 'failed'), ('COMPLETED', 'STOPPED', 'stopped'),
    ('COMPLETED', 'EXPIRED', 'failed'), ('COMPLETED', 'NEW_RESULT', 'unknown'),
])
def test_status_mapping_and_test_evidence_stays_separate(tmp_path, state, result, expected):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        return remote_response(request, state, result)
    _, plan, monitor = setup(tmp_path, lambda: provider(handler))
    run = asyncio.run(monitor.refresh(plan['id']))
    assert run['pipeline_status'] == expected
    assert run['steps'][0]['status'] == expected
    assert run['test_status'] == 'not_collected'
    assert run['terminal'] == (state == 'COMPLETED')
    assert run['last_checked_at'] is not None
    assert 'SECRET' not in json.dumps(run)
    assert len(calls) == 2


def test_steps_pagination_and_no_external_follow(tmp_path):
    _, plan, _ = setup(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        if '/steps' not in request.url.path:
            return remote_response(request)
        if request.url.params.get('page') == '2':
            return httpx.Response(200, json={'values': [{'uuid': STEP, 'state': {'name': 'PENDING'}}]})
        return httpx.Response(200, json={'values': [], 'next': str(request.url.copy_set_param('page', '2'))})
    result = asyncio.run(provider(handler).status(plan))
    assert len(result['steps']) == 1
    assert len(calls) == 3

    for destination in ['https://evil.example/steps', 'https://api.bitbucket.org/2.0/user',
                        'https://user:pass@api.bitbucket.org/steps']:
        calls.clear()
        def invalid(request):
            calls.append(request)
            if '/steps' not in request.url.path:
                return remote_response(request)
            return httpx.Response(200, json={'values': [], 'next': destination})
        with pytest.raises(ProviderError, match='Invalid pipeline'):
            asyncio.run(provider(invalid).status(plan))
        assert len(calls) == 2


def test_read_error_backoff_preserves_last_observation(tmp_path):
    fail = False
    def handler(request):
        if fail:
            return httpx.Response(429, text='SECRET')
        return remote_response(request)
    ex, plan, monitor = setup(tmp_path, lambda: provider(handler))
    initial = asyncio.run(monitor.refresh(plan['id']))
    fail = True
    current = asyncio.run(monitor.refresh(plan['id']))
    assert current['pipeline_status'] == 'running'
    assert current['last_checked_at'] == initial['last_checked_at']
    assert current['monitoring_status'] == 'retrying'
    assert current['error']['code'] == 'rate_limited'
    assert current['consecutive_errors'] == 1
    with ex.connect() as db:
        due = db.execute('SELECT next_poll FROM remote_runs').fetchone()[0]
    assert due > time.time() + 15
    assert 'SECRET' not in json.dumps(current)
    fail = False
    assert asyncio.run(monitor.refresh(plan['id']))['error'] is None


def test_restart_recovers_old_submitted_plan_without_post(tmp_path):
    ex, plan, first = setup(tmp_path)
    with ex.connect() as db:
        db.execute('DROP TABLE remote_runs')
    calls = []
    def handler(request):
        calls.append(request.method)
        return remote_response(request)
    second = RunMonitor(ex, lambda: provider(handler))
    asyncio.run(second.tick())
    assert second.get(plan['id'])['pipeline_status'] == 'running'
    assert calls == ['GET', 'GET']
    assert second.list()[0]['id'] == plan['id']
    assert ex.get(plan['id'])['pipeline']['uuid'] == UUID


def test_lease_fences_stale_worker_and_expires(tmp_path):
    ex, plan, monitor = setup(tmp_path)
    token = monitor.claim_poll(plan['id'])
    assert token
    other = RunMonitor(ex)
    assert other.claim_poll(plan['id'], True) is None
    with ex.connect() as db:
        db.execute('UPDATE remote_runs SET lease_until=0')
    assert other.claim_poll(plan['id']) != token


def test_stale_poll_result_cannot_overwrite_newer_result(tmp_path):
    ex, plan, monitor = setup(tmp_path)
    class Slow:
        async def status(self, record):
            with ex.connect() as db:
                db.execute('UPDATE remote_runs SET lease_until=0')
            await newer.refresh(plan['id'])
            return {'pipeline_status': 'running', 'terminal': False, 'steps': []}
    newer = RunMonitor(ex, lambda: provider(lambda r: remote_response(r, 'COMPLETED', 'SUCCESSFUL')))
    monitor.provider_factory = Slow
    assert asyncio.run(monitor.refresh(plan['id']))['pipeline_status'] == 'successful'


def test_monitor_timeout_is_not_remote_failure_and_refresh_resumes(tmp_path):
    ex, plan, monitor = setup(tmp_path, lambda: provider(remote_response))
    row = monitor.get(plan['id'])
    row['monitoring_started_at'] = time.time() - MONITOR_WINDOW - 1
    with ex.connect() as db:
        db.execute('UPDATE remote_runs SET record=?', (json.dumps(row),))
    asyncio.run(monitor.tick())
    assert monitor.get(plan['id'])['monitoring_status'] == 'timed_out'
    assert monitor.get(plan['id'])['pipeline_status'] == 'unknown'
    assert asyncio.run(monitor.refresh(plan['id']))['monitoring_status'] == 'active'


@pytest.mark.parametrize('outcome,expected', [(204, 'acknowledged'), (403, 'rejected'), (500, 'unknown'), ('timeout', 'unknown')])
def test_cancel_is_durable_and_needs_remote_ack(tmp_path, outcome, expected):
    posts = []
    stopped = False
    def handler(request):
        if request.method == 'POST':
            posts.append(request)
            assert request.url.path.endswith('/stopPipeline')
            if outcome == 'timeout':
                raise httpx.ReadTimeout('SECRET')
            return httpx.Response(outcome, text='SECRET')
        return remote_response(request, 'COMPLETED' if stopped else 'IN_PROGRESS', 'STOPPED' if stopped else None)
    ex, plan, monitor = setup(tmp_path, lambda: provider(handler))
    asyncio.run(monitor.refresh(plan['id']))
    run = asyncio.run(monitor.cancel(plan['id']))
    assert run['cancel_status'] == expected
    assert run['pipeline_status'] == 'running'
    restarted = RunMonitor(ex, lambda: provider(handler))
    asyncio.run(restarted.cancel(plan['id']))
    assert len(posts) == 1
    assert 'SECRET' not in json.dumps(run)
    stopped = True
    run = asyncio.run(restarted.refresh(plan['id']))
    assert run['pipeline_status'] == 'stopped'
    assert run['cancel_status'] == 'confirmed'


def test_cancel_completion_race_keeps_observed_terminal_state(tmp_path):
    ex, plan, monitor = setup(tmp_path)
    class Racing:
        async def cancel(self, record):
            await observer.refresh(plan['id'])
    observer = RunMonitor(ex, lambda: provider(lambda r: remote_response(r, 'COMPLETED', 'SUCCESSFUL')))
    monitor.provider_factory = Racing
    result = asyncio.run(monitor.cancel(plan['id']))
    assert result['pipeline_status'] == 'successful'
    assert result['cancel_status'] == 'not_stopped'


def test_concurrent_cancel_only_posts_once(tmp_path):
    calls = []
    class Cancel:
        async def cancel(self, record):
            calls.append(1)
            await asyncio.sleep(0)
    ex, plan, monitor = setup(tmp_path, Cancel)
    other = RunMonitor(ex, Cancel)
    async def both():
        await asyncio.gather(monitor.cancel(plan['id']), other.cancel(plan['id']))
    asyncio.run(both())
    assert len(calls) == 1


def test_http_history_refresh_cancel_and_restart(tmp_path):
    app = create_app(ROOT / 'relay.example.json', tmp_path, monitoring=False)
    plan = submitted(app.state.execution)
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == 'POST':
            return httpx.Response(204)
        return remote_response(request)
    app.state.monitor.provider_factory = lambda: provider(handler)
    path = '/api/remote-runs/' + plan['id']
    with TestClient(app) as client:
        assert client.get('/api/remote-runs').json()[0]['id'] == plan['id']
        assert client.get('/api/remote-runs?limit=0').status_code == 422
        assert client.get('/api/remote-runs/missing').status_code == 404
        assert client.post(path + '/refresh').json()['pipeline_status'] == 'running'
        for body in [{}, {'confirmed': False}, {'confirmed': 1}, {'confirmed': 'true'}]:
            assert client.post(path + '/cancel', json=body).status_code == 422
        assert client.post(path + '/cancel', json={'confirmed': True}).json()['cancel_status'] == 'acknowledged'
        assert client.get('/api/runs').json() == []
    restarted = create_app(ROOT / 'relay.example.json', tmp_path, monitoring=False)
    with TestClient(restarted) as client:
        assert client.get(path).json()['pipeline_status'] == 'running'
    assert calls == ['GET', 'GET', 'POST']


def test_lifespan_polling_starts_and_is_stopped(tmp_path):
    app = create_app(ROOT / 'relay.example.json', tmp_path)
    plan = submitted(app.state.execution)
    app.state.monitor.provider_factory = lambda: provider(lambda r: remote_response(r, 'COMPLETED', 'SUCCESSFUL'))
    with TestClient(app) as client:
        for _ in range(100):
            if client.get('/api/remote-runs/' + plan['id']).json()['terminal']:
                break
            time.sleep(.01)
        else:
            pytest.fail('Lifespan did not poll the submitted pipeline')
    assert app.state.monitor.get(plan['id'])['pipeline_status'] == 'successful'


@pytest.mark.parametrize('failure', ['wrong_uuid', 'malformed_steps', 'pagination_loop', 'redirect', 'timeout'])
def test_invalid_remote_snapshots_never_report_success(tmp_path, failure):
    calls = []
    def handler(request):
        calls.append(request)
        if failure == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://evil.example'}, text='SECRET')
        if failure == 'timeout':
            raise httpx.ReadTimeout('SECRET')
        if failure == 'wrong_uuid':
            return httpx.Response(200, json={'uuid': STEP, 'state': {'name': 'COMPLETED'}})
        if '/steps' in request.url.path:
            if failure == 'malformed_steps':
                return httpx.Response(200, json={'values': 'SECRET'})
            return httpx.Response(200, json={'values': [], 'next': str(request.url)})
        return remote_response(request, 'COMPLETED', 'SUCCESSFUL')
    _, plan, monitor = setup(tmp_path, lambda: provider(handler))
    run = asyncio.run(monitor.refresh(plan['id']))
    assert run['pipeline_status'] == 'unknown'
    assert run['last_checked_at'] is None
    assert run['error']
    assert 'SECRET' not in json.dumps(run)
    assert len(calls) <= 2


def test_interrupted_cancel_is_not_reposted_on_restart(tmp_path):
    ex, plan, monitor = setup(tmp_path)
    run = monitor.get(plan['id'])
    run.update(cancel_status='requested', cancel_requested_at=0)
    with ex.connect() as db:
        db.execute('UPDATE remote_runs SET record=?', (json.dumps(run),))
    calls = []
    def handler(request):
        calls.append(request.method)
        return remote_response(request)
    restarted = RunMonitor(ex, lambda: provider(handler))
    asyncio.run(restarted.tick())
    result = restarted.get(plan['id'])
    assert result['cancel_status'] == 'unknown'
    assert result['pipeline_status'] == 'running'
    assert calls == ['GET', 'GET']


def test_history_sut_comes_from_saved_plan_for_existing_runs(tmp_path):
    execution, plan, monitor = setup(tmp_path)
    monitor.adopt()
    saved = execution.get(plan['id'])
    saved['plan']['sut_id'] = 'service-a'
    execution.save(saved)
    assert monitor.list()[0]['sut_id'] == 'service-a'
    saved['plan'].pop('sut_id')
    execution.save(saved)
    assert monitor.list()[0]['sut_id'] is None
