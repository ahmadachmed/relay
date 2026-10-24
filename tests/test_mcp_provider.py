import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from mcp.types import CallToolResult, TextContent, Tool
from fastapi.testclient import TestClient
from relay.api import create_app
from relay.registry import load_registry
from relay.providers import ProviderError
from relay.providers.mcp import MCPCatalogProvider, MCPConfig, decode

ROOT=Path(__file__).resolve().parents[1]


def config():
    return MCPConfig(url='https://mcp.example.com/mcp',repository={'tool':'repo','result_path':'data'},
                     branches={'tool':'branches','arguments':{'cursor':'$cursor'},'result_path':'values','cursor_path':'next'},
                     pipeline_file={'tool':'file','result_path':'content','arguments':{'ref':'$branch'}})


class Session:
    def __init__(self): self.calls=[]
    async def list_tools(self,cursor=None):
        names=['repo','branches'] if cursor is None else ['file']
        return NS(tools=[Tool(name=n,inputSchema={'type':'object'}) for n in names],nextCursor='next' if cursor is None else None)
    async def call_tool(self,name,arguments):
        self.calls.append((name,arguments))
        if name=='repo': data={'data':{'name':'api'}}
        elif name=='branches':
            data={'values':[{'name':'main' if arguments.get('cursor') is None else 'develop'}], 'next':'page2' if arguments.get('cursor') is None else None}
        else: data={'content':'pipelines:\n  custom:\n    smoke-tests:\n      - step:\n          script: ["SECRET"]\n'}
        return CallToolResult(content=[],structuredContent=data)


def provider(session,cfg=None):
    @asynccontextmanager
    async def connector(c,h):
        yield session
    return MCPCatalogProvider(cfg or config(),{},connector)


def project(): return load_registry(ROOT/'projects.example.json').projects['api']


def test_discovery_pagination_and_catalog():
    s=Session();result=asyncio.run(provider(s).catalog(project()))
    assert result['transport']=='mcp'
    assert result['branches']==['main','develop']
    assert result['pipelines'][0]['available']
    assert s.calls[-1]==('file',{'ref':'main'})
    assert 'SECRET' not in json.dumps(result)


def test_missing_tool_prevents_calls():
    c=config();c.repository.tool='missing';s=Session()
    with pytest.raises(ProviderError,match='not advertised'):
        asyncio.run(provider(s,c).catalog(project()))
    assert s.calls==[]


def test_schema_mismatch_prevents_calls():
    class Strict(Session):
        async def list_tools(self,cursor=None):
            r=await super().list_tools(cursor)
            for t in r.tools: t.inputSchema={'type':'object','required':['workspace']}
            return r
    s=Strict()
    with pytest.raises(ProviderError,match='schema'):
        asyncio.run(provider(s).catalog(project()))
    assert s.calls==[]


def test_tool_error_is_sanitized():
    with pytest.raises(ProviderError) as e:
        decode(CallToolResult(isError=True,content=[TextContent(type='text',text='secret credential')]))
    assert 'secret credential' not in str(e.value)


def test_text_json_fallback():
    assert decode(CallToolResult(content=[TextContent(type='text',text='{"data":1}')]))=={'data':1}


def test_repeated_cursor():
    class Repeated(Session):
        async def call_tool(self,name,arguments):
            if name=='branches':return CallToolResult(content=[],structuredContent={'values':[],'next':'same'})
            return await super().call_tool(name,arguments)
    with pytest.raises(ProviderError,match='cursor'):
        asyncio.run(provider(Repeated()).catalog(project()))


def test_connection_exception_sanitized():
    @asynccontextmanager
    async def broken(c,h):
        raise RuntimeError('secret header')
        yield
    with pytest.raises(ProviderError) as e:
        asyncio.run(MCPCatalogProvider(config(),{},broken).catalog(project()))
    assert e.value.code=='connection_failed'
    assert 'secret header' not in str(e.value)


def test_environment_headers(tmp_path,monkeypatch):
    c=config();c.header_env={'x-workbench-api-key':'TEST_MCP_SECRET'}
    path=tmp_path/'mcp.json';path.write_text(c.model_dump_json())
    monkeypatch.setenv('RELAY_MCP_CONFIG',str(path));monkeypatch.setenv('TEST_MCP_SECRET','test-only')
    p=MCPCatalogProvider.from_environment()
    assert p.headers=={'x-workbench-api-key':'test-only'}
    assert 'test-only' not in p.config.model_dump_json()


def test_unconfigured_api(tmp_path,monkeypatch):
    monkeypatch.delenv('RELAY_MCP_CONFIG',raising=False)
    with TestClient(create_app(ROOT/'relay.example.json',tmp_path,ROOT/'projects.example.json')) as c:
        r=c.get('/api/project-registry/api/catalog')
        assert r.status_code==503
        assert r.json()['detail']['code']=='not_configured'


@pytest.mark.parametrize('url',['http://remote.example/mcp','https://user:pass@host/mcp','https://host/mcp?token=secret'])
def test_invalid_server_urls(url):
    d=config().model_dump();d['url']=url
    with pytest.raises(ValueError):MCPConfig.model_validate(d)


def test_real_sdk_streamable_http_handshake(monkeypatch):
    """Exercise initialize/list/call and custom header auth over loopback HTTP."""
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")  # Test server is loopback only.
    import socket
    import threading
    import time
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    server_mcp=FastMCP('test-catalog',stateless_http=True,json_response=True)
    @server_mcp.tool()
    def repo() -> dict:
        return {'data':{'name':'api'}}
    @server_mcp.tool()
    def branches(cursor: str | None = None) -> dict:
        return {'values':[{'name':'main'}],'next':None}
    @server_mcp.tool()
    def file(ref: str) -> dict:
        return {'content':'pipelines:\n  custom:\n    smoke-tests: []\n'}
    class Auth(BaseHTTPMiddleware):
        async def dispatch(self,request,call_next):
            if request.headers.get('x-test-key')!='test-only':
                return JSONResponse({'error':'unauthorized'},status_code=401)
            return await call_next(request)
    app=server_mcp.streamable_http_app();app.add_middleware(Auth)
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    try:
        deadline=time.monotonic()+5
        while not server.started and time.monotonic()<deadline:time.sleep(.01)
        assert server.started
        d=config().model_dump();d['url']=f'http://127.0.0.1:{port}/mcp'
        result=asyncio.run(MCPCatalogProvider(MCPConfig.model_validate(d),{'x-test-key':'test-only'}).catalog(project()))
        assert result['connected'] and result['pipelines'][0]['available']
    finally:
        server.should_exit=True;thread.join(timeout=5);sock.close()


def test_optional_string_cursor_omitted_initially():
    class OptionalCursor(Session):
        async def list_tools(self,cursor=None):
            result=await super().list_tools(cursor)
            for tool in result.tools:
                if tool.name=='branches':
                    tool.inputSchema={'type':'object','properties':{'cursor':{'type':'string'}}}
            return result
    s=OptionalCursor()
    asyncio.run(provider(s).catalog(project()))
    assert ('branches',{}) in s.calls
    assert ('branches',{'cursor':'page2'}) in s.calls


@pytest.mark.parametrize('allow_missing',[True,False])
def test_terminal_cursor_policy(allow_missing):
    class MissingNext(Session):
        async def call_tool(self,name,arguments):
            if name=='branches':
                return CallToolResult(content=[],structuredContent={'values':[{'name':'main'}]})
            return await super().call_tool(name,arguments)
    c=config();c.branches.missing_cursor_is_end=allow_missing
    if allow_missing:
        assert asyncio.run(provider(MissingNext(),c).catalog(project()))['connected']
    else:
        with pytest.raises(ProviderError,match='field is missing'):
            asyncio.run(provider(MissingNext(),c).catalog(project()))


@pytest.mark.parametrize('error,code',[(ProviderError('tool_missing','Configured tool missing.'),'tool_missing'),(TimeoutError('secret'),'timeout'),(RuntimeError('secret'),'connection_failed')])
def test_nested_exception_groups(error,code):
    @asynccontextmanager
    async def grouped(c,h):
        raise ExceptionGroup('secret wrapper',[ExceptionGroup('nested',[error])])
        yield
    with pytest.raises(ProviderError) as caught:
        asyncio.run(MCPCatalogProvider(config(),{},grouped).catalog(project()))
    assert caught.value.code==code
    assert 'secret' not in str(caught.value)


def test_initial_null_can_be_enabled():
    c=config();c.branches.omit_initial_cursor=False;s=Session()
    asyncio.run(provider(s,c).catalog(project()))
    assert ('branches',{'cursor':None}) in s.calls


@pytest.mark.parametrize('status,code',[(401,'unauthorized'),(403,'forbidden'),(429,'rate_limited'),(500,'upstream_error')])
@pytest.mark.parametrize('grouped',[False,True])
def test_http_errors_are_classified_without_leaking(status,code,grouped):
    import httpx
    @asynccontextmanager
    async def failed(c,h):
        request=httpx.Request('POST','https://mcp.example.com/mcp',headers={'Authorization':'Bearer secret'})
        response=httpx.Response(status,request=request,text='sensitive upstream body')
        error=httpx.HTTPStatusError('secret exception text',request=request,response=response)
        if grouped:
            raise ExceptionGroup('secret wrapper',[ExceptionGroup('inner',[error])])
        raise error
        yield
    with pytest.raises(ProviderError) as caught:
        asyncio.run(MCPCatalogProvider(config(),{},failed).catalog(project()))
    assert caught.value.code==code
    assert 'secret' not in str(caught.value)
    assert 'sensitive' not in str(caught.value)


@pytest.mark.parametrize('code,http_status',[('unauthorized',502),('forbidden',502),('rate_limited',503)])
def test_api_preserves_provider_error_codes(tmp_path,monkeypatch,code,http_status):
    class FailedProvider:
        async def catalog(self,project):
            raise ProviderError(code,'Sanitized diagnostic')
    monkeypatch.setattr(MCPCatalogProvider,'from_environment',classmethod(lambda cls:FailedProvider()))
    with TestClient(create_app(ROOT/'relay.example.json',tmp_path,ROOT/'projects.example.json')) as client:
        response=client.get('/api/project-registry/api/catalog')
    assert response.status_code==http_status
    assert response.json()['detail']=={'code':code,'message':'Sanitized diagnostic'}
