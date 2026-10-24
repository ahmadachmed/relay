import httpx
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from relay.api import create_app
from relay.registry import load_registry
from relay.providers import ProviderError
from relay.providers.bitbucket import BitbucketProvider, BASE

ROOT = Path(__file__).resolve().parents[1]


def project():
    return load_registry(ROOT/'projects.example.json').projects['api']


def test_catalog_pagination_and_no_yaml_leaks():
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == 'GET'
        path = request.url.path
        if path.endswith('/refs/branches'):
            if 'page=2' in str(request.url):
                return httpx.Response(200,json={'values':[{'name':'develop'}]})
            return httpx.Response(200,json={'values':[{'name':'main'}], 'next':BASE+'/repositories/example/api-automation/refs/branches?page=2'})
        if path.endswith('bitbucket-pipelines.yml'):
            return httpx.Response(200,text='pipelines:\n  custom:\n    smoke-tests:\n      - step:\n          script: ["echo secret-script"]\n    hidden-pipeline: []\n')
        return httpx.Response(200,json={'name':'api'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        catalog = BitbucketProvider(client).catalog(project())
    assert catalog['branches'] == ['main','develop']
    assert catalog['pipelines'][0]['available'] is True
    assert len(calls) == 4
    assert 'secret-script' not in str(catalog)
    assert 'hidden-pipeline' not in str(catalog)


@pytest.mark.parametrize('status,code',[(401,'unauthorized'),(403,'forbidden'),(404,'not_found'),(429,'rate_limited'),(500,'upstream_error'),(302,'upstream_error')])
def test_errors_are_sanitized(status,code):
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(status,text='secret-token'))) as client:
        with pytest.raises(ProviderError) as e:
            BitbucketProvider(client).catalog(project())
    assert e.value.code == code
    assert 'secret-token' not in str(e.value)


def test_pagination_cannot_leak_auth():
    calls=[]
    def handler(r):
        calls.append(r)
        return httpx.Response(200,json={'values':[], 'next':'https://evil.example/2.0/repositories/example/api-automation/refs/branches'})
    with httpx.Client(transport=httpx.MockTransport(handler),auth=('user','secret')) as client:
        with pytest.raises(ProviderError):
            BitbucketProvider(client).catalog(project())
    assert len(calls)==2
    assert all(r.url.host=='api.bitbucket.org' for r in calls)


def test_missing_credentials(monkeypatch,tmp_path):
    monkeypatch.delenv('RELAY_BITBUCKET_EMAIL',raising=False)
    monkeypatch.delenv('RELAY_BITBUCKET_API_TOKEN',raising=False)
    with TestClient(create_app(ROOT/'relay.example.json',tmp_path,ROOT/'projects.example.json')) as client:
        r=client.get('/api/project-registry/api/catalog')
        assert r.status_code==503
        assert r.json()['detail']['code']=='not_configured'
        assert client.get('/api/project-registry/missing/catalog').status_code==404


@pytest.mark.parametrize('body',['!!python/object/apply:os.system [echo BAD]', '[]', 'pipelines: null'])
def test_invalid_yaml(body):
    def handle(r):
        if r.url.path.endswith('/refs/branches'):
            return httpx.Response(200,json={'values':[{'name':'main'}]})
        if r.url.path.endswith('.yml'):
            return httpx.Response(200,text=body)
        return httpx.Response(200,json={})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError,match='custom pipeline'):
            BitbucketProvider(client).catalog(project())


def test_network_error_is_sanitized():
    def handle(r):
        raise httpx.ConnectError('secret-token')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as e:
            BitbucketProvider(client).catalog(project())
        assert e.value.code=='connection_failed'
        assert 'secret-token' not in str(e.value)
