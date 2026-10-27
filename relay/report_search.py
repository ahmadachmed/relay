"""Bounded literal search through tests in a Robot output report."""
import io
from xml.etree.ElementTree import ParseError
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from .execution import PlanError
from .report_evidence import MAX_REPORT_BYTES
from .report_sections import MAX_NODES, MAX_DEPTH


def search_robot(body, sha, query, offset=0, limit=20, sanitize=None, include_suites=False):
    clean = sanitize or (lambda value: value)
    if not query.strip() or len(query) > 200 or offset < 0 or not 1 <= limit <= 50:
        raise PlanError('invalid_search', 'Enter a search term of 1–200 characters and a valid page.', 422)
    if len(body) > MAX_REPORT_BYTES:
        raise PlanError('report_limit', 'Report exceeds 10 MB.', 413)
    needle = query.casefold()
    stack, suites, results = [], [], []
    total = nodes = tests = suite_count = 0
    suite_kw_depth = None
    suite_kw_scope = None
    current = None
    section = 0

    def add(kind, section_id, value, name='', suite=None, scope=None):
        nonlocal total
        # Sanitize full fields before matching or creating a bounded excerpt.
        value, name = clean(value), clean(name)
        if needle not in value.casefold() and needle not in name.casefold():
            return
        if offset <= total < offset + limit:
            position = value.casefold().find(needle)
            # Casefolding may expand characters; translate the position to the
            # original text before computing the excerpt's character offset.
            start = 0
            if position >= 0:
                folded_offset = 0
                for char_index, char in enumerate(value):
                    if folded_offset + len(char.casefold()) > position:
                        start = max(0, char_index - 100)
                        break
                    folded_offset += len(char.casefold())
            owner = suite if suite is not None else current
            results.append({'test_id': None if suite is not None else owner['id'], 'test_name': None if suite is not None else owner['name'],
                'suite_id': owner['id'] if suite is not None else None, 'suite_path': owner['suite_path'], 'scope': scope, 'kind': kind, 'section_id': section_id,
                'name': name[:300], 'text': value[start:start+500], 'text_offset': start,
                'text_truncated': start > 0 or start + 500 < len(value)})
        total += 1

    try:
        for event, element in ET.iterparse(io.BytesIO(body), events=('start', 'end'), forbid_dtd=True):
            if event == 'start':
                nodes += 1
                stack.append(element.tag)
                if nodes > MAX_NODES or len(stack) > MAX_DEPTH:
                    raise PlanError('report_limit', 'Report exceeds node or nesting limits.', 413)
                if nodes == 1 and element.tag != 'robot':
                    raise PlanError('unsupported_report', 'Expected a Robot Framework output XML.', 415)
                if element.tag == 'suite':
                    name=clean(element.get('name', ''))[:300]
                    path=' / '.join([*(item['name'] for item in suites),name])[:600]
                    suites.append({'id':str(suite_count), 'name':name, 'suite_path':path, 'section':0})
                    suite_count += 1
                elif (include_suites and element.tag == 'kw' and suites and len(stack)>1 and stack[-2]=='suite'
                      and element.get('type', '').upper() in ('SETUP', 'TEARDOWN')):
                    suite_kw_depth=len(stack)
                    suite_kw_scope=element.get('type').upper()
                if element.tag == 'test':
                    if current is not None:
                        raise PlanError('invalid_report', 'Nested tests are unsupported.', 422)
                    test_name = element.get('name', '')
                    current = {'id': str(tests), 'name': clean(test_name)[:300],
                               'suite_path': ' / '.join(item['name'] for item in suites)[:600]}
                    tests += 1
                    section = 0
                    add('test', None, test_name)
            else:
                if current is not None:
                    if element.tag in ('kw', 'msg', 'status', 'arg'):
                        add(element.tag, str(section), element.text or '', element.get('name', ''))
                        section += 1
                    if element.tag == 'test':
                        current = None
                if include_suites and suites and current is None:
                    if suite_kw_depth is not None and len(stack) >= suite_kw_depth and element.tag in ('kw','msg','status','arg'):
                        add(element.tag, str(suites[-1]['section']), element.text or '', element.get('name', ''), suite=suites[-1], scope=suite_kw_scope)
                        suites[-1]['section'] += 1
                    elif element.tag == 'status' and len(stack)>1 and stack[-2]=='suite':
                        add('suite_status', str(suites[-1]['section']), element.text or '', suite=suites[-1], scope='SUITE')
                        suites[-1]['section'] += 1
                if element.tag == 'kw' and suite_kw_depth == len(stack):
                    suite_kw_depth = None
                    suite_kw_scope = None
                if element.tag == 'suite':
                    suites.pop()
                element.clear()
                stack.pop()
    except (ParseError, DefusedXmlException, LookupError, ValueError):
        raise PlanError('invalid_report', 'Malformed or unsafe XML report.', 422) from None
    return {'sha256': sha, 'items': results, 'total': total, 'offset': offset,
            'next_offset': offset + len(results) if offset + len(results) < total else None,
            'total_tests': tests, 'scope': 'operator_uploaded',
            'limitations': [('Search covers test names, test sections, suite setup/teardown and suite status.' if include_suites else 'Search covers test names and test sections; suite setup/teardown is not included.'),
                            'Excerpts may be truncated. Local inspection only; not shared with AI.']}
