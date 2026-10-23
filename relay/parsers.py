"""Normalize runner reports without asking a model to invent results."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def robot_report(path: Path) -> list[dict]:
    root = ET.parse(path).getroot()
    tests = []
    def visit(suite, parents):
        names = parents + [suite.get('name', '')]
        for test in suite.findall('test'):
            status = test.find('status')
            if status is None:
                raise ValueError('Robot test has no status')
            tests.append({'id': test.get('id'), 'name': '.'.join(names + [test.get('name', '')]),
                          'status': status.get('status', 'UNKNOWN').lower(),
                          'message': status.text or '', 'evidence': 'output.xml'})
        for child in suite.findall('suite'):
            visit(child, names)
    for suite in root.findall('suite'):
        visit(suite, [])
    return tests


def playwright_report(path: Path) -> list[dict]:
    report = json.loads(path.read_text())
    if report.get('errors'):
        raise ValueError('Playwright reported global errors; inspect results.json')
    tests = []
    def visit(suite, parents):
        names = parents + [suite.get('title', '')]
        for spec in suite.get('specs', []):
            for test in spec.get('tests', []):
                results = test.get('results', [])
                last = results[-1] if results else {}
                outcome = test.get('status', 'unexpected')
                tests.append({'id': spec.get('id'),
                              'name': ' / '.join(names + [spec['title'], test.get('projectName', '')]),
                              'status': {'expected': 'pass', 'unexpected': 'fail', 'flaky': 'flaky', 'skipped': 'skip'}.get(outcome, 'unknown'),
                              'message': '\n'.join(e.get('message', '') for e in last.get('errors', [])),
                              'attempts': len(results), 'actual_status': last.get('status'),
                              'evidence': 'results.json'})
        for child in suite.get('suites', []):
            visit(child, names)
    for suite in report.get('suites', []):
        visit(suite, [])
    return tests
