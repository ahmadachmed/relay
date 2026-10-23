import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from relay.api import create_app
from relay.contracts import PlanRequest, Registry, Run, PipelineStep, TestResult as Result, Artifact
from relay.registry import load_registry, resolve_plan

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def registry():
    return load_registry(ROOT / 'projects.example.json')


@pytest.mark.parametrize('project,branch,report,parameters', [
    ('api', 'main', 'robot_xml', {}),
    ('mobile', 'master', 'robot_xml', {'PLATFORM': 'ios'}),
    ('web', 'main', 'junit_xml', {}),
])
def test_three_stacks(registry, project, branch, report, parameters):
    plan = resolve_plan(registry, PlanRequest(project_id=project, pipeline_id='smoke', parameters=parameters))
    assert plan.branch == branch
    assert plan.project.report_type == report
    assert plan.parameters['ENV'] == 'uat'
    assert plan.parameters['TAGS'] == 'smoke'
    assert Run(plan=plan).test_status == 'not_collected'


@pytest.mark.parametrize('changes,match', [
    ({'project_id':'missing'}, 'unknown project'),
    ({'pipeline_id':'missing'}, 'unknown pipeline'),
    ({'branch':'production'}, 'branch is not allowed'),
    ({'parameters':{'PASSWORD':'secret'}}, 'unknown pipeline parameter'),
    ({'parameters':{'ENV':'production'}}, 'outside allowed_values'),
    ({'project_id':'mobile'}, 'missing required parameter: PLATFORM'),
    ({'project_id':'mobile','parameters':{'PLATFORM':'windows'}}, 'outside allowed_values'),
])
def test_invalid_selections(registry, changes, match):
    request = {'project_id':'api', 'pipeline_id':'smoke', **changes}
    with pytest.raises(ValueError, match=match):
        resolve_plan(registry, PlanRequest(**request))


def test_snapshot_and_request_are_not_mutated(registry):
    request = PlanRequest(project_id='api', pipeline_id='smoke', branch='develop')
    plan = resolve_plan(registry, request)
    assert request.parameters == {}
    registry.projects['api'].repository = 'other/repo'
    assert plan.project.repository == 'example/api-automation'
    assert plan.branch == 'develop'


@pytest.mark.parametrize('field,value', [('token','secret'),('report_type','junit_xml'),('default_branch','missing'),('provider','github'),('repository','https://user:secret@example.com/repo')])
def test_invalid_project_config(registry, field, value):
    config = registry.model_dump()
    config['projects']['api'][field] = value
    with pytest.raises(ValidationError):
        Registry.model_validate(config)


def test_disabled_and_invalid_mobile_config(registry):
    registry.projects['web'].enabled = False
    with pytest.raises(ValueError, match='disabled'):
        resolve_plan(registry, PlanRequest(project_id='web', pipeline_id='smoke'))
    config = registry.model_dump()
    del config['projects']['mobile']['pipelines']['smoke']['parameters']['PLATFORM']
    with pytest.raises(ValueError, match='platform'):
        Registry.model_validate(config)


def test_invalid_default_and_strict_types(registry):
    config = registry.model_dump()
    config['projects']['api']['pipelines']['smoke']['parameters']['ENV']['default'] = 'prod'
    with pytest.raises(ValueError, match='default'):
        Registry.model_validate(config)
    with pytest.raises(ValidationError):
        PlanRequest(project_id='api', pipeline_id='smoke', parameters={'WORKERS':4})


def test_run_status_and_round_trip(registry):
    plan = resolve_plan(registry, PlanRequest(project_id='web', pipeline_id='smoke'))
    run = Run(plan=plan, pipeline_status='successful', test_status='failed',
              steps=[PipelineStep(id='step-1', name='Tests', status='successful')],
              tests=[Result(id='test-1', name='Login', status='fail', artifact_ids=['report'])],
              artifacts=[Artifact(id='report',name='results.xml',kind='junit_xml',availability='available',storage_key='run/report')])
    restored = Run.model_validate_json(run.model_dump_json())
    assert restored.pipeline_status == 'successful'
    assert restored.tests[0].status == 'fail'
    assert restored.tests[0].attempts is None


def test_api_validation_has_no_execution_side_effects(tmp_path):
    app = create_app(ROOT / 'relay.example.json', tmp_path)
    client = TestClient(app)
    assert len(client.get('/api/project-registry').json()['projects']) == 3
    r = client.post('/api/plans/validate', json={'project_id':'web','pipeline_id':'smoke'})
    assert r.status_code == 200
    assert r.json()['project']['stack'] == 'pytest_playwright'
    assert client.get('/api/runs').json() == []
    assert app.state.runner.active is None
    for payload in [dict(project_id='missing',pipeline_id='smoke'), dict(project_id='api',pipeline_id='smoke',token='secret')]:
        assert client.post('/api/plans/validate',json=payload).status_code == 422
