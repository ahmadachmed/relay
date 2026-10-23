import json
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from relay.api import create_app
from relay.parsers import robot_report, playwright_report
from relay.runner import Runner

ROOT = Path(__file__).resolve().parents[1]


def wait(runner, run_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = runner.get(run_id)
        if run['status'] not in ('queued', 'running'):
            return run
        time.sleep(.05)
    raise AssertionError('Run did not finish')


def test_real_robot_pass_fail(tmp_path):
    runner = Runner(ROOT / 'relay.example.json', tmp_path)
    passed = wait(runner, runner.start('robot-api', 'smoke')['id'])
    assert passed['status'] == 'passed'
    assert passed['tests'][0]['status'] == 'pass'
    failed = wait(runner, runner.start('robot-api', 'failure-demo')['id'])
    assert failed['status'] == 'failed'
    assert 'expected OTP rejection' in failed['tests'][0]['message']


def test_cancel_busy_and_timeout(tmp_path):
    (tmp_path / 'slow.robot').write_text('*** Test Cases ***\nSlow\n    Sleep    10s\n')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'projects': {'slow': {'adapter': 'robot', 'enabled': True, 'path': '.', 'timeout_seconds': 1, 'suites': {'smoke': ['slow.robot']}}}}))
    runner = Runner(config, tmp_path / 'runs')
    run = runner.start('slow', 'smoke')
    with pytest.raises(RuntimeError):
        runner.start('slow', 'smoke')
    runner.cancel(run['id'])
    assert wait(runner, run['id'])['status'] == 'cancelled'
    run = wait(runner, runner.start('slow', 'smoke')['id'])
    assert run['status'] == 'error'
    assert 'timeout' in run['error'].lower() or 'exceeded' in run['error']


def test_api_validation(tmp_path):
    client = TestClient(create_app(ROOT / 'relay.example.json', tmp_path))
    assert client.post('/api/chat', json={'message': 'run mobile smoke'}).status_code == 400
    assert client.post('/api/chat', json={'message': 'run robot-api ;rm'}).status_code == 400
    assert client.post('/api/chat', json={'message': 'help'}, headers={'origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/runs/nonexistent').status_code == 404
    assert client.get('/').status_code == 200


def test_playwright_retries_and_expected_failure(tmp_path):
    path = tmp_path / 'results.json'
    path.write_text(json.dumps({'suites': [{'title': 'web', 'specs': [{'id': '1', 'title': 'login', 'tests': [{'status': 'flaky', 'results': [{'status': 'failed'}, {'status': 'passed'}]}, {'status': 'expected', 'results': [{'status': 'failed'}]}]}]}]}))
    results = playwright_report(path)
    assert results[0]['status'] == 'flaky'
    assert results[0]['attempts'] == 2
    assert results[1]['status'] == 'pass'
    path.write_text('{"errors": [{"message":"setup failed"}]}')
    with pytest.raises(ValueError):
        playwright_report(path)


def test_invalid_robot_report(tmp_path):
    path = tmp_path / 'output.xml'
    path.write_text('<robot><suite><test name="broken"/></suite></robot>')
    with pytest.raises(ValueError):
        robot_report(path)


def test_restart_marks_incomplete_run(tmp_path):
    folder = tmp_path / ('a' * 32)
    folder.mkdir()
    (folder / 'run.json').write_text(json.dumps({'id': folder.name, 'status': 'running'}))
    runner = Runner(ROOT / 'relay.example.json', tmp_path)
    assert runner.get(folder.name)['status'] == 'interrupted'
