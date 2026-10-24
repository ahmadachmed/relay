import pytest
from relay.report_evidence import parse_report


def test_robot_results_and_setup_errors():
    body = b'<robot><suite name="API"><kw type="SETUP"><status status="FAIL">setup</status></kw><test id="s1-t1" name="Login"><status status="FAIL">bad</status></test><test id="s1-t2" name="Skip"><status status="SKIP"/></test></suite></robot>'
    results = parse_report(body, 'robot_xml', 'artifact-1')
    assert [t['status'] for t in results] == ['fail', 'skip', 'error']
    assert all(t['artifact_ids'] == ['artifact-1'] for t in results)


def test_junit_preserves_identity_and_does_not_invent_retries():
    body = b'<testsuites><testsuite name="pytest"><testcase classname="tests.api" name="test_a[x]"/><testcase name="test_b"><error message="collection failed"/></testcase><testcase name="test_c"><skipped/></testcase><testcase name="test_d"><failure/></testcase></testsuite></testsuites>'
    results = parse_report(body, 'junit_xml', 'artifact-1')
    assert [t['status'] for t in results] == ['pass', 'error', 'skip', 'fail']
    assert all(t['attempts'] is None for t in results)
    assert len({t['id'] for t in results}) == 4
    assert 'test_a[x]' in results[0]['name']


@pytest.mark.parametrize('body', [b'<robot>', b'<robot/>', b'<html/>', b'<!DOCTYPE robot><robot/>', b'<!ENTITY x "x"><robot/>'])
def test_invalid_or_empty_reports_fail(body):
    with pytest.raises(ValueError):
        parse_report(body, 'robot_xml', 'artifact-1')


def test_utf16_dtd_is_rejected():
    body = '<!DOCTYPE robot [<!ENTITY x "SECRET">]><robot><suite name="&x;"/></robot>'.encode('utf-16')
    with pytest.raises(ValueError):
        parse_report(body, 'robot_xml', 'artifact-1')


def test_duplicate_suite_names_keep_unique_result_ids():
    body = b'<testsuites><testsuite name="suite"><testcase name="same"/></testsuite><testsuite name="suite"><testcase name="same"/></testsuite></testsuites>'
    results = parse_report(body, 'junit_xml', 'artifact-1')
    assert len({t['id'] for t in results}) == 2
