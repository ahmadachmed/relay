"""Evidence-backed XML normalization. Never infer test success from pipeline status."""
import hashlib
from xml.etree.ElementTree import ParseError
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

MAX_REPORT_BYTES = 10_000_000


def parse_report(body, format, artifact_id):
    if len(body) > MAX_REPORT_BYTES or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise ValueError('Unsafe or oversized XML report')
    try:
        root = ET.fromstring(body, forbid_dtd=True)
    except (ParseError, DefusedXmlException):
        raise ValueError('Malformed or unsafe XML report') from None
    tests = []
    occurrences = {}

    def add(identity, name, status, message=''):
        count = occurrences.get(identity, 0)
        occurrences[identity] = count + 1
        stable_id = hashlib.sha256((identity + ':' + str(count)).encode()).hexdigest()
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
        for i, msg in enumerate(root.findall('./errors/msg')):
            if msg.get('level') == 'ERROR':
                add('robot-error-' + str(i), 'Robot execution error', 'error', msg.text or '')
    elif format == 'junit_xml':
        if root.tag not in ('testsuite', 'testsuites'):
            raise ValueError('Expected JUnit XML')
        def visit(suite, parents):
            names = parents + [suite.get('name', '')]
            for i, test in enumerate(suite.findall('testcase')):
                name = ' / '.join(names + [test.get('classname', ''), test.get('name', '')])
                status, message = 'pass', ''
                for tag, outcome in [('skipped', 'skip'), ('failure', 'fail'), ('error', 'error')]:
                    child = test.find(tag)
                    if child is not None:
                        status, message = outcome, child.get('message', '') + '\n' + (child.text or '')
                add(name + ':' + str(i), name, status, message)
            for i, error in enumerate(suite.findall('error')):
                name = ' / '.join(names) + ' [suite error]'
                add(name + str(i), name, 'error', error.get('message', '') + (error.text or ''))
            for child in suite.findall('testsuite'):
                visit(child, names)
        visit(root, [])
    else:
        raise ValueError('Unsupported report format')
    if not tests:
        raise ValueError('Report contains no tests or execution errors')
    if len(tests) > 100000:
        raise ValueError('Too many report results')
    return tests
