"""Bounded, read-only inspection of suite-level Robot setup and teardown."""
import io
from xml.etree.ElementTree import ParseError
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from .execution import PlanError
from .report_evidence import MAX_REPORT_BYTES
from .report_sections import MAX_NODES, MAX_DEPTH, TEXT_LIMIT


def inspect_suites(body, sha, offset=0, limit=20, suite_id=None, sanitize=None):
    clean = sanitize or (lambda value: value)
    if offset < 0 or offset > 100000 or not 1 <= limit <= 50 or (suite_id is not None and (not suite_id.isdecimal() or str(int(suite_id)) != suite_id or len(suite_id) > 10)):
        raise PlanError('invalid_suite_request', 'Invalid suite or page.', 422)
    if len(body) > MAX_REPORT_BYTES:
        raise PlanError('report_limit', 'Report exceeds 10 MB.', 413)
    stack, suites, suite_stack, selected, records = [], [], [], None, []
    current_kw_depth = None
    kw_scope = None
    nodes = count = 0
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
                    name = clean(element.get('name', ''))
                    path = ' / '.join([*(item['name'] for item in suite_stack), name[:300]])
                    suite = {'id':str(len(suites)), 'name':name[:300], 'suite_path':path[:600],
                             'suite_path_truncated':len(path)>600 or len(name)>300 or any(item['suite_path_truncated'] for item in suite_stack),
                             'status':'UNKNOWN', 'status_message':'', 'status_message_truncated':False, 'start':None, 'end':None, 'elapsed':None}
                    suites.append(suite)
                    suite_stack.append(suite)
                    if suite_id == suite['id']:
                        selected = suite
                elif (element.tag == 'kw' and suite_stack and len(stack) > 1 and stack[-2] == 'suite'
                      and element.get('type', '').upper() in ('SETUP', 'TEARDOWN')):
                    current_kw_depth = len(stack)
                    kw_scope = element.get('type').upper()
            else:
                if suite_stack and element.tag == 'status' and len(stack) > 1 and stack[-2] == 'suite':
                    suite = suite_stack[-1]
                    suite['status'] = clean(element.get('status', 'UNKNOWN'))[:40]
                    status_message = clean(element.text or '')
                    suite['status_message'] = status_message[:TEXT_LIMIT]
                    suite['status_message_truncated'] = len(status_message) > TEXT_LIMIT
                    suite['start'] = clean(element.get('start', element.get('starttime', '')))[:100] or None
                    suite['end'] = clean(element.get('end', element.get('endtime', '')))[:100] or None
                    suite['elapsed'] = clean(element.get('elapsed', ''))[:100] or None
                    if suite_id == suite['id']:
                        if offset <= count < offset + limit:
                            records.append({'section_id':str(count), 'scope':'SUITE', 'kind':'status', 'name':'',
                                'text':suite['status_message'], 'truncated':suite['status_message_truncated'],
                                'status':suite['status'], 'type':None, 'level':None})
                        count += 1
                if current_kw_depth is not None and suite_stack and suite_stack[-1]['id'] == suite_id:
                    if element.tag in ('kw', 'msg', 'status', 'arg') and len(stack) >= current_kw_depth:
                        if offset <= count < offset + limit:
                            value = clean(element.text or '')
                            records.append({'section_id':str(count), 'scope':kw_scope, 'kind':element.tag,
                                            'name':clean(element.get('name', ''))[:300], 'text':value[:TEXT_LIMIT],
                                            'truncated':len(value)>TEXT_LIMIT, 'status':clean(element.get('status', '')) if element.tag == 'status' else None,
                                            'type':clean(element.get('type', '')) if element.tag == 'kw' and element.get('type') is not None else None,
                                            'level':clean(element.get('level', '')) if element.tag == 'msg' and element.get('level') is not None else None})
                        count += 1
                if element.tag == 'kw' and current_kw_depth == len(stack):
                    current_kw_depth = None
                    kw_scope = None
                if element.tag == 'suite':
                    suite_stack.pop()
                element.clear()
                stack.pop()
    except (ParseError, DefusedXmlException, LookupError, ValueError):
        raise PlanError('invalid_report', 'Malformed or unsafe XML report.', 422) from None
    if suite_id is not None:
        if selected is None:
            raise PlanError('not_found', 'Suite not found in this report.', 404)
        return {'sha256':sha, 'suite':selected, 'items':records, 'total':count, 'offset':offset,
                'next_offset':offset+len(records) if offset+len(records)<count else None,
                'limitations':['Suite status does not prove independent test root causes.', 'Local inspection only; not shared with AI.']}
    return {'sha256':sha, 'items':suites[offset:offset+limit], 'total':len(suites), 'offset':offset,
            'next_offset':offset+limit if offset+limit<len(suites) else None,
            'limitations':['Suite status does not prove independent test root causes.', 'Local inspection only; not shared with AI.']}
