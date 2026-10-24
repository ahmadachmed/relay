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


def test_nested_junit_totals_and_optional_counters():
    body = b'<testsuites tests="4" failures="1" errors="1" skipped="1"><testsuite tests="2" failures="1"><testcase name="ok"/><testcase name="bad"><failure/></testcase></testsuite><testsuite><testcase name="error"><error/></testcase><testcase name="skip"><skipped/></testcase></testsuite></testsuites>'
    assert [t['status'] for t in parse_report(body, 'junit_xml', 'a')] == ['pass', 'fail', 'error', 'skip']


@pytest.mark.parametrize('body', [
    b'<testsuites tests="2"><testsuite><testcase name="ok"/></testsuite></testsuites>',
    b'<testsuites><testsuite tests="2"><testcase name="ok"/></testsuite></testsuites>',
])
def test_nested_junit_mismatches_are_rejected(body):
    with pytest.raises(ValueError, match='counters'):
        parse_report(body, 'junit_xml', 'a')


def test_junit_multiple_error_nodes_preserve_all_messages():
    body = b'<testsuite tests="1" errors="2" failures="0"><testcase name="example"><error message="setup">setup details</error><error message="teardown">teardown details</error></testcase></testsuite>'
    result = parse_report(body, 'junit_xml', 'a')
    assert len(result) == 1
    assert result[0]['status'] == 'error'
    assert 'setup details' in result[0]['message']
    assert 'teardown details' in result[0]['message']
    assert result[0]['attempts'] is None


def test_junit_multiple_failures_and_mixed_outcomes():
    body = b'<testsuite tests="1" errors="1" failures="2"><testcase name="example"><failure message="first"/><failure message="second"/><error message="cleanup"/></testcase></testsuite>'
    result = parse_report(body, 'junit_xml', 'a')
    assert result[0]['status'] == 'error'
    assert all(message in result[0]['message'] for message in ['first', 'second', 'cleanup'])


def test_multiple_errors_still_reject_incorrect_counters():
    body = b'<testsuite tests="1" errors="1"><testcase name="example"><error/><error/></testcase></testsuite>'
    with pytest.raises(ValueError, match='counters'):
        parse_report(body, 'junit_xml', 'a')
