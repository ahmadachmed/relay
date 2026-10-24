"""Read-only Bitbucket Cloud REST provider. Credentials never enter the registry."""
import os
from urllib.parse import quote, urlsplit
import httpx
import yaml
from ..contracts import Project
from . import ProviderError

BASE = 'https://api.bitbucket.org/2.0'


class BitbucketProvider:
    def __init__(self, client: httpx.Client):
        self.client = client

    @classmethod
    def from_environment(cls):
        email = os.getenv('RELAY_BITBUCKET_EMAIL')
        token = os.getenv('RELAY_BITBUCKET_API_TOKEN')
        if not email or not token:
            raise ProviderError('not_configured', 'Set RELAY_BITBUCKET_EMAIL and RELAY_BITBUCKET_API_TOKEN on the backend.')
        return cls(httpx.Client(auth=httpx.BasicAuth(email, token), timeout=20, follow_redirects=False))

    def close(self):
        self.client.close()

    def get(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'api.bitbucket.org' or not parsed.path.startswith('/2.0/repositories/') or parsed.fragment:
            raise ProviderError('invalid_response', 'Bitbucket returned an invalid resource URL.')
        try:
            with self.client.stream('GET', url) as response:
                errors = {
                    401: ('unauthorized', 'Bitbucket rejected the credential; check its expiry.'),
                    403: ('forbidden', 'Credential lacks access to this repository.'),
                    404: ('not_found', 'Repository, branch or pipeline configuration was not found.'),
                    429: ('rate_limited', 'Bitbucket rate limit reached; retry later.'),
                }
                if response.status_code in errors:
                    raise ProviderError(*errors[response.status_code])
                if not 200 <= response.status_code < 300:
                    raise ProviderError('upstream_error', 'Bitbucket could not complete the request.')
                chunks = []; size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ProviderError('response_too_large', 'Bitbucket response exceeds the catalog size limit.')
                    chunks.append(chunk)
                return b''.join(chunks)
        except httpx.HTTPError:
            raise ProviderError('connection_failed', 'Cannot reach Bitbucket; check backend network access.') from None

    def json(self, url):
        import json
        try:
            value = json.loads(self.get(url))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise ProviderError('invalid_response', 'Bitbucket returned invalid JSON.') from None

    def catalog(self, project: Project):
        workspace, repo = project.repository.split('/')
        # Dot-only coordinates are not accepted as URL path components.
        if workspace in ('.', '..') or repo in ('.', '..'):
            raise ProviderError('invalid_configuration', 'Invalid repository coordinate.')
        root = f'{BASE}/repositories/{quote(workspace, safe="")}/{quote(repo, safe="")}'
        self.json(root)  # Verify actual repository access first.
        branches = []; url = root + '/refs/branches?pagelen=100'; seen = set()
        while url:
            if url in seen or len(seen) >= 50:
                raise ProviderError('pagination_limit', 'Branch catalog is incomplete; narrow the repository configuration.')
            # Follow only branch pagination for this exact repository.
            if urlsplit(url).path != urlsplit(root + '/refs/branches').path:
                raise ProviderError('invalid_response', 'Invalid branch pagination URL.')
            seen.add(url)
            page = self.json(url)
            values = page.get('values')
            if not isinstance(values, list) or any(not isinstance(v, dict) or not isinstance(v.get('name'), str) for v in values):
                raise ProviderError('invalid_response', 'Bitbucket returned an invalid branch list.')
            branches.extend(v['name'] for v in values)
            url = page.get('next')
            if url is not None and not isinstance(url, str):
                raise ProviderError('invalid_response', 'Invalid branch pagination URL.')
        if project.default_branch not in branches:
            raise ProviderError('not_found', 'Configured default branch was not found.')
        raw = self.get(root + '/src/' + quote(project.default_branch, safe='') + '/bitbucket-pipelines.yml')
        try:
            document = yaml.safe_load(raw)
            custom = document.get('pipelines', {}).get('custom', {})
            if not isinstance(custom, dict):
                raise ValueError()
        except (yaml.YAMLError, AttributeError, ValueError, RecursionError):
            raise ProviderError('invalid_pipeline_config', 'Cannot read custom pipeline declarations.') from None
        # Publish configured metadata only, never raw YAML, scripts or secret defaults.
        return {
            'provider': 'bitbucket', 'transport': 'rest', 'connected': True,
            'repository': project.repository, 'branch': project.default_branch,
            'branches': [b for b in branches if b in project.allowed_branches],
            'pipelines': [dict(id=key, selector=p.selector, available=p.selector in custom,
                               parameters={k:v.model_dump() for k,v in p.parameters.items()})
                          for key,p in project.pipelines.items()],
        }
