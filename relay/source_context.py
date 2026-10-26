"""Read-only Bitbucket file preview at a saved run's immutable commit."""
import asyncio
import hashlib
import json
import os
import re
from pathlib import PurePosixPath
from urllib.parse import quote
import httpx
from .execution import PlanError
from .contracts import normalize_source_directory, sensitive_source_component
from .providers.bitbucket import BitbucketCatalogProvider, API
from .assistant import redact

MAX_BYTES = 24000


def safe_path(path):
    parts = path.split('/')
    suffix = PurePosixPath(path).suffix.lower()
    if (not path or len(path) > 300 or any(p in ('', '.', '..') or p.startswith('.') for p in parts)
        or not re.fullmatch(r'[A-Za-z0-9_./-]+', path)
        or suffix not in ('.py', '.robot', '.resource', '.yaml', '.yml', '.json', '.toml', '.md')
        or any(sensitive_source_component(p) for p in parts)):
        raise PlanError('source_path_denied', 'Source path is not an approved text-code path.', 422)


class SourceContext:
    def __init__(self, execution, monitor, registry, transport=None):
        self.execution, self.monitor, self.registry = execution, monitor, registry
        self.transport = transport
        self.lock = asyncio.Lock()

    async def preview(self, run_id, path):
        if os.getenv('RELAY_SOURCE_ENABLED') != '1':
            raise PlanError('source_disabled', 'Source preview is disabled in the backend.', 503)
        safe_path(path)
        self.monitor.get(run_id)
        saved = self.execution.get(run_id)
        plan = saved['plan']
        project = self.registry.projects.get(plan['project_id'])
        repository = plan['project']['repository']
        commit = saved['commit']
        directory_allowed = bool(project) and any(path.startswith(normalize_source_directory(directory)) for directory in project.source_directories)
        if (not project or not project.enabled or project.provider != 'bitbucket'
            or plan['project']['provider'] != 'bitbucket' or project.repository != repository
            or not (path in project.source_files or directory_allowed)):
            raise PlanError('source_not_allowed', 'Repository or file is not currently approved for this run.', 403)
        if not re.fullmatch(r'[0-9a-fA-F]{40}', commit or ''):
            raise PlanError('source_missing_commit', 'Saved run has no valid immutable commit.', 422)
        workspace, repo = repository.split('/')
        if workspace in ('.','..') or repo in ('.','..'):
            raise PlanError('source_not_allowed', 'Invalid repository identity.', 403)
        if self.lock.locked():
            raise PlanError('source_busy', 'Another source request is active.', 429)
        async with self.lock:
            provider = BitbucketCatalogProvider.from_environment()
            url = f'{API}/{quote(workspace,safe="")}/{quote(repo,safe="")}/src/{commit}/{quote(path,safe="/")}'
            try:
                async with asyncio.timeout(20):
                    async with httpx.AsyncClient(auth=provider.auth, timeout=15, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                        meta = json.loads(await self._read(client, url+'?format=meta', 6000))
                        if (not isinstance(meta, dict) or meta.get('type') != 'commit_file' or meta.get('path') != path
                            or meta.get('commit', {}).get('hash') != commit or type(meta.get('size')) is not int
                            or not 0 <= meta['size'] <= MAX_BYTES or not isinstance(meta.get('attributes'), list)
                            or any(attr not in ('executable',) for attr in meta['attributes'])):
                            raise PlanError('source_invalid_metadata', 'Source is not a verified bounded regular text file at the saved commit.', 502)
                        body = await self._read(client, url, MAX_BYTES)
                        if len(body) != meta['size']:
                            raise ValueError('Size mismatch')
                        text = body.decode('utf-8')
                        if '\x00' in text or 'PRIVATE KEY-----' in text:
                            raise ValueError('Excluded content')
            except (TimeoutError, httpx.TimeoutException):
                raise PlanError('source_timeout', 'Source request timed out; no alternate commit was used.', 504) from None
            except (httpx.HTTPError, ValueError, TypeError, AttributeError, RecursionError):
                raise PlanError('source_unavailable', 'Source is unavailable or invalid; no alternate commit was used.', 502) from None
            digest = hashlib.sha256(body).hexdigest()
            return {'run_id':run_id,'project_id':plan['project_id'],'repository':repository,'commit':commit,'path':path,
                    'sha256':digest,'source_id':hashlib.sha256(f'{repository}:{commit}:{path}:{digest}'.encode()).hexdigest(),
                    'text':redact(text),'redacted':True,'limitations':['Read-only preview; not sent to AI or executed. Redaction is best-effort.']}

    async def _read(self, client, url, limit):
        async with client.stream('GET',url) as response:
            if response.status_code != 200:
                raise PlanError('source_remote_error', f'Bitbucket source read rejected (HTTP {response.status_code}); verify access, commit and file.', 502)
            body=bytearray()
            async for chunk in response.aiter_bytes():
                if len(body)+len(chunk)>limit:
                    raise PlanError('source_too_large','Source response exceeds its byte budget.',413)
                body.extend(chunk)
        return bytes(body)
