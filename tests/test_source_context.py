import httpx
import pytest
from fastapi.testclient import TestClient
from relay.api import create_app
from test_monitoring import submitted

BODY=b'assert response.status_code == 200\npassword="private-value"'


@pytest.fixture
def source(tmp_path,monkeypatch):
    monkeypatch.setenv('RELAY_SOURCE_ENABLED','1')
    monkeypatch.setenv('RELAY_BITBUCKET_EMAIL','qa@example.invalid')
    monkeypatch.setenv('RELAY_BITBUCKET_API_TOKEN','test-only-token')
    app=create_app(data=tmp_path,monitoring=False)
    project=app.state.execution.registry.projects['api']
    project.source_files=['tests/test_api.py']
    run=submitted(app.state.execution)
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.method=='GET'
        assert '/src/'+run['commit']+'/tests/test_api.py' in request.url.path
        if request.url.query:
            return httpx.Response(200,json={'type':'commit_file','path':'tests/test_api.py','commit':{'hash':run['commit']},'attributes':[],'size':len(BODY)})
        return httpx.Response(200,content=BODY)
    app.state.source_context.transport=httpx.MockTransport(handler)
    with TestClient(app) as client:
        yield app,client,run,calls


def preview(client,run,**changes):
    return client.post(f"/api/remote-runs/{run['id']}/source-preview",json={'path':'tests/test_api.py','confirmed':True}|changes)


def test_read_pinned_source_and_redact(source):
    app,client,run,calls=source
    result=preview(client,run)
    assert result.status_code==200
    assert len(calls)==2
    data=result.json()
    assert data['commit']==run['commit']
    assert 'private-value' not in data['text']
    assert 'assert response' in data['text']
    assert data['sha256'] and data['source_id']

@pytest.mark.parametrize('path',['../secret.py','.env','tests/credentials.json','other.py','tests/test_api.py/','https://example.org/file.py'])
def test_unapproved_paths_never_contact_provider(source,path):
    app,client,run,calls=source
    assert preview(client,run,path=path).status_code in (403,422)
    assert not calls

@pytest.mark.parametrize('change',[{'attributes':['link']},{'attributes':['binary']},{'attributes':['subrepository']},{'type':'commit_directory'},{'commit':{'hash':'b'*40}},{'size':24001}])
def test_invalid_metadata_blocks_content(source,change):
    app,client,run,calls=source
    count=[]
    def handler(request):
        count.append(request)
        return httpx.Response(200,json={'type':'commit_file','path':'tests/test_api.py','commit':{'hash':run['commit']},'attributes':[],'size':len(BODY)}|change)
    app.state.source_context.transport=httpx.MockTransport(handler)
    assert preview(client,run).status_code==502
    assert len(count)==1


def test_disabled_confirmation_and_repository_remap(source,monkeypatch):
    app,client,run,calls=source
    assert preview(client,run,confirmed=False).status_code==422
    assert preview(client,run,confirmed=1).status_code==422
    monkeypatch.delenv('RELAY_SOURCE_ENABLED')
    assert preview(client,run).status_code==503
    monkeypatch.setenv('RELAY_SOURCE_ENABLED','1')
    app.state.execution.registry.projects['api'].repository='other/repo'
    assert preview(client,run).status_code==403
    assert not calls

@pytest.mark.parametrize('status',[401,403,404,429,302])
def test_no_redirect_retry_or_head_fallback(source,status):
    app,client,run,calls=source
    count=[]
    def handler(request):
        count.append(request)
        return httpx.Response(status,headers={'Location':'https://example.org/private'})
    app.state.source_context.transport=httpx.MockTransport(handler)
    assert preview(client,run).status_code==502
    assert len(count)==1


@pytest.mark.parametrize('directory', ['tests', 'tests/'])
def test_directory_permission_includes_direct_and_nested_files(source,directory):
    app,client,run,calls=source
    project=app.state.execution.registry.projects['api']
    project.source_files=[]
    project.source_directories=[directory]
    for path in ('tests/test_api.py','tests/nested/test_api.py'):
        requests=[]
        def handler(request):
            requests.append(request)
            if request.url.query:
                return httpx.Response(200,json={'type':'commit_file','path':path,'commit':{'hash':run['commit']},'attributes':[],'size':len(BODY)})
            return httpx.Response(200,content=BODY)
        app.state.source_context.transport=httpx.MockTransport(handler)
        assert preview(client,run,path=path).status_code==200
        assert len(requests)==2
        assert all('/src/'+run['commit']+'/' in r.url.path for r in requests)

@pytest.mark.parametrize('path',['tests-other/test_api.py','tests2/test_api.py','tests/../other.py','tests/.env','tests/secret.py','tests/blob.png','other/test_api.py'])
def test_directory_permission_keeps_boundaries_and_denials(source,path):
    app,client,run,calls=source
    project=app.state.execution.registry.projects['api']
    project.source_files=[]
    project.source_directories=['tests/']
    assert preview(client,run,path=path).status_code in (403,422)
    assert not calls

@pytest.mark.parametrize('attribute',['link','binary','subrepository'])
def test_directory_permission_does_not_bypass_metadata(source,attribute):
    app,client,run,calls=source
    project=app.state.execution.registry.projects['api']
    project.source_files=[];project.source_directories=['tests/']
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200,json={'type':'commit_file','path':'tests/test_api.py','commit':{'hash':run['commit']},'attributes':[attribute],'size':len(BODY)})
    app.state.source_context.transport=httpx.MockTransport(handler)
    assert preview(client,run).status_code==502
    assert len(seen)==1

@pytest.mark.parametrize('directories',[['/'],[''],['.'],['../tests'],['tests/../other'],['tests//'],['tests/*'],['.git/'],['tests/secret/'],['tests','tests/'],['https://example.org/'],['tests\\nested']])
def test_invalid_directory_configuration_is_rejected(source,directories):
    from pydantic import ValidationError
    from relay.contracts import Project
    app,client,run,calls=source
    config=app.state.execution.registry.projects['api'].model_dump()
    config['source_directories']=directories
    with pytest.raises(ValidationError):
        Project.model_validate(config)
    assert not calls


def test_directory_configuration_normalizes_and_defaults(source):
    from relay.contracts import Project
    app,_,_,_=source
    config=app.state.execution.registry.projects['api'].model_dump()
    config.pop('source_directories')
    assert Project.model_validate(config).source_directories==[]
    config['source_directories']=['tests','keywords/']
    assert Project.model_validate(config).source_directories==['tests/','keywords/']
