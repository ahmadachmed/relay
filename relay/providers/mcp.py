"""MCP Streamable HTTP catalog adapter with explicit administrator tool bindings."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
import httpx
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import Field, model_validator
from ..contracts import Contract, Project
from . import ProviderError


class Binding(Contract):
    tool: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    result_path: str = ''
    cursor_path: str | None = None
    missing_cursor_is_end: bool = False
    omit_initial_cursor: bool = True


class MCPConfig(Contract):
    url: str
    header_env: dict[str, str] = Field(default_factory=dict)
    repository: Binding
    branches: Binding
    pipeline_file: Binding
    timeout_seconds: int = Field(default=30, ge=1, le=120)

    @model_validator(mode='after')
    def valid_url(self):
        u = urlsplit(self.url)
        if u.username or u.password or u.query or u.fragment or not u.hostname:
            raise ValueError('MCP URL must not contain credentials, query or fragment')
        if u.scheme != 'https' and not (u.scheme == 'http' and u.hostname in ('localhost','127.0.0.1')):
            raise ValueError('MCP requires HTTPS except on loopback')
        return self


def select(value, path):
    for key in path.split('.') if path else []:
        if not isinstance(value, dict) or key not in value:
            raise ProviderError('invalid_response', 'Configured MCP result field is missing.')
        value = value[key]
    return value


def render(value, context, omit_initial_cursor=False):
    if isinstance(value, str):
        if value.startswith('$'):
            if value[1:] not in context:
                raise ProviderError('invalid_configuration', 'Unknown MCP argument placeholder.')
            return context[value[1:]]
        return value
    if isinstance(value, dict):
        return {k:render(v,context,omit_initial_cursor) for k,v in value.items()
                if not (omit_initial_cursor and v == "$cursor" and context.get("cursor") is None)}
    if isinstance(value, list):
        return [render(v,context,omit_initial_cursor) for v in value]
    return value


def decode(result):
    if result.isError:
        raise ProviderError('tool_error', 'MCP tool failed; check server permissions and configuration.')
    value = result.structuredContent
    if value is None:
        texts = [c.text for c in result.content if c.type == 'text']
        if len(texts) != 1:
            raise ProviderError('invalid_response', 'Expected one JSON MCP response.')
        try:
            value = json.loads(texts[0])
        except ValueError:
            raise ProviderError('invalid_response', 'MCP tool did not return JSON.') from None
    if len(json.dumps(value)) > 2_000_000:
        raise ProviderError('response_too_large', 'MCP tool result exceeds catalog size limit.')
    return value


@asynccontextmanager
async def connect(config, headers):
    # No credential forwarding across redirects, including transport-created clients.
    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout or 30, auth=auth, follow_redirects=False)
    async with streamablehttp_client(config.url, headers=headers, timeout=config.timeout_seconds,
                                     sse_read_timeout=config.timeout_seconds, httpx_client_factory=factory) as (read,write,_):
        async with ClientSession(read,write) as session:
            await session.initialize()
            yield session


class MCPCatalogProvider:
    def __init__(self, config, headers, connector=connect):
        self.config, self.headers, self.connector = config, headers, connector

    @classmethod
    def from_environment(cls):
        path = os.getenv('RELAY_MCP_CONFIG')
        if not path:
            raise ProviderError('not_configured', 'Set RELAY_MCP_CONFIG to your private MCP configuration file.')
        try:
            config = MCPConfig.model_validate_json(Path(path).read_text())
            headers = {key:os.environ[env] for key,env in config.header_env.items()}
            if any(not v for v in headers.values()):
                raise ValueError()
        except (OSError, ValueError, KeyError):
            raise ProviderError('invalid_configuration', 'Check MCP configuration and required secret environment variables.') from None
        return cls(config,headers)

    async def catalog(self, project: Project):
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self.connector(self.config,self.headers) as session:
                    return await self._catalog(session,project)
        except ProviderError:
            raise
        except TimeoutError:
            raise ProviderError('timeout', 'MCP catalog request timed out.') from None
        except ExceptionGroup as exc:
            def leaves(group):
                for error in group.exceptions:
                    if isinstance(error, BaseExceptionGroup):
                        yield from leaves(error)
                    else:
                        yield error
            errors = list(leaves(exc))
            known = next((e for e in errors if isinstance(e, ProviderError)), None)
            if known is not None:
                raise ProviderError(known.code, str(known)) from None
            if any(isinstance(e, TimeoutError) for e in errors):
                raise ProviderError('timeout', 'MCP catalog request timed out.') from None
            raise ProviderError('connection_failed', 'MCP connection failed; check URL, authentication and network access.') from None
        except Exception:
            # SDK task groups may wrap HTTP/auth/transport exceptions; never expose their raw text.
            raise ProviderError('connection_failed', 'MCP connection failed; check URL, authentication and network access.') from None

    async def _catalog(self, session, project):
        tools = {}; cursor = None; seen = set()
        for _ in range(50):
            listing = await session.list_tools(cursor=cursor)
            tools.update({t.name:t for t in listing.tools})
            cursor = listing.nextCursor
            if not cursor: break
            if cursor in seen: raise ProviderError('pagination_limit','MCP tool pagination repeated.')
            seen.add(cursor)
        else: raise ProviderError('pagination_limit','MCP tool list is incomplete.')
        bindings = [self.config.repository,self.config.branches,self.config.pipeline_file]
        for binding in bindings:
            if binding.tool not in tools:
                raise ProviderError('tool_missing','A configured catalog tool is not advertised by the MCP server.')
        workspace,repo = project.repository.split('/')
        context = dict(workspace=workspace,repo=repo,repository=project.repository,branch=project.default_branch,cursor=None)
        async def call(binding):
            arguments = render(binding.arguments,context,binding.omit_initial_cursor)
            from jsonschema import validate, ValidationError, SchemaError
            try: validate(arguments,tools[binding.tool].inputSchema)
            except (ValidationError,SchemaError):
                raise ProviderError('invalid_configuration','Configured arguments do not match the MCP tool schema.') from None
            return decode(await session.call_tool(binding.tool,arguments=arguments))
        repository = select(await call(self.config.repository),self.config.repository.result_path)
        if not isinstance(repository,dict) or not repository:
            raise ProviderError('invalid_response','Repository tool returned no repository metadata.')
        branches=[]; seen=set()
        for _ in range(50):
            data=await call(self.config.branches)
            names=select(data,self.config.branches.result_path)
            if not isinstance(names,list) or any(not isinstance(n,dict) or not isinstance(n.get('name'),str) for n in names):
                raise ProviderError('invalid_response','Branch tool must return objects containing name.')
            branches.extend(n['name'] for n in names)
            try:
                cursor=select(data,self.config.branches.cursor_path) if self.config.branches.cursor_path else None
            except ProviderError:
                if not self.config.branches.missing_cursor_is_end:
                    raise
                cursor=None
            if cursor is None or cursor == '': break
            if not isinstance(cursor,(str,int)) or str(cursor) in seen:
                raise ProviderError('pagination_limit','Invalid or repeated branch cursor.')
            seen.add(str(cursor));context['cursor']=cursor
        else: raise ProviderError('pagination_limit','Branch catalog is incomplete.')
        if project.default_branch not in branches:
            raise ProviderError('not_found','Configured branch is absent from MCP results.')
        raw=select(await call(self.config.pipeline_file),self.config.pipeline_file.result_path)
        if not isinstance(raw,str): raise ProviderError('invalid_response','Pipeline tool must return file text.')
        try:
            custom=yaml.safe_load(raw).get('pipelines',{}).get('custom',{})
            if not isinstance(custom,dict): raise ValueError()
        except (yaml.YAMLError,AttributeError,ValueError,RecursionError):
            raise ProviderError('invalid_pipeline_config','Invalid pipeline YAML from MCP.') from None
        return dict(provider=project.provider,transport='mcp',connected=True,repository=project.repository,
                    branch=project.default_branch,branches=[b for b in branches if b in project.allowed_branches],
                    pipelines=[dict(id=k,selector=p.selector,available=p.selector in custom,
                                    parameters={k:v.model_dump() for k,v in p.parameters.items()})
                               for k,p in project.pipelines.items()])
