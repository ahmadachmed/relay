import pytest
from relay.execution import PlanError
from relay.suite_sections import inspect_suites

REPORT=b'''<robot><suite name="Root"><kw name="init" type="SETUP"><msg level="FAIL">No database</msg><status status="FAIL"/></kw><suite name="Child"><test name="one"><status status="FAIL"/></test><status status="FAIL">Parent setup failed</status></suite><kw name="cleanup" type="TEARDOWN"><msg>done</msg><status status="PASS"/></kw><status status="FAIL" start="2026-01-01T00:00:00" elapsed="2.5">Setup failed</status></suite></robot>'''


def test_suite_metadata_and_setup_teardown_are_independent_of_tests():
    listing=inspect_suites(REPORT,'hash')
    assert [(s['id'],s['suite_path'],s['status']) for s in listing['items']]==[('0','Root','FAIL'),('1','Root / Child','FAIL')]
    assert listing['items'][0]['start']=='2026-01-01T00:00:00'
    detail=inspect_suites(REPORT,'hash',suite_id='0',limit=50)
    assert any(x['scope']=='SETUP' and x['text']=='No database' for x in detail['items'])
    assert any(x['scope']=='TEARDOWN' and x['text']=='done' for x in detail['items'])
    assert inspect_suites(REPORT,'hash',suite_id='1')['items'][0]['text']=='Parent setup failed'
    first=inspect_suites(REPORT,'hash',suite_id='0',limit=1)
    assert first['next_offset']==1
    second=inspect_suites(REPORT,'hash',suite_id='0',offset=1,limit=1)
    assert second['items'][0]['section_id']=='1'


def test_suite_redaction_and_invalid_xml():
    result=inspect_suites(REPORT,'hash',suite_id='0',sanitize=lambda s:s.replace('No database','[REDACTED]'))
    assert 'No database' not in str(result)
    assert '[REDACTED]' in str(result)
    with pytest.raises(PlanError): inspect_suites(b'<!DOCTYPE robot><robot/>','hash')
    with pytest.raises(PlanError): inspect_suites(REPORT,'hash',suite_id='99')


def test_suite_inspection_is_conversation_scoped_and_deleted(tmp_path):
    from fastapi.testclient import TestClient
    from relay.api import create_app
    with TestClient(create_app(data=tmp_path,monitoring=False)) as client:
        a,b=[client.post('/api/conversations').json()['id'] for _ in range(2)]
        path=f'/api/conversations/{a}/attachments'
        item=client.post(path,params={'filename':'output.xml'},content=REPORT).json()
        url=f"{path}/{item['id']}/robot-report/suites"
        assert client.get(url,params={'suite_id':'0'}).json()['total']>0
        assert client.get(url.replace(a,b)).status_code==404
        assert client.get(url,params={'suite_id':'01'}).status_code==422
        client.delete(f"{path}/{item['id']}")
        assert client.get(url).status_code==404
