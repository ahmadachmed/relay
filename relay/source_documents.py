"""Bounded local source snapshots and OpenAPI operation extraction."""
import hashlib
import json
import os

import yaml

from .knowledge import read_file


def read_local(source, path):
    if source.provider != 'local' or not source.enabled or path not in source.files:
        raise ValueError('Source file unavailable')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in source.location.split('/')[1:] + path.split('/')[:-1]:
            if not part:
                continue
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        body = read_file(fd, path.split('/')[-1], source.max_file_bytes)
    finally:
        os.close(fd)
    text = body.decode('utf-8')
    if '\x00' in text or 'PRIVATE KEY-----' in text:
        raise ValueError('Excluded source content')
    return {'path': path, 'sha256': hashlib.sha256(body).hexdigest(), 'text': text}


def extract_operation(snapshot, method, path, max_bytes=20000):
    # JSON first avoids YAML implicit scalar conversion for ordinary JSON specs.
    text = snapshot['text']
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        depth = 0
        for count, event in enumerate(yaml.parse(text)):
            if count > 20000 or isinstance(event, yaml.events.AliasEvent):
                raise ValueError('YAML complexity limit exceeded')
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
            if depth > 64:
                raise ValueError('YAML depth limit exceeded')
        document = yaml.safe_load(text)
    if not isinstance(document, dict) or not (document.get('swagger') == '2.0' or str(document.get('openapi', '')).startswith(('3.0.', '3.1.'))):
        raise ValueError('Unsupported API specification')
    item = document.get('paths', {}).get(path)
    if isinstance(item, dict) and '$ref' in item:
        raise ValueError('Referenced path items are unsupported')
    operation = item.get(method.lower()) if isinstance(item, dict) else None
    if not isinstance(operation, dict):
        raise ValueError('Operation unavailable')
    result = {'operation': operation, 'path_parameters': item.get('parameters', []),
              'security': operation.get('security', document.get('security', [])), 'references': {}}
    pending = [result['operation'], result['path_parameters'], result['security']]
    # Include declared security scheme definitions when authentication applies.
    if result['security']:
        result['security_definitions'] = document.get('securityDefinitions', document.get('components', {}).get('securitySchemes', {}))
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
                if not isinstance(ref, str) or not ref.startswith('#/'):
                    raise ValueError('External references are unsupported')
                if ref not in result['references']:
                    target = document
                    try:
                        for part in ref[2:].split('/'):
                            part = part.replace('~1', '/').replace('~0', '~')
                            target = target[int(part)] if isinstance(target, list) else target[part]
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
