"""Request-scoped reference retrieval. No execution or authorization writes.

Original bytes live only in a request. Durable manifests contain redacted metadata
and sent-content hashes, never raw text. All operations recheck current config.
"""
import asyncio
import hashlib
import json
import os
import posixpath
from copy import deepcopy
from pathlib import PurePosixPath
import re
import sqlite3
import time
from uuid import uuid4
from urllib.parse import quote, urlsplit, unquote

import httpx
import yaml
from pydantic import Field

from .contracts import Contract, SutId, sensitive_source_component
from .execution import PlanError
from .registry import resolve_plan
from .source_registry import SourceRegistry
from .source_documents import read_local, parse_document, parse_data, _extract_operation
from .providers.bitbucket import BitbucketCatalogProvider, API
from .providers import ProviderError

MAX_SHARED = 20_000
MAX_CALL_BYTES = 100_000
METHODS = {'get', 'put', 'post', 'patch', 'delete', 'head', 'options', 'trace'}
SUFFIXES = {'.md', '.txt', '.json', '.yaml', '.yml', '.py', '.robot', '.resource', '.toml'}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')


class SourceOperation(Contract):
    operation: str = Field(pattern='^(search_sources|read_source_section)$')
    query: str | None = Field(default=None, min_length=1, max_length=1000)
    handle: str | None = Field(default=None, pattern='^[a-f0-9]{32}$')


def _open_directory(root):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.split('/')[1:]:
            if not part:
                continue
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def discover(source):
    """Bounded directory discovery using directory descriptors, never symlinks."""
    files = list(source.files)
    if not source.directories:
        return files
    count = 0
    root = _open_directory(source.location)
    def walk(fd, prefix, depth):
        nonlocal count
        if depth > 4:
            return
        # Do not materialize an unbounded directory listing.
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > 256:
                    return
                name = entry.name
                if (name.startswith('.') or sensitive_source_component(name)
                        or not re.fullmatch(r'[A-Za-z0-9_.-]+', name)):
                    continue
                path = prefix + name
                if entry.is_file(follow_symlinks=False) and PurePosixPath(name).suffix.lower() in SUFFIXES:
                    files.append(path)
                elif entry.is_dir(follow_symlinks=False):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        walk(child, path + '/', depth + 1)
                    finally:
                        os.close(child)
    try:
        for directory in source.directories:
            fd = os.dup(root)
            try:
                for part in directory.rstrip('/').split('/'):
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                walk(fd, directory, 0)
            finally:
                os.close(fd)
    finally:
        os.close(root)
    return sorted(set(files))[:128]


class SourceService:
    def __init__(self, projects, execution, monitor, transport=None):
        self.projects, self.execution, self.monitor = projects, execution, monitor
        self.transport = transport
        self.path = execution.path.parent / 'source-manifests.sqlite3'
        self.path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS source_manifests (id TEXT PRIMARY KEY, created REAL, record TEXT)')

    def registry(self):
        if os.getenv('RELAY_SOURCE_CONTEXT_ENABLED') != '1':
            raise PlanError('source_context_disabled', 'Original source context is disabled.', 503)
        filename = os.getenv('RELAY_SOURCES')
        if not filename:
            return SourceRegistry()
        try:
            with open(filename, 'rb') as stream:
                body = stream.read(1_000_001)
            if len(body) > 1_000_000:
                raise ValueError()
            return SourceRegistry.model_validate_json(body)
        except (OSError, ValueError):
            raise PlanError('source_configuration', 'Source configuration is unavailable or invalid.', 503) from None

    def catalog(self):
        registry = self.registry()
        return {'enabled': True, 'sources': [
            {'id': key, 'category': s.category, 'scope_id': s.scope_id, 'environments': s.environments,
             'provider': s.provider, 'format': s.format, 'revision': s.revision or 'current request snapshot'}
            for key, s in sorted(registry.sources.items()) if s.enabled]}

    def identity(self, request):
        if request.run_id:
            self.monitor.get(request.run_id)
            saved = self.execution.get(request.run_id)
            plan = saved['plan']
            project = self.projects.projects.get(plan['project_id'])
            if (not project or not project.enabled or project.repository != plan['project']['repository']
                    or project.provider != plan['project']['provider']):
                raise PlanError('source_identity', 'Run project is disabled or repository mapping changed.', 403)
            specs = plan['project']['pipelines'][plan['pipeline_id']]['parameters']
            env = [plan['parameters'].get(k) for k, v in specs.items() if v['kind'] == 'environment']
            identity = {'mode': 'run', 'run_id': request.run_id, 'project_id': plan['project_id'],
                        'sut_id': plan.get('sut_id'), 'branch': plan['branch'], 'commit': saved.get('commit'),
                        'repository': plan['project']['repository'], 'environment': env[0] if len(env) == 1 else None}
        else:
            if request.context is None:
                raise PlanError('source_identity', 'Select a project, pipeline, environment and optional SUT for planning.', 422)
            try:
                plan = resolve_plan(self.projects, request.context)
            except ValueError:
                raise PlanError('source_identity', 'Planning selection is invalid.', 422) from None
            specs = plan.project.pipelines[plan.pipeline_id].parameters
            # Environment must be explicitly selected, not inferred from defaults/tags.
            keys = [k for k, v in specs.items() if v.kind == 'environment']
            environment = request.context.parameters.get(keys[0]) if len(keys) == 1 else None
            identity = {'mode': 'planning', 'run_id': None, 'project_id': plan.project_id,
                        'sut_id': plan.sut_id, 'branch': plan.branch, 'commit': None,
                        'repository': plan.project.repository, 'environment': environment}
        if not identity['environment']:
            raise PlanError('source_environment', 'Select one explicit, unambiguous environment.', 422)
        return identity

    async def start(self, request):
        if not request.include_source_context:
            return None
        if not request.source_ids or len(set(request.source_ids)) != len(request.source_ids):
            raise PlanError('source_selection', 'Select unique original source IDs to share.', 422)
        identity = self.identity(request)
        session = SourceSession(self, identity, request.source_ids)
        async with asyncio.timeout_at(session.deadline):
            await session.load()
        return session

    def save(self, identity, shared, diagnostics):
        response_id = uuid4().hex
        record = {'id': response_id, 'identity': identity, 'sources': list(shared.values()),
                  'diagnostics': sorted(set(diagnostics)), 'retention_days': 30,
                  'replay': 'Metadata only; source bytes are not archived.'}
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM source_manifests WHERE created < ?', (time.time() - 30 * 86400,))
            db.execute('INSERT INTO source_manifests VALUES (?,?,?)', (response_id, time.time(), json.dumps(record)))
            db.execute('DELETE FROM source_manifests WHERE id NOT IN (SELECT id FROM source_manifests ORDER BY created DESC LIMIT 200)')
        return record

    async def manifest(self, response_id):
        if not re.fullmatch('[a-f0-9]{32}', response_id):
            raise PlanError('source_manifest', 'Unknown source manifest.', 404)
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT record FROM source_manifests WHERE id=? AND created>=?', (response_id, time.time()-30*86400)).fetchone()
        if not row:
            raise PlanError('source_manifest', 'Source manifest unavailable or expired.', 404)
        record = json.loads(row[0])
        registry = self.registry()
        for item in record['sources']:
            item['freshness'] = 'unavailable'
            try:
                source = self.authorize(registry, item['source_id'], record['identity'])
                # Reconfiguration changes the source identity even at the same path.
                if item.get('config_sha256') != self.fingerprint(source):
                    item['freshness'] = 'configuration_changed'
                    continue
                if source.provider == 'local':
                    files = await asyncio.to_thread(discover, source)
                    source.files = files
                    snapshot = await asyncio.to_thread(read_local, source, item['path'])
                    item['freshness'] = 'unchanged' if snapshot['sha256'] == item['sha256'] else 'changed'
                    for dependency in item.get('dependencies', []):
                        dep = await asyncio.to_thread(read_local, source, dependency['path'])
                        if dep['sha256'] != dependency['sha256']:
                            item['freshness'] = 'changed'
                else:
                    item['freshness'] = 'pinned_commit_not_rechecked'
            except (PlanError, OSError, ValueError):
                pass
        from .assistant import redact_source_metadata
        return redact_source_metadata(record)

    @staticmethod
    def fingerprint(source):
        return digest(encoded({'config': source.model_dump(), 'location': source.location}))

    def authorize(self, registry, source_id, identity):
        source = registry.sources.get(source_id)
        if source is None:
            raise PlanError('source_denied', 'Selected source is not available.', 403)
        scope = identity['project_id'] if source.category == 'automation' else identity['sut_id']
        try:
            source = registry.authorize(source_id, source.category, scope, identity['environment'])
        except ValueError:
            raise PlanError('source_denied', 'Source is outside the selected category, scope or environment.', 403) from None
        project = self.projects.projects.get(identity['project_id'])
        if not project or not project.enabled or project.repository != identity['repository']:
            raise PlanError('source_denied', 'Project configuration changed or is disabled.', 403)
        if source.provider == 'bitbucket' and source.category == 'automation' and (source.location != project.repository or project.provider != 'bitbucket'):
            raise PlanError('source_denied', 'Automation source repository does not match the selected project.', 403)
        return source


class SourceSession:
    def __init__(self, service, identity, source_ids):
        self.service, self.identity, self.source_ids = service, identity, source_ids
        self.id = uuid4().hex
        self.limits = service.registry().limits
        self.deadline = asyncio.get_running_loop().time() + self.limits.wall_seconds
        self.sections, self.configs, self.snapshots, self.documents = {}, {}, {}, {}
        self.diagnostics, self.shared = [], {}
        self.bytes_read, self.files_read = 0, 0

    def current(self, source_id):
        source = self.service.authorize(self.service.registry(), source_id, self.identity)
        if self.service.fingerprint(source) != self.configs[source_id]:
            raise PlanError('source_changed', 'Source authorization changed; start a new request.', 409)
        return source

    async def _remote(self, source, path, commit, remaining):
        provider = BitbucketCatalogProvider.from_environment()
        workspace, repo = source.location.split('/')
        base = f'{API}/{quote(workspace,safe="")}/{quote(repo,safe="")}'
        async def read(client, url, limit):
            async with client.stream('GET', url) as response:
                if response.status_code != 200:
                    raise ValueError('Remote read failed')
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    self.bytes_read += len(chunk)
                    if len(body) + len(chunk) > limit or self.bytes_read > self.limits.read_bytes:
                        raise ValueError('Remote read limit')
                    body.extend(chunk)
                return bytes(body)
        async with httpx.AsyncClient(auth=provider.auth, transport=self.service.transport or provider.transport,
                                     timeout=10, follow_redirects=False, trust_env=False) as client:
            if not commit:
                if source.category == 'sut':
                    commit = source.revision
                    if not re.fullmatch('[a-fA-F0-9]{40}', commit or ''):
                        raise ValueError('SUT source requires a pinned commit')
                else:
                    branch = self.identity['branch']
                    meta = json.loads(await read(client, base + '/refs/branches/' + quote(branch,safe=''), 6000))
                    commit = meta.get('target', {}).get('hash')
                    if meta.get('name') != branch or not re.fullmatch('[a-fA-F0-9]{40}', commit or ''):
                        raise ValueError('Branch identity unavailable')
                    self.identity['commit'] = commit
            if not re.fullmatch('[a-fA-F0-9]{40}', commit or ''):
                raise ValueError('Immutable commit missing')
            url = f'{base}/src/{commit}/{quote(path,safe="/")}'
            meta = json.loads(await read(client, url+'?format=meta', min(6000, self.limits.read_bytes-self.bytes_read)))
            size = meta.get('size')
            if (meta.get('type') != 'commit_file' or meta.get('path') != path or meta.get('commit',{}).get('hash') != commit
                    or type(size) is not int or not 0 <= size <= min(remaining, self.limits.read_bytes-self.bytes_read)
                    or not isinstance(meta.get('attributes'), list) or any(a != 'executable' for a in meta['attributes'])):
                raise ValueError('Remote file metadata invalid')
            body = await read(client, url, min(remaining, self.limits.read_bytes-self.bytes_read))
            if len(body) != size:
                raise ValueError('Remote file changed')
            text = body.decode('utf-8')
            if '\x00' in text or 'PRIVATE KEY-----' in text:
                raise ValueError('Excluded content')
            return {'path':path, 'sha256':digest(body), 'text':text, 'commit':commit, 'repository':source.location}

    async def load(self):
        registry = self.service.registry()
        for key in self.source_ids:
            source = self.service.authorize(registry, key, self.identity)
            self.configs[key] = self.service.fingerprint(source)
            if source.category == 'automation' and source.provider == 'local' and self.identity['mode'] == 'run':
                self.diagnostics.append(f'{key}: local automation omitted; saved-run code requires the recorded repository commit.')
                continue
            try:
                files = await asyncio.to_thread(discover, source) if source.provider == 'local' else source.files
            except (OSError, ValueError):
                self.diagnostics.append(f'{key}: directory unavailable.')
                continue
            if source.directories:
                self.diagnostics.append(f'{key}: directory discovery is bounded to depth 4, 256 entries and 128 files; additional coverage may be omitted.')
            source.files = files
            for path in files:
                if self.files_read >= self.limits.files or self.bytes_read >= self.limits.read_bytes:
                    self.diagnostics.append('Source discovery/read budget reached; coverage is incomplete.')
                    break
                self.files_read += 1
                if PurePosixPath(path).suffix.lower() not in SUFFIXES:
                    self.diagnostics.append(f'{key}: unsupported file type omitted.')
                    continue
                try:
                    remaining = min(source.max_file_bytes, self.limits.read_bytes - self.bytes_read)
                    if source.provider == 'local':
                        def count(size):
                            self.bytes_read += size
                        snapshot = await asyncio.to_thread(read_local, source, path, remaining, count)
                    else:
                        # Saved automation commit is mandatory; never resolve HEAD on a run.
                        commit = self.identity['commit'] if source.category == 'automation' else source.revision
                        if self.identity['mode'] == 'run' and source.category == 'automation' and not re.fullmatch('[a-fA-F0-9]{40}', commit or ''):
                            raise ValueError('Saved automation commit missing')
                        snapshot = await self._remote(source, path, commit, remaining)
                    self.snapshots[(key,path)] = snapshot
                    if source.format == 'openapi':
                        document = await asyncio.to_thread(parse_document, snapshot['text'])
                        self.documents[(key,path)] = document
                        if not isinstance(document.get('paths', {}), dict):
                            raise ValueError('Invalid paths')
                        for endpoint, item in document.get('paths',{}).items():
                            if not isinstance(endpoint,str) or not endpoint.startswith('/'):
                                self.diagnostics.append(f'{key}: invalid path key omitted.')
                                continue
                            if isinstance(item, dict) and '$ref' in item:
                                self.diagnostics.append(f'{key}: referenced path items are unsupported and omitted.')
                                continue
                            if not isinstance(item, dict):
                                continue
                            for method in METHODS & item.keys():
                                operation = item[method]
                                if not isinstance(operation, dict):
                                    continue
                                selector = f'{method.upper()} {endpoint}'
                                search = selector+' '+str(operation.get('operationId',''))+' '+str(operation.get('tags',[]))+' '+str(operation.get('summary',''))+' '+str(operation.get('description',''))
                                self.add(key, snapshot, selector, search, None, (method,endpoint))
                    else:
                        lines = snapshot['text'].splitlines(keepends=True)
                        # Stable bounded line sections; headings are retained as ordinary text.
                        for first in range(0,len(lines),40):
                            text = ''.join(lines[first:first+40])
                            selector = f'lines {first+1}-{min(first+40,len(lines))}'
                            self.add(key,snapshot,selector,path+' '+text,text,None)
                except (ValueError, OSError, TypeError, AttributeError, RecursionError, yaml.YAMLError, ProviderError, httpx.HTTPError):
                    self.diagnostics.append(f'{key}: source unavailable, unsupported or exceeds limits; contract coverage may be incomplete.')
        if len(self.sections) >= self.limits.sections:
            self.diagnostics.append('Section index limit reached; coverage is incomplete.')

    def add(self, key, snapshot, selector, search, text, operation):
        if len(self.sections) >= self.limits.sections:
            return
        handle = uuid4().hex
        self.sections[handle] = {'handle':handle, 'source_id':key, 'path':snapshot['path'], 'sha256':snapshot['sha256'],
            'selector':selector, 'commit':snapshot.get('commit'), 'repository':snapshot.get('repository'),
            'parser_version':'openapi-operation-v1' if operation else 'text-lines-v1', 'search':search,
            'text':text, 'operation':operation}

    def search(self, query):
        terms = set(re.findall(r'\w+', query.casefold()))
        results = []
        for key in self.source_ids:
            self.current(key)
        explicit = re.search(r'\b(get|put|post|patch|delete|head|options|trace)\s+(/[^\s]*)', query, re.I)
        selected_operation = None
        if explicit:
            method, path = explicit[1].lower(), explicit[2]
            available = {tuple(s['operation']) for s in self.sections.values() if s['operation']}
            for _ in range(16):
                if (method, path) in available:
                    selected_operation = (method, path)
                    break
                if not path or path[-1] not in '`.,;!?)]}':
                    break
                path = path[:-1]
        for handle, item in self.sections.items():
            words = set(re.findall(r'\w+', item['search'].casefold()))
            score = len(terms & words)
            if item['operation']:
                method,path = item['operation']
                if explicit:
                    if (method,path) != selected_operation:
                        continue
                    score += 100
            if score:
                results.append((score, item['source_id'], item['path'], item['selector'], handle))
        results.sort(key=lambda x:(-x[0],*x[1:4]))
        # No raw snippets in discovery: only authorized relative path/selector metadata.
        return [{'handle': h, **{k:self.sections[h][k] for k in ('source_id','path','selector','sha256','commit','parser_version')}} for *_,h in results[:self.limits.candidates]]

    def read(self, handle):
        if handle not in self.sections:
            raise PlanError('source_handle', 'Unknown, expired or cross-snapshot source handle.', 422)
        item = self.sections[handle]
        source = self.current(item['source_id'])
        snapshot = self.snapshots[(item['source_id'],item['path'])]
        if item['operation']:
            method,path = item['operation']
            try:
                document, dependencies = self.operation_document(item['source_id'],item['path'],method,path)
                text = _extract_operation(snapshot,method,path,self.limits.context_bytes,document)['text']
            except (ValueError, TypeError, KeyError, IndexError, UnicodeError, AttributeError, RecursionError, yaml.YAMLError):
                self.diagnostics.append(f"{item['source_id']}: required operation dependencies unavailable or over budget; no partial contract sent.")
                return None
        else:
            dependencies = []
            text = item['text']
        # config hash is private provenance, not a filesystem location.
        result = {k:item[k] for k in ('handle','source_id','path','sha256','selector','commit','repository','parser_version')}
        result.update(category=source.category,scope_id=source.scope_id,text=text,
                      config_sha256=self.configs[item['source_id']], snapshot_id=self.id, dependencies=dependencies,
                      dependency_candidates=self.code_dependencies(item) if not item['operation'] else [])
        return result


    def operation_document(self, source_id, path, method, endpoint):
        """Rewrite only authorized same-source refs to internal pointers in a private copy.

        No fetch is triggered by a ref. Unregistered or budget-omitted files fail closed.
        Original source hashes and referenced file hashes remain separate provenance.
        """
        root = deepcopy(self.documents[(source_id,path)])
        if 'x-relay-external' in root:
            raise ValueError('Reserved bundling key')
        external, dependencies, loaded = {}, [], {path: ''}
        count = 0
        visited = set()
        origin_documents = {path:root}
        def pointer(ref, origin):
            if not isinstance(ref,str):
                raise ValueError('Invalid ref')
            uri = urlsplit(ref)
            if uri.scheme or uri.netloc or uri.query or (uri.path and uri.path.startswith('/')):
                raise ValueError('External network or absolute reference denied')
            fragment = unquote(uri.fragment,errors='strict')
            if fragment and not fragment.startswith('/'):
                raise ValueError('Unsupported anchor')
            if not uri.path:
                target = origin
                prefix = loaded[origin]
            else:
                target = posixpath.normpath(posixpath.join(posixpath.dirname(origin),unquote(uri.path,errors='strict')))
                if target.startswith('../') or target in ('.','..') or (source_id,target) not in self.snapshots:
                    raise ValueError('Unregistered or unavailable reference file')
                if target not in loaded:
                    label = str(len(loaded))
                    loaded[target] = '/x-relay-external/'+label
                    snap = self.snapshots[(source_id,target)]
                    document = parse_data(snap['text'])
                    dependencies.append({'source_id':source_id,'path':target,'sha256':snap['sha256'],'commit':snap.get('commit')})
                    external[label] = document
                    origin_documents[target] = document
                prefix = loaded[target]
            value = origin_documents[target]
            for part in fragment[1:].split('/') if fragment else []:
                if re.search(r'~(?![01])',part):
                    raise ValueError('Invalid JSON pointer')
                part = part.replace('~1','/').replace('~0','~')
                if isinstance(value,list):
                    if not re.fullmatch('0|[1-9][0-9]*',part):
                        raise ValueError('Invalid JSON pointer index')
                    value = value[int(part)]
                else:
                    value = value[part]
            walk(value,target)
            return '#'+prefix+fragment
        def walk(value, origin):
            nonlocal count
            identity = (origin,id(value))
            if isinstance(value,(dict,list)):
                if identity in visited:
                    return
                visited.add(identity)
            count += 1
            if count > 30000:
                raise ValueError('Cross-file dependency traversal limit')
            if isinstance(value,dict):
                if any(k in value for k in ('$id','$dynamicRef','$recursiveRef')):
                    raise ValueError('Schema base/dynamic refs require unsupported resolution')
                if '$ref' in value:
                    value['$ref'] = pointer(value['$ref'],origin)
                for child in value.values():
                    walk(child,origin)
            elif isinstance(value,list):
                for child in value:
                    walk(child,origin)
        item = root['paths'][endpoint]
        operation = item[method]
        for value in (operation, item.get('parameters', []),
                      operation.get('security',root.get('security',[])),
                      root.get('securityDefinitions',root.get('components',{}).get('securitySchemes',{})),
                      operation.get('servers',item.get('servers',root.get('servers',[])))):
            walk(value,path)
        if external:
            root['x-relay-external'] = external
        return root, dependencies

    def code_dependencies(self, item):
        if not item['path'].endswith(('.py','.robot','.resource')):
            return []
        snapshot = self.snapshots[(item['source_id'],item['path'])]
        references=set()
        # Supported static forms only; no dynamic imports, variables or execution.
        if item['path'].endswith('.py'):
            for module in re.findall(r'(?m)^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))',snapshot['text']):
                name=next(value for value in module if value)
                references.add(name.replace('.','/')+'.py')
        else:
            for target in re.findall(r'(?im)^\s*Resource(?:\t+| {2,})([^\s]+)',snapshot['text']):
                references.add(posixpath.normpath(posixpath.join(posixpath.dirname(item['path']),target)))
        return [{'handle':handle,'path':section['path'],'selector':section['selector']} for handle,section in self.sections.items()
                if section['source_id']==item['source_id'] and section['path'] in references][:8]
