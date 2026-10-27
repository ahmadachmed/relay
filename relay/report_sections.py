"""Local, bounded Robot report inspection; no model sharing or run association."""
import io
import hashlib
from xml.etree.ElementTree import ParseError
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from .execution import PlanError
from .report_evidence import MAX_REPORT_BYTES

MAX_NODES = 100_000
MAX_DEPTH = 64
TEXT_LIMIT = 2000


def inspect_robot(body, sha, offset=0, limit=20, test_id=None, sanitize=None, section_id=None, text_offset=0):
    clean = sanitize or (lambda value: value)
    if len(body) > MAX_REPORT_BYTES:
        raise PlanError('report_limit', 'Report exceeds 10 MB.', 413)
    tests, records, stack = [], [], []
    current = None
    nodes = 0
    try:
        for event, element in ET.iterparse(io.BytesIO(body), events=('start', 'end'), forbid_dtd=True):
            if event == 'start':
                nodes += 1
                stack.append(element.tag)
                if nodes > MAX_NODES or len(stack) > MAX_DEPTH:
                    raise PlanError('report_limit', 'Report exceeds node or nesting limits.', 413)
                if nodes == 1 and element.tag != 'robot':
                    raise PlanError('unsupported_report', 'Expected a Robot Framework output XML.', 415)
                if element.tag == 'test':
                    if current is not None:
                        raise PlanError('invalid_report', 'Nested tests are unsupported.', 422)
                    current = {'id': str(len(tests)), 'name': clean(element.get('name', ''))[:300], 'status': 'UNKNOWN'}
                    records = []
            else:
                if current is not None:
                    if element.tag == 'status' and len(stack) > 1 and stack[-2] == 'test':
                        current['status'] = clean(element.get('status', 'UNKNOWN'))
                    if current['id'] == test_id and element.tag in ('kw', 'msg', 'status', 'arg'):
                        original = element.text or ''
                        value = clean(original)
                        item_id = str(len(records))
                        start = text_offset if section_id == item_id else 0
                        end = min(start + TEXT_LIMIT, len(value))
                        records.append({'section_id': item_id, 'kind': element.tag,
                                        'text_offset': start, 'next_text_offset': end if end < len(value) else None,
                                        'text_sha256': hashlib.sha256(value.encode('utf-8')).hexdigest(),
                                        'depth': len(stack), 'name': clean(element.get('name', ''))[:300],
                                        'text': value[start:end], 'truncated': start > 0 or end < len(value),
                                        'status': clean(element.get('status', '')) if element.tag == 'status' else None,
                                        'type': clean(element.get('type', '')) if element.tag == 'kw' and element.get('type') is not None else None,
                                        'level': clean(element.get('level', '')) if element.tag == 'msg' and element.get('level') is not None else None})
                    if element.tag == 'test':
                        if current['id'] == test_id:
                            selected = records
                        tests.append(current)
                        current = None
                element.clear()
                stack.pop()
    except (ParseError, DefusedXmlException, LookupError, ValueError):
        raise PlanError('invalid_report', 'Malformed or unsafe XML report.', 422) from None
    base = {'sha256': sha, 'total_tests': len(tests), 'scope': 'operator_uploaded',
            'limitations': ['Local inspection only; not shared with AI.',
                             'Suite setup/teardown outside tests is not included. No root cause or retry inference.']}
    if test_id is not None:
        if not any(t['id'] == test_id for t in tests):
            raise PlanError('not_found', 'Test not found in this report.', 404)
        base['test'] = next(t for t in tests if t['id'] == test_id)
        items = selected
        if section_id is not None:
            if not section_id.isdecimal() or str(int(section_id)) != section_id or int(section_id) >= len(items):
                raise PlanError('not_found', 'Section not found in this report.', 404)
            item = items[int(section_id)]
            if text_offset > 0 and not item['text']:
                raise PlanError('invalid_report_offset', 'Text offset is outside the section.', 422)
            return dict(base, items=[item], total=len(items), offset=int(section_id), next_offset=(int(section_id) + 1 if int(section_id) + 1 < len(items) else None))
    else:
        items = [t for t in tests if t['status'] == 'FAIL']
    return dict(base, items=items[offset:offset + limit], total=len(items), offset=offset,
                next_offset=offset + limit if offset + limit < len(items) else None)
