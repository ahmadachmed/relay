"""Read-only Bitbucket Cloud catalog over REST; credentials stay in the backend."""
import asyncio
import json
import os
import re
from urllib.parse import quote

import httpx
import yaml

from . import ProviderError

API = 'https://api.bitbucket.org/2.0/repositories'
MAX_BYTES = 2_000_000
TIMEOUT = 60


class BitbucketCatalogProvider:
    def __init__(self, email, token, transport=None):
        self.auth = httpx.BasicAuth(email, token)
        self.transport = transport

    @classmethod
    def from_environment(cls):
        email = os.getenv('RELAY_BITBUCKET_EMAIL', '')
        token = os.getenv('RELAY_BITBUCKET_API_TOKEN', '')
        if not email or not token:
            raise ProviderError('not_configured',
                'Set RELAY_BITBUCKET_EMAIL and RELAY_BITBUCKET_API_TOKEN in the backend environment.')
        if ':' in email or any(c in email + token for c in '\r\n'):
            raise ProviderError('invalid_configuration', 'Invalid Bitbucket credentials.')
        return cls(email, token)

    async def _read(self, client, url, stage, as_json=True):
        async with client.stream('GET', url) as response:
            if response.status_code != 200:
                code, message = {
                    401: ('unauthorized', 'Bitbucket authentication failed.'),
                    403: ('forbidden', 'Bitbucket access denied; check scopes and repository access.'),
                    404: ('not_found', 'Bitbucket repository, branch or file is unavailable.'),
                    429: ('rate_limited', 'Bitbucket rate limit reached; retry later.'),
                }.get(response.status_code, ('upstream_error', 'Bitbucket request failed.'))
                raise ProviderError(code, f'{stage}: {message}')
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_BYTES:
                    raise ProviderError('response_too_large', f'{stage}: Bitbucket response exceeds 2 MB.')
                body.extend(chunk)
        try:
            text = body.decode('utf-8-sig')
            return json.loads(text) if as_json else text
        except (UnicodeError, ValueError):
            raise ProviderError('invalid_response', f'{stage}: Invalid Bitbucket response encoding or JSON.') from None

    async def catalog(self, project):
        try:
            async with asyncio.timeout(TIMEOUT):
                async with httpx.AsyncClient(auth=self.auth, timeout=TIMEOUT,
                        follow_redirects=False, transport=self.transport) as client:
                    return await self._catalog(client, project)
        except ProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError('timeout', 'Bitbucket catalog request timed out.') from None
        except httpx.RequestError:
            raise ProviderError('connection_failed', 'Bitbucket connection failed; check network access.') from None

    async def _catalog(self, client, project):
        workspace, repo = project.repository.split('/')
        if workspace in ('.', '..') or repo in ('.', '..') or project.default_branch in ('.', '..'):
            raise ProviderError('invalid_configuration', 'Invalid repository or branch path.')
        base = f'{API}/{quote(workspace, safe="")}/{quote(repo, safe="")}'
        repository = await self._read(client, base, 'repository')
        if (not isinstance(repository, dict)
                or not isinstance(repository.get('full_name'), str)
                or repository['full_name'].lower() != project.repository.lower()):
            raise ProviderError('invalid_response', 'repository: Repository identity does not match configuration.')
        branch = await self._read(client,
            f'{base}/refs/branches/{quote(project.default_branch, safe="")}', 'branch')
        target = branch.get('target') if isinstance(branch, dict) else None
        commit = target.get('hash') if isinstance(target, dict) else None
        if (not isinstance(branch, dict) or branch.get('name') != project.default_branch
                or not isinstance(commit, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', commit)):
            raise ProviderError('invalid_response', 'branch: Invalid branch identity or commit hash.')
        raw = await self._read(client, f'{base}/src/{commit}/bitbucket-pipelines.yml',
                               'pipeline_file', as_json=False)
        try:
            document = yaml.safe_load(raw)
            pipelines = document.get('pipelines') if isinstance(document, dict) else None
            if not isinstance(pipelines, dict) or not pipelines:
                raise ValueError()
            custom = pipelines.get('custom', {})
            if (not isinstance(custom, dict)
                    or any(not isinstance(k, str) or not isinstance(v, list) or not v
                           for k, v in custom.items())):
                raise ValueError()
        except (yaml.YAMLError, ValueError, RecursionError):
            raise ProviderError('invalid_pipeline_config', 'pipeline_file: Invalid Bitbucket pipeline YAML.') from None
        return dict(provider=project.provider, transport='rest', connected=True,
            repository=project.repository, branch=project.default_branch, commit=commit,
            branch_scope='default_only', branches=[project.default_branch],
            pipelines=[dict(id=key, selector=p.selector, available=p.selector in custom,
                parameters={name: spec.model_dump() for name, spec in p.parameters.items()})
                for key, p in project.pipelines.items()])
