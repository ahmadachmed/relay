import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from relay.api import create_app
from relay.providers import ProviderError
from relay.providers.bitbucket import BitbucketCatalogProvider, MAX_BYTES
from relay.registry import load_registry

ROOT = Path(__file__).resolve().parents[1]
COMMIT = 'a' * 40
YAML = 'pipelines:\n  custom:\n    smoke-tests:\n      - step:\n          script: [echo SECRET]\n'


def project():
    return load_registry(ROOT / 'projects.example.json').projects['api']


def provider(handler):
    return BitbucketCatalogProvider('qa@example.com', 'private-token', httpx.MockTransport(handler))


def response(request, raw=YAML):
    if '/refs/branches/' in request.url.path:
        return httpx.Response(200, json={'name': project().default_branch, 'target': {'hash': COMMIT}})
    if '/src/' in request.url.path:
        return httpx.Response(200, text=raw)
    return httpx.Response(200, json={'full_name': project().repository})


def test_large_raw_file_and_pinned_commit():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.headers['authorization'].startswith('Basic ')
        return response(request, '# ' + 'x' * 221117 + '\n' + YAML)
    result = asyncio.run(provider(handler).catalog(project()))
    assert result['transport'] == 'rest'
    assert result['connected']
    assert result['commit'] == COMMIT
    assert result['branch_scope'] == 'default_only'
    assert result['branches'] == ['main']
    assert result['pipelines'][0]['available']
    assert len(calls) == 3
    assert calls[-1].url.path.endswith(f'/src/{COMMIT}/bitbucket-pipelines.yml')
    assert 'SECRET' not in json.dumps(result)
    assert 'private-token' not in json.dumps(result)


@pytest.mark.parametrize('status,code', [(401,'unauthorized'), (403,'forbidden'),
    (404,'not_found'), (429,'rate_limited'), (500,'upstream_error'), (302,'upstream_error')])
def test_http_errors_and_redirects(status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='secret upstream body', headers={'Location':'https://other.example'})
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider(handler).catalog(project()))
    assert caught.value.code == code
    assert 'secret' not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize('raw', ['error: file not found', '{}', 'pipelines: null',
    'pipelines: {}', 'pipelines: [bad]', 'pipelines:\n  custom: []',
    'pipelines:\n  custom:\n    smoke-tests: null', '[broken'])
def test_invalid_pipeline_does_not_report_connected(raw):
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider(lambda r: response(r, raw)).catalog(project()))
    assert caught.value.code == 'invalid_pipeline_config'


def test_missing_selector_is_unavailable():
    raw = 'pipelines:\n  default:\n    - step:\n        script: [echo ok]\n'
    result = asyncio.run(provider(lambda r: response(r, raw)).catalog(project()))
    assert result['connected'] and not result['pipelines'][0]['available']


@pytest.mark.parametrize('stage', ['repository', 'branch'])
def test_invalid_identity_stops_before_file(stage):
    calls = []
    def handler(request):
        calls.append(request)
        if stage == 'repository' or '/refs/' in request.url.path:
            return httpx.Response(200, json={'error': 'secret'})
        return response(request)
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider(handler).catalog(project()))
    assert caught.value.code == 'invalid_response'
    assert all('/src/' not in r.url.path for r in calls)


def test_response_size_bound():
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider(lambda r: httpx.Response(200, content=b'x' * (MAX_BYTES + 1))).catalog(project()))
    assert caught.value.code == 'response_too_large'


@pytest.mark.parametrize('error,code', [(httpx.ReadTimeout('secret'),'timeout'),
    (httpx.ConnectError('secret'),'connection_failed')])
def test_transport_errors_are_sanitized(error, code):
    def handler(request):
        raise error
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider(handler).catalog(project()))
    assert caught.value.code == code
    assert 'secret' not in str(caught.value)


def test_environment_and_no_mcp_requirement(monkeypatch):
    monkeypatch.delenv('RELAY_MCP_CONFIG', raising=False)
    monkeypatch.setenv('RELAY_BITBUCKET_EMAIL', 'qa@example.com')
    monkeypatch.setenv('RELAY_BITBUCKET_API_TOKEN', 'test-token')
    assert isinstance(BitbucketCatalogProvider.from_environment(), BitbucketCatalogProvider)
    monkeypatch.delenv('RELAY_BITBUCKET_API_TOKEN')
    with pytest.raises(ProviderError) as caught:
        BitbucketCatalogProvider.from_environment()
    assert caught.value.code == 'not_configured'


def test_catalog_api_uses_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(BitbucketCatalogProvider, 'from_environment',
        classmethod(lambda cls: provider(response)))
    with TestClient(create_app(ROOT/'relay.example.json', tmp_path, ROOT/'projects.example.json')) as client:
        r = client.get('/api/project-registry/api/catalog')
    assert r.status_code == 200
    assert r.json()['transport'] == 'rest'
    assert r.json()['pipelines'][0]['available']


@pytest.mark.parametrize('code,status', [('not_configured',503), ('unauthorized',502),
    ('forbidden',502), ('rate_limited',503)])
def test_catalog_api_errors(tmp_path, monkeypatch, code, status):
    def fail(cls):
        raise ProviderError(code, 'Sanitized diagnostic')
    monkeypatch.setattr(BitbucketCatalogProvider, 'from_environment', classmethod(fail))
    with TestClient(create_app(ROOT/'relay.example.json', tmp_path, ROOT/'projects.example.json')) as client:
        r = client.get('/api/project-registry/api/catalog')
    assert r.status_code == status
    assert r.json()['detail']['code'] == code
