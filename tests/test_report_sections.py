import hashlib
import pytest
from relay.report_sections import inspect_robot
from relay.execution import PlanError
from fastapi.testclient import TestClient
from relay.api import create_app


def test_pagination_and_full_failure_access():
    body = ('<robot><suite>' + ''.join(f'<test name="case{i}"><kw name="request"><msg>detail{i}</msg></kw><status status="FAIL"/></test>' for i in range(25)) + '</suite></robot>').encode()
    sha = hashlib.sha256(body).hexdigest()
    first = inspect_robot(body, sha)
    assert first['total'] == 25 and first['next_offset'] == 20
    assert inspect_robot(body, sha, offset=20)['items'][-1]['name'] == 'case24'
    details = inspect_robot(body, sha, test_id='24')
    assert any(r['text'] == 'detail24' for r in details['items'])
    assert details['sha256'] == sha


@pytest.mark.parametrize('body', [b'<robot>', b'<!DOCTYPE robot><robot/>', b'<other/>', b'<robot>' + b'<kw>' * 65 + b'</kw>' * 65 + b'</robot>'])
def test_reject_invalid_reports(body):
    with pytest.raises(PlanError):
        inspect_robot(body, 'hash')


def test_scope_delete_and_query_validation(tmp_path):
    client = TestClient(create_app(data=tmp_path, monitoring=False))
    a, b = [client.post('/api/conversations').json()['id'] for _ in range(2)]
    path = f'/api/conversations/{a}/attachments'
    item = client.post(path, params={'filename': 'output.xml'}, content=b'<robot/>').json()['id']
    url = path + '/' + item + '/robot-report'
    assert client.get(url).json()['total_tests'] == 0
    assert client.get(url.replace(a, b)).status_code == 404
    assert client.get(url, params={'limit': 51}).status_code == 422
    assert client.get(url, params={'test_id': '0'}).status_code == 404
    client.delete(path + '/' + item)
    assert client.get(url).status_code == 404
