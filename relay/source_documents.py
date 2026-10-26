"""Bounded local source snapshots and OpenAPI operation extraction."""
import hashlib
import json
import os
import re
from urllib.parse import unquote

import yaml

from .knowledge import read_file


def read_local(source, path, max_bytes=None, byte_counter=None):
    if source.provider != 'local' or not source.enabled or path not in source.files:
        raise ValueError('Source file unavailable')
    limit = source.max_file_bytes if max_bytes is None else min(source.max_file_bytes, max_bytes)
    if limit < 1:
        raise ValueError('Source read budget exhausted')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in source.location.split('/')[1:] + path.split('/')[:-1]:
            if not part:
                continue
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        body = read_file(fd, path.split('/')[-1], limit, byte_counter=byte_counter)
    finally:
        os.close(fd)
    text = body.decode('utf-8')
    if '\x00' in text or 'PRIVATE KEY-----' in text:
        raise ValueError('Excluded source content')
    return {'path': path, 'sha256': hashlib.sha256(body).hexdigest(), 'text': text}


def extract_operation(snapshot, method, path, max_bytes=20000):
    try:
        return _extract_operation(snapshot, method, path, max_bytes)
    except (yaml.YAMLError, RecursionError, TypeError, AttributeError, KeyError, OverflowError) as exc:
        raise ValueError("Invalid API structure") from None


def _extract_operation(snapshot, method, path, max_bytes):
    # JSON first avoids YAML implicit scalar conversion for ordinary JSON specs.
    text = snapshot['text']
    # Bound syntax depth before either parser builds a Python object graph.
    depth = 0
    for count, event in enumerate(yaml.parse(text)):
        if count > 20000 or isinstance(event, yaml.events.AliasEvent):
            raise ValueError('Source complexity limit exceeded')
        if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
            depth += 1
        elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
            depth -= 1
        if depth > 64:
            raise ValueError('Source depth limit exceeded')
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        document = yaml.safe_load(text)
    if not isinstance(document, dict) or not (document.get('swagger') == '2.0' or str(document.get('openapi', '')).startswith(('3.0.', '3.1.'))):
        raise ValueError('Unsupported API specification')
    paths = document.get('paths', {})
    if not isinstance(paths, dict):
        raise ValueError('Invalid API paths')
    components = document.get('components', {})
    if not isinstance(components, dict):
        raise ValueError('Invalid API components')
    item = paths.get(path)
    if isinstance(item, dict) and '$ref' in item:
        raise ValueError('Referenced path items are unsupported')
    operation = item.get(method.lower()) if isinstance(item, dict) else None
    if not isinstance(operation, dict):
        raise ValueError('Operation unavailable')
    result = {'operation': operation, 'path_parameters': item.get('parameters', []),
              'security': operation.get('security', document.get('security', [])), 'references': {}}
    if document.get('swagger') == '2.0':
        result['transport'] = {key: document[key] for key in ('host', 'basePath') if key in document}
        for key in ('consumes', 'produces', 'schemes'):
            if key in operation or key in document:
                result[key] = operation.get(key, document.get(key))
        if 'schemes' in result:
            result['transport']['schemes'] = result['schemes']
    else:
        result['servers'] = operation.get('servers', item.get('servers', document.get('servers', [{'url': '/'}])))
    pending = [result.get('servers', []), result['operation'], result['path_parameters'], result['security']]
    # Include declared security scheme definitions when authentication applies.
    if result['security']:
        result['security_definitions'] = document.get('securityDefinitions', components.get('securitySchemes', {}))
        pending.append(result['security_definitions'])
    nodes = 0
    while pending:
        value = pending.pop()
        nodes += 1
        if nodes > 10000:
            raise ValueError('Dependency traversal limit exceeded')
        if isinstance(value, dict):
            ref = value.get('$ref')
            if ref is not None:
                if not isinstance(ref, str) or not ref.startswith('#'):
                    raise ValueError('External references are unsupported')
                if ref not in result['references']:
                    target = document
                    try:
                        if re.search(r'%(?![0-9A-Fa-f]{2})', ref):
                            raise ValueError('Invalid reference encoding')
                        pointer = unquote(ref[1:], errors='strict')
                        if pointer and not pointer.startswith('/'):
                            raise ValueError('Invalid local pointer')
                        parts = pointer[1:].split('/') if pointer else []
                        for part in parts:
                            if re.search(r'~(?![01])', part):
                                raise ValueError('Invalid pointer escape')
                            part = part.replace('~1', '/').replace('~0', '~')
                            if isinstance(target, list):
                                if not re.fullmatch(r'0|[1-9][0-9]*', part):
                                    raise ValueError('Invalid array index')
                                target = target[int(part)]
                            else:
                                target = target[part]
                    except (KeyError, TypeError, ValueError, IndexError):
                        raise ValueError('Unresolved API reference') from None
                    result['references'][ref] = target
                    pending.append(target)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    try:
        encoded = json.dumps(result, ensure_ascii=False)
    except (ValueError, RecursionError, TypeError):
        raise ValueError('Invalid API structure') from None
    if len(encoded.encode('utf-8')) > max_bytes:
        raise ValueError('Operation dependencies exceed context budget')
    return {'path': snapshot['path'], 'sha256': snapshot['sha256'], 'selector': method.upper() + ' ' + path,
            'parser_version': 'openapi-operation-v1', 'text': encoded}
