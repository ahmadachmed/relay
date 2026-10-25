"""Evidence-backed XML normalization. Never infer test success from pipeline status."""
import hashlib
import json
from xml.etree.ElementTree import ParseError
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

MAX_REPORT_BYTES = 10_000_000


def parse_report(body, format, artifact_id):
    if len(body) > MAX_REPORT_BYTES:
        raise ValueError('Oversized report')
    if format == 'allure_json':
        return parse_allure_result(body, artifact_id)
    if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise ValueError('Unsafe XML report')
    try:
        root = ET.fromstring(body, forbid_dtd=True)
    except (ParseError, DefusedXmlException):
        raise ValueError('Malformed or unsafe XML report') from None
    tests = []
    occurrences = {}

    def add(identity, name, status, message=''):
        count = occurrences.get(identity, 0)
        occurrences[identity] = count + 1
        stable_id = hashlib.sha256((artifact_id + ':' + identity + ':' + str(count)).encode()).hexdigest()
        tests.append(dict(id=stable_id, name=name,
                          status=status, message=message[:2000], artifact_ids=[artifact_id], attempts=None))

    if format == 'robot_xml':
        if root.tag != 'robot':
            raise ValueError('Expected Robot output XML')
        def visit(suite, parents):
            names = parents + [suite.get('name', '')]
            for test in suite.findall('test'):
                state = test.find('status')
                if state is None:
                    raise ValueError('Missing Robot test status')
                status = {'PASS': 'pass', 'FAIL': 'fail', 'SKIP': 'skip'}.get(state.get('status'), 'unknown')
                name = '.'.join(names + [test.get('name', '')])
                add(test.get('id', name) + name, name, status, state.text or '')
            for kw in suite.findall('kw'):
                state = kw.find('status')
                if kw.get('type') in ('SETUP', 'TEARDOWN') and state is not None and state.get('status') == 'FAIL':
                    name = '.'.join(names) + ' [' + kw.get('type') + ']'
                    add(name, name, 'error', state.text or '')
            for child in suite.findall('suite'):
                visit(child, names)
        for suite in root.findall('suite'):
            visit(suite, [])
        # Root log messages are diagnostics, not additional test outcomes.
        # Their original contents remain in the downloadable XML evidence.
    elif format == 'junit_xml':
        if root.tag not in ('testsuite', 'testsuites'):
            raise ValueError('Expected JUnit XML')
        def visit(suite, parents):
            names = parents + [suite.get('name', '')]
            counts = dict(tests=0, failures=0, errors=0, skipped=0)
            for i, test in enumerate(suite.findall('testcase')):
                name = ' / '.join(names + [test.get('classname', ''), test.get('name', '')])
                status, messages = 'pass', []
                for tag, outcome, counter in [('skipped', 'skip', 'skipped'),
                        ('failure', 'fail', 'failures'), ('error', 'error', 'errors')]:
                    children = test.findall(tag)
                    counts[counter] += len(children)
                    if children:
                        status = outcome
                    for child in children:
                        messages.append(child.get('message', '') + '\n' + (child.text or ''))
                message = '\n'.join(messages)
                add(name + ':' + str(i), name, status, message)
                counts['tests'] += 1
            for i, error in enumerate(suite.findall('error')):
                name = ' / '.join(names) + ' [suite error]'
                add(name + str(i), name, 'error', error.get('message', '') + (error.text or ''))
                counts['errors'] += 1
            for child in suite.findall('testsuite'):
                nested = visit(child, names)
                for key in counts:
                    counts[key] += nested[key]
            # Validate only supplied counters; wrapper totals include nested suites.
            for key, actual in counts.items():
                declared = suite.get(key)
                if declared is not None:
                    if (not declared.isascii() or not declared.isdecimal()
                            or int(declared) != actual):
                        raise ValueError('Inconsistent JUnit report counters')
            return counts
        visit(root, [])
    else:
        raise ValueError('Unsupported report format')
    if not tests:
        raise ValueError('Report contains no tests or execution errors')
    if len(tests) > 100000:
        raise ValueError('Too many report results')
    return tests


def parse_allure_result(body, artifact_id):
    """One Allure *-result.json, not a launch, container or attachment bundle."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        data = json.loads(body, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON constant')))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Malformed Allure JSON') from None
    if not isinstance(data, dict):
        raise ValueError('Expected one Allure test result object')
    for key in ('uuid', 'name', 'status'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError('Missing Allure test identity or status')
    statuses = {'passed': 'pass', 'failed': 'fail', 'broken': 'error', 'skipped': 'skip', 'unknown': 'unknown'}
    if data['status'] not in statuses:
        raise ValueError('Unsupported Allure status')
    details = data.get('statusDetails', {})
    if not isinstance(details, dict) or any(not isinstance(details.get(k, ''), str) for k in ('message', 'trace')):
        raise ValueError('Invalid Allure status details')
    name = data.get('fullName') or data['name']
    if not isinstance(name, str) or (data.get('stage') is not None and not isinstance(data['stage'], str)):
        raise ValueError('Invalid Allure identity or stage')
    status = statuses[data['status']]
    if data.get('stage', 'finished') != 'finished':
        status = 'unknown'
    return [dict(id=hashlib.sha256((artifact_id + ':' + data['uuid']).encode()).hexdigest(),
                 name=name, status=status, message='\n'.join(details.get(k, '') for k in ('message', 'trace') if details.get(k))[:2000],
                 artifact_ids=[artifact_id], attempts=None)]
