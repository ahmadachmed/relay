"""Bounded, read-only project knowledge; no directory discovery or code execution."""
from datetime import date
import hashlib
import json
import os
import re
import stat

ALLOWED = {'overview.md', 'business-rules.md', 'troubleshooting.md', 'api-contract.yaml'}
MAX_FILE = 8000
MAX_TOTAL = 20000


def read_file(directory, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Not a regular file')
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError('File exceeds budget')
    return body


def load_knowledge(project_id, environment):
    result = {'sources': [], 'diagnostics': []}
    root = os.getenv('RELAY_KNOWLEDGE_ROOT')
    if not root:
        result['diagnostics'].append('Project knowledge is not configured.')
        return result
    if not re.fullmatch(r'[A-Za-z0-9_-]+', project_id):
        result['diagnostics'].append('Invalid project knowledge scope.')
        return result
    try:
        # Walk absolute components with dirfd and NOFOLLOW; no symlink escapes.
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in os.path.abspath(root).split('/')[1:] + [project_id]:
                if not part:
                    continue
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            manifest = json.loads(read_file(fd, 'manifest.json', 12000))
            if not isinstance(manifest, list) or len(manifest) > 4:
                raise ValueError('Invalid manifest')
            total, ids = 0, set()
            for entry in manifest:
                try:
                    if not isinstance(entry, dict):
                        raise ValueError('Invalid entry')
                    name, doc_id = entry.get('path'), entry.get('id')
                    if name not in ALLOWED or not isinstance(doc_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', doc_id) or doc_id in ids:
                        raise ValueError('Invalid or duplicate document identity/path')
                    ids.add(doc_id)
                    if entry.get('project_id') != project_id:
                        raise ValueError('Project does not match')
                    envs = entry.get('environments')
                    if not isinstance(envs, list) or not envs or any(not isinstance(e, str) for e in envs):
                        raise ValueError('Invalid environment applicability')
                    if environment is None or environment not in envs:
                        result['diagnostics'].append(f'{doc_id}: environment does not match; omitted.')
                        continue
                    if entry.get('verification') != 'verified':
                        raise ValueError('Document is not verified')
                    if any(not isinstance(entry.get(k), str) or not 0 < len(entry[k]) <= 200 for k in ('owner', 'source', 'type', 'revision')):
                        raise ValueError('Missing source/review metadata')
                    reviewed = date.fromisoformat(entry.get('reviewed_at', ''))
                    age = (date.today() - reviewed).days
                    if not 0 <= age <= 180:
                        raise ValueError('Review date is stale or in the future')
                    body = read_file(fd, name, MAX_FILE)
                    digest = hashlib.sha256(body).hexdigest()
                    if entry.get('sha256') != digest:
                        raise ValueError('Document changed since review')
                    text = body.decode('utf-8')
                    if '\x00' in text or 'PRIVATE KEY-----' in text:
                        raise ValueError('Excluded content')
                    total += len(body)
                    if total > MAX_TOTAL:
                        raise ValueError('Total knowledge budget exceeded')
                    digest = hashlib.sha256(body).hexdigest()
                    result['sources'].append({'id': hashlib.sha256(f'{project_id}:{doc_id}:{digest}'.encode()).hexdigest(),
                        'document_id': doc_id, 'project_id': project_id, 'path': name, 'sha256': digest,
                        'revision': entry['revision'], 'owner': entry['owner'], 'source': entry['source'],
                        'type': entry['type'], 'reviewed_at': reviewed.isoformat(), 'verification': 'operator_verified', 'text': text})
                except (OSError, ValueError, TypeError, UnicodeError) as exc:
                    reasons = {'Invalid entry', 'Invalid or duplicate document identity/path', 'Project does not match',
                               'Invalid environment applicability', 'Document is not verified', 'Missing source/review metadata',
                               'Review date is stale or in the future', 'Document changed since review', 'Excluded content',
                               'Total knowledge budget exceeded', 'File exceeds budget', 'Not a regular file'}
                    reason = str(exc) if type(exc) is ValueError and str(exc) in reasons else 'Document is missing, unreadable or invalid'
                    result['diagnostics'].append(reason + '; omitted.')
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError, RecursionError):
        result['diagnostics'].append('Knowledge directory or manifest is unavailable or invalid.')
    return result
