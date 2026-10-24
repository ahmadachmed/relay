"""Bounded, credential-safe reads and cancellation of an existing pipeline UUID."""
import asyncio
from datetime import datetime
from urllib.parse import quote, urlsplit, unquote
from uuid import UUID

import httpx

from . import ProviderError
from .bitbucket import API, TIMEOUT, BitbucketCatalogProvider


RESULTS = {'SUCCESSFUL', 'FAILED', 'ERROR', 'STOPPED', 'EXPIRED', 'SKIPPED', 'NOT_RUN'}


def state_summary(state):
    if not isinstance(state, dict) or not isinstance(state.get('name'), str):
        raise ValueError()
    name = state['name']
    result = state.get('result', {}) or {}
    if not isinstance(result, dict):
        raise ValueError()
    result = result.get('name')
    result = result if result in RESULTS else None
    terminal = name == 'COMPLETED'
    status = {'PENDING': 'pending', 'IN_PROGRESS': 'running', 'PAUSED': 'pending',
              'HALTED': 'pending'}.get(name, 'unknown')
    if terminal:
        status = {'SUCCESSFUL': 'successful', 'FAILED': 'failed', 'ERROR': 'failed',
                  'EXPIRED': 'failed', 'STOPPED': 'stopped'}.get(result, 'unknown')
    return dict(status=status, remote_state=name if name in {
        'PENDING', 'IN_PROGRESS', 'PAUSED', 'HALTED', 'COMPLETED', 'READY', 'NOT_RUN'} else 'UNKNOWN',
        result=result, terminal=terminal)


def timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError()
    return datetime.fromisoformat(value.replace('Z', '+00:00')).isoformat()


class BitbucketRunProvider(BitbucketCatalogProvider):
    def pipeline_url(self, record):
        workspace, repo = record['plan']['project']['repository'].split('/')
        uuid = '{' + str(UUID(record['pipeline']['uuid'])) + '}'
        return f'{API}/{quote(workspace, safe="")}/{quote(repo, safe="")}/pipelines/{quote(uuid, safe="")}'

    async def status(self, record):
        try:
            url = self.pipeline_url(record)
            async with asyncio.timeout(TIMEOUT):
                async with httpx.AsyncClient(auth=self.auth, timeout=TIMEOUT,
                        follow_redirects=False, transport=self.transport) as client:
                    pipeline = await self._read(client, url, 'pipeline')
                    if UUID(pipeline['uuid']) != UUID(record['pipeline']['uuid']):
                        raise ValueError()
                    summary = state_summary(pipeline['state'])
                    summary.update(created_on=timestamp(pipeline.get('created_on')),
                                   completed_on=timestamp(pipeline.get('completed_on')))
                    steps_url = url + '/steps'
                    next_url = steps_url + '?pagelen=100'
                    seen, steps, ids = set(), [], set()
                    for _ in range(20):
                        parts = urlsplit(next_url)
                        if (parts.scheme != 'https' or parts.netloc != 'api.bitbucket.org'
                                or unquote(parts.path).rstrip('/') != unquote(urlsplit(steps_url).path)
                                or parts.fragment or next_url in seen):
                            raise ValueError()
                        seen.add(next_url)
                        page = await self._read(client, next_url, 'steps')
                        if not isinstance(page, dict) or not isinstance(page.get('values'), list):
                            raise ValueError()
                        for step in page['values']:
                            uuid = '{' + str(UUID(step['uuid'])) + '}'
                            if uuid in ids or len(steps) >= 1000:
                                raise ValueError()
                            ids.add(uuid)
                            name = step.get('name') or f'Step {len(steps) + 1}'
                            if not isinstance(name, str) or len(name) > 512:
                                raise ValueError()
                            steps.append(dict(id=uuid, name=name, **state_summary(step['state']),
                                started_on=timestamp(step.get('started_on')),
                                completed_on=timestamp(step.get('completed_on'))))
                        next_url = page.get('next')
                        if not next_url:
                            break
                        if not isinstance(next_url, str):
                            raise ValueError()
                    else:
                        raise ValueError()
            return dict(pipeline_status=summary.pop('status'), steps=steps, **summary)
        except ProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError('timeout', 'Remote monitoring request timed out.') from None
        except httpx.RequestError:
            raise ProviderError('connection_failed', 'Remote monitoring connection failed.') from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError('invalid_response', 'Invalid pipeline or step response.') from None

    async def cancel(self, record):
        try:
            url = self.pipeline_url(record) + '/stopPipeline'
            async with asyncio.timeout(TIMEOUT):
                async with httpx.AsyncClient(auth=self.auth, timeout=TIMEOUT,
                        follow_redirects=False, transport=self.transport) as client:
                    async with client.stream('POST', url) as response:
                        if response.status_code == 204:
                            return
                        if response.status_code in (400, 401, 403, 404, 409, 422, 429):
                            raise ProviderError('cancel_rejected',
                                f'Bitbucket rejected cancellation (HTTP {response.status_code}).')
                        raise ValueError()
        except ProviderError:
            raise
        except (TimeoutError, httpx.RequestError, ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError('cancel_unknown',
                'Cancellation outcome unknown; monitoring will verify the remote state.') from None
