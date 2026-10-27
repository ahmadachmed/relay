import pytest
from fastapi.testclient import TestClient
from relay.api import create_app
from relay.execution import PlanError
from relay.report_search import search_robot
from relay.report_sections import inspect_robot

REPORT = b'<robot><suite name="Root"><test name="first"><kw name="Connect"><msg>Connection timeout</msg><status status="FAIL"/></kw><status status="FAIL"/></test><test name="second"><kw name="Timeout handler"><msg>connection timeout</msg><status status="PASS"/></kw><status status="PASS"/></test></suite></robot>'


def test_search_pagination_and_resolvable_sections():
    all_results = search_robot(REPORT, 'hash', 'timeout')
    assert all_results['total'] == 3
    pages = [search_robot(REPORT, 'hash', 'timeout', offset=i, limit=1) for i in range(3)]
    assert [page['next_offset'] for page in pages] == [1, 2, None]
    for page in pages:
        item = page['items'][0]
        detail = inspect_robot(REPORT, 'hash', test_id=item['test_id'], section_id=item['section_id'])
        assert detail['items'][0]['kind'] == item['kind']
        assert item['suite_path'] == 'Root'


def test_search_redacts_before_matching_and_handles_malformed():
    assert search_robot(REPORT, 'hash', 'timeout', sanitize=lambda value: value.replace('timeout', '[REDACTED]').replace('Timeout', '[REDACTED]'))['total'] == 0
    with pytest.raises(PlanError):
        search_robot(b'<!DOCTYPE robot><robot/>', 'hash', 'x')


def test_search_is_scoped_and_deleted_with_attachment(tmp_path):
    with TestClient(create_app(data=tmp_path, monitoring=False)) as client:
        a, b = [client.post('/api/conversations').json()['id'] for _ in range(2)]
        base = f'/api/conversations/{a}/attachments'
        file = client.post(base, params={'filename': 'output.xml'}, content=REPORT).json()
        path = f"{base}/{file['id']}/robot-report/search"
        assert client.post(path, json={'query': 'timeout'}).json()['total'] == 3
        assert client.post(path.replace(a, b), json={'query': 'timeout'}).status_code == 404
        assert client.post(path, json={'query': '   '}).status_code == 422
        client.delete(f"{base}/{file['id']}")
        assert client.post(path, json={'query': 'timeout'}).status_code == 404


def test_search_excerpt_keeps_unicode_offsets_and_match():
    text = 'İ' * 1800 + ' expected timeout'
    body = ('<robot><suite><test name="x"><msg>' + text + '</msg><status status="FAIL"/></test></suite></robot>').encode()
    item = search_robot(body, 'hash', 'timeout')['items'][0]
    assert item['text_offset'] > 0 and 'timeout' in item['text']
    assert item['text'] == text[item['text_offset']:item['text_offset']+500]
    assert item['text_truncated']


def test_suite_search_requires_scope_and_resolves_setup_section():
    from relay.suite_sections import inspect_suites
    body=b'<robot><suite name="Root"><kw name="init" type="SETUP"><msg>database unavailable</msg><status status="FAIL"/></kw><test name="blocked"><status status="FAIL"/></test><status status="FAIL">setup failed</status></suite></robot>'
    assert search_robot(body,'hash','database')['total']==0
    result=search_robot(body,'hash','database',include_suites=True)
    match=result['items'][0]
    assert match['test_id'] is None and match['suite_id']=='0' and match['scope']=='SETUP'
    details=inspect_suites(body,'hash',suite_id=match['suite_id'],offset=int(match['section_id']),limit=1)
    assert details['items'][0]['text']=='database unavailable'
