"""Atomic, immutable knowledge bundle snapshots beneath a no-follow private root.

The current pointer is the only commit point. A crash before pointer replacement
leaves the previous complete bundle active; orphan snapshots are never discovered.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
import re
import stat
from uuid import uuid4
from .knowledge import ALLOWED, MAX_FILE, MAX_TOTAL, read_file

POINTER = '.relay-current.json'
MAX_SNAPSHOT = 180_000


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def scope_directory(category, scope, create=False):
    if category not in ('automation', 'sut') or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', scope):
        raise ValueError('Invalid scope')
    root = os.environ.get('RELAY_KNOWLEDGE_ROOT', '')
    if not root or not os.path.isabs(root):
        raise ValueError('Configure an absolute knowledge root')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.split('/')[1:]:
            if not part or part in ('.', '..'):
                raise ValueError('Invalid root')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        for part in (category, scope):
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def validate_documents(documents):
    if not isinstance(documents, list) or len(documents) > 4:
        raise ValueError('Invalid document count')
    names, total = set(), 0
    for doc in documents:
        if not isinstance(doc, dict) or doc.get('path') not in ALLOWED or doc['path'] in names:
            raise ValueError('Invalid document path')
        names.add(doc['path'])
        text = doc.get('text')
        if not isinstance(text, str) or '\x00' in text or 'PRIVATE KEY-----' in text:
            raise ValueError('Invalid document content')
        body = text.encode()
        total += len(body)
        if len(body) > MAX_FILE or total > MAX_TOTAL or hashlib.sha256(body).hexdigest() != doc.get('sha256'):
            raise ValueError('Invalid document bytes or hash')
        for key in ('revision', 'owner', 'source', 'type'):
            if not isinstance(doc.get(key), str) or not 0 < len(doc[key]) <= 200:
                raise ValueError('Invalid document metadata')
        if not isinstance(doc.get('environments'), list) or not doc['environments'] or any(not isinstance(env, str) or len(env) > 256 for env in doc['environments']):
            raise ValueError('Invalid environments')
        if doc.get('verification') not in ('verified', 'unverified'):
            raise ValueError('Invalid verification')
        date.fromisoformat(doc.get('reviewed_at', ''))
    return documents


def revision_file(revision):
    if not re.fullmatch(r'[a-f0-9]{32}', revision):
        raise ValueError('Invalid revision')
    return f'.relay-revision-{revision}.json'


def read_revision(fd, revision, category, scope):
    data = json.loads(read_file(fd, revision_file(revision), MAX_SNAPSHOT))
    if not isinstance(data, dict):
        raise ValueError('Invalid snapshot')
    if data.get('category') != category or data.get('scope_id') != scope or data.get('revision') != revision:
        raise ValueError('Invalid revision scope')
    validate_documents(data['documents'])
    if data.get('hash') != digest(data['documents']):
        raise ValueError('Invalid revision hash')
    return data


def current(fd, category, scope):
    try:
        pointer = json.loads(read_file(fd, POINTER, 1000))
    except FileNotFoundError:
        return None
    if not isinstance(pointer, dict) or not isinstance(pointer.get('revision'), str):
        raise ValueError('Invalid current pointer')
    return read_revision(fd, pointer['revision'], category, scope)


def baseline(fd, category, scope):
    active = current(fd, category, scope)
    if active is not None:
        return active
    try:
        manifest = json.loads(read_file(fd, 'manifest.json', 12000))
    except FileNotFoundError:
        documents = []
    else:
        if not isinstance(manifest, dict) or manifest.get('schema_version') != 2 or manifest.get('category') != category or manifest.get('scope_id') != scope:
            raise ValueError('Migrate the bundle to schema version 2 before editing')
        if not isinstance(manifest.get('documents'), list) or len(manifest['documents']) > 4:
            raise ValueError('Invalid manifest')
        documents = []
        for entry in manifest['documents']:
            if not isinstance(entry, dict) or entry.get('path') not in ALLOWED:
                raise ValueError('Invalid document path')
            documents.append(dict(entry, text=read_file(fd, entry['path'], MAX_FILE).decode('utf-8')))
        validate_documents(documents)
    return {'revision': None, 'hash': digest(documents), 'documents': documents, 'category': category, 'scope_id': scope}


@contextmanager
def bundle_lock(fd):
    lock = os.open('.relay-lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError('Invalid lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(lock)


def atomic_json(fd, name, value):
    temporary = f'.relay-tmp-{uuid4().hex}'
    handle = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(json.dumps(value, ensure_ascii=False).encode())
            stream.flush()
            os.fsync(stream.fileno())
        # Reject pre-existing symlinks/FIFOs rather than replacing them.
        try:
            existing = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError('Invalid target')
        except FileNotFoundError:
            pass
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def commit(fd, before, documents, proposal_id, actor, provenance, operation='apply'):
    validate_documents(documents)
    # Import the pre-managed bundle as a rollback target before first commit.
    if before['revision'] is None:
        imported = dict(before, revision=uuid4().hex, previous=None, proposal_id=None,
                        actor=actor, created_at=datetime.now(timezone.utc).isoformat(), operation='import', provenance=[])
        atomic_json(fd, revision_file(imported['revision']), imported)
        before = imported
    revision = uuid4().hex
    snapshot = dict(category=before['category'], scope_id=before['scope_id'], revision=revision,
                    previous=before['revision'], hash=digest(documents), documents=documents,
                    proposal_id=proposal_id, actor=actor, operation=operation, provenance=provenance,
                    created_at=datetime.now(timezone.utc).isoformat())
    atomic_json(fd, revision_file(revision), snapshot)
    atomic_json(fd, POINTER, {'revision': revision})
    return snapshot
