"""Optional repository Downloads transport; distinct from pipeline UI artifacts."""
import asyncio
import os
import re
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx

from . import ProviderError
from .bitbucket import API, TIMEOUT, BitbucketCatalogProvider
from ..report_evidence import MAX_REPORT_BYTES


class BitbucketEvidenceProvider(BitbucketCatalogProvider):
    def __init__(self, email, token, transport=None, download_hosts=None):
        super().__init__(email, token, transport)
        self.download_hosts = set(download_hosts if download_hosts is not None else
            filter(None, os.getenv('RELAY_EVIDENCE_DOWNLOAD_HOSTS', '').split(',')))
        for host in self.download_hosts:
            # Exact operator-approved DNS hostnames; never wildcards or IP literals.
            if (not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)
                    or '.' not in host or re.fullmatch(r'[0-9.]+', host)):
                raise ProviderError('invalid_configuration', 'Invalid evidence download hostname configuration.')

    async def fetch_report(self, record, spec):
        name = spec.get('download_name')
        if not name:
            raise ProviderError('source_not_configured', 'Report has no repository Downloads mapping.')
        uuid = UUID(record['pipeline']['uuid']).hex
        filename = name.replace('{pipeline_uuid}', uuid).replace('{commit}', record['commit'])
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', filename) or len(filename) > 240 or uuid not in filename:
            raise ProviderError('invalid_configuration', 'Invalid report Downloads mapping.')
        workspace, repo = record['plan']['project']['repository'].split('/')
        url = f'{API}/{quote(workspace, safe="")}/{quote(repo, safe="")}/downloads/{quote(filename, safe="")}'
        try:
            async with asyncio.timeout(TIMEOUT):
                # Authentication is applied only to the fixed Bitbucket endpoint below.
                async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                        transport=self.transport, trust_env=False) as client:
                    async with client.stream('GET', url, auth=self.auth) as response:
                        if response.status_code in (302, 307):
                            destination = response.headers.get('location', '')
                        else:
                            return await self._body(response)
                    parts = urlsplit(destination)
                    if (parts.scheme != 'https' or parts.hostname not in self.download_hosts
                            or parts.username or parts.password or parts.fragment
                            or parts.port not in (None, 443)):
                        raise ProviderError('download_host_not_allowed',
                            'Download redirect host is not approved; configure the exact storage hostname.')
                    # Signed URLs stay in memory; no credential/header inheritance and no second redirect.
                    async with client.stream('GET', destination) as response:
                        return await self._body(response)
        except ProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError('timeout', 'Evidence download timed out.') from None
        except (httpx.RequestError, ValueError):
            raise ProviderError('download_failed', 'Evidence download failed.') from None

    async def _body(self, response):
        code = response.status_code
        if code != 200:
            message = {404: ('missing_evidence', 'Published report is unavailable.'),
                       401: ('unauthorized', 'Evidence authentication failed.'),
                       403: ('forbidden', 'Evidence access denied.'),
                       429: ('rate_limited', 'Evidence rate limit reached.')}.get(code,
                       ('download_failed', 'Evidence download failed.'))
            raise ProviderError(*message)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > MAX_REPORT_BYTES:
                raise ProviderError('report_too_large', 'Report exceeds 10 MB.')
            body.extend(chunk)
        return bytes(body)
