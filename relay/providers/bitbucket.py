"""Bitbucket Cloud catalog and confirmed trigger; credentials stay in the backend."""
import asyncio
import json
import os
import re
from uuid import UUID
from urllib.parse import quote

import httpx
import yaml

from . import ProviderError

API = 'https://api.bitbucket.org/2.0/repositories'
MAX_BYTES = 2_000_000
TIMEOUT = 60


def _is_pipeline_declaration(value):
    if isinstance(value, list):
        return bool(value)
    # Discovery only: imported definitions are resolved by Bitbucket at runtime.
    return (isinstance(value, dict) and set(value) == {'import'}
            and isinstance(value['import'], str) and bool(value['import'].strip()))


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
                    or any(not isinstance(k, str) or not _is_pipeline_declaration(v)
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


    async def trigger(self, record):
        """One POST only. Any uncertain result must be reconciled, never retried."""
        plan = record['plan']
        workspace, repo = plan['project']['repository'].split('/')
        base = f'{API}/{quote(workspace, safe="")}/{quote(repo, safe="")}'
        payload = {
            'target': {'type': 'pipeline_ref_target', 'ref_type': 'branch',
                       'ref_name': plan['branch'],
                       'commit': {'type': 'commit', 'hash': record['commit']},
                       'selector': {'type': 'custom', 'pattern': record['selector']}},
            'variables': [{'key': k, 'value': v, 'secured': False}
                          for k, v in plan['parameters'].items()],
        }
        try:
            async with asyncio.timeout(TIMEOUT):
                async with httpx.AsyncClient(auth=self.auth, timeout=TIMEOUT,
                        follow_redirects=False, transport=self.transport) as client:
                    async with client.stream('POST', f'{base}/pipelines/', json=payload) as response:
                        if response.status_code in (400, 401, 403, 404, 422, 429):
                            raise ProviderError('trigger_rejected',
                                f'Bitbucket rejected trigger (HTTP {response.status_code}); review access and plan.')
                        if response.status_code != 201:
                            raise ValueError()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_BYTES:
                                raise ValueError()
                            body.extend(chunk)
            result = json.loads(body)
            uuid = '{' + str(UUID(result['uuid'])) + '}'
            number = result['build_number']
            if type(number) is not int or number < 1:
                raise ValueError()
            return {'uuid': uuid, 'url': f'https://bitbucket.org/{workspace}/{repo}/pipelines/results/{number}'}
        except ProviderError:
            raise
        except (TimeoutError, httpx.RequestError, ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError('trigger_unknown',
                'Trigger outcome unknown; inspect Bitbucket. This plan will not submit again.') from None


    async def reconcile(self, record, pipeline_uuid):
        """Read a manually identified pipeline; never submit a replacement."""
        workspace, repo = record['plan']['project']['repository'].split('/')
        uuid = '{' + str(UUID(pipeline_uuid)) + '}'
        base = f'{API}/{quote(workspace, safe="")}/{quote(repo, safe="")}'
        try:
            async with asyncio.timeout(TIMEOUT):
                async with httpx.AsyncClient(auth=self.auth, timeout=TIMEOUT,
                        follow_redirects=False, transport=self.transport) as client:
                    result = await self._read(client, f'{base}/pipelines/{quote(uuid, safe="")}', 'pipeline')
            target = result['target']
            if (str(UUID(result['uuid'])) != str(UUID(uuid))
                    or target['ref_name'] != record['plan']['branch']
                    or target['ref_type'] != 'branch'
                    or target['commit']['hash'] != record['commit']
                    or target['selector'] != {'type': 'custom', 'pattern': record['selector']}
                    or type(result['build_number']) is not int or result['build_number'] < 1):
                raise ValueError()
            return {'uuid': uuid,
                    'url': f"https://bitbucket.org/{workspace}/{repo}/pipelines/results/{result['build_number']}"}
        except ProviderError:
            raise
        except (TimeoutError, httpx.RequestError, ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError('reconciliation_failed',
                'Could not verify the pipeline identity and target; plan remains unchanged.') from None
