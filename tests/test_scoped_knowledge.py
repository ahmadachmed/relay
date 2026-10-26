from datetime import date
import hashlib
import json
import pytest
from pydantic import ValidationError
from relay.contracts import Pipeline, PlanRequest, Registry
from relay.registry import resolve_plan
from relay.knowledge import load_automation_knowledge, load_sut_knowledge


def bundle(root, category, scope):
    path = root/category/scope
    path.mkdir(parents=True)
    body = b'Reviewed synthetic service contract.'
    (path/'overview.md').write_bytes(body)
    entry = dict(path='overview.md', environments=['uat'], verification='verified', owner='QA',
                 source='synthetic fixture', type='overview', revision='1',
                 reviewed_at=date.today().isoformat(), sha256=hashlib.sha256(body).hexdigest())
    manifest = dict(schema_version=2, category=category, scope_id=scope, documents=[entry])
    (path/'manifest.json').write_text(json.dumps(manifest))
    return path, manifest


def test_categories_and_automatic_ids(tmp_path, monkeypatch):
    monkeypatch.setenv('RELAY_KNOWLEDGE_ROOT', str(tmp_path))
    bundle(tmp_path, 'automation', 'api')
    bundle(tmp_path, 'sut', 'api')
    automation = load_automation_knowledge('api', 'uat')['sources'][0]
    sut = load_sut_knowledge('api', 'uat')['sources'][0]
    assert automation['document_id'] == sut['document_id'] == 'overview'
    assert automation['id'] != sut['id']
    assert sut['category'] == 'sut'
    assert sut['scope_id'] == 'api'
    assert not load_sut_knowledge('api', 'dev')['sources']


@pytest.mark.parametrize('change', [dict(category='automation'), dict(scope_id='other'), dict(schema_version=1)])
def test_scope_mismatch(tmp_path, monkeypatch, change):
    monkeypatch.setenv('RELAY_KNOWLEDGE_ROOT', str(tmp_path))
    path, manifest = bundle(tmp_path, 'sut', 'service-a')
    (path/'manifest.json').write_text(json.dumps(manifest | change))
    assert not load_sut_knowledge('service-a', 'uat')['sources']


@pytest.mark.parametrize('level', ['category', 'scope', 'manifest', 'document'])
def test_symlinks(tmp_path, monkeypatch, level):
    monkeypatch.setenv('RELAY_KNOWLEDGE_ROOT', str(tmp_path))
    path, _ = bundle(tmp_path, 'sut', 'service-a')
    target = {'category': path.parent, 'scope': path, 'manifest': path/'manifest.json', 'document': path/'overview.md'}[level]
    renamed = target.with_name(target.name + '-actual')
    target.rename(renamed)
    target.symlink_to(renamed, target_is_directory=renamed.is_dir())
    assert not load_sut_knowledge('service-a', 'uat')['sources']


def test_new_invalid_bundle_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv('RELAY_KNOWLEDGE_ROOT', str(tmp_path))
    path, _ = bundle(tmp_path, 'automation', 'api')
    legacy = tmp_path/'api'
    legacy.mkdir()
    (legacy/'manifest.json').write_text('[]')
    (path/'manifest.json').write_text('invalid')
    result = load_automation_knowledge('api', 'uat')
    assert not result['sources']
    assert not any('deprecated' in item for item in result['diagnostics'])


def test_duplicate_paths_and_manual_id(tmp_path, monkeypatch):
    monkeypatch.setenv('RELAY_KNOWLEDGE_ROOT', str(tmp_path))
    path, manifest = bundle(tmp_path, 'sut', 'service-a')
    manifest['documents'] *= 2
    (path/'manifest.json').write_text(json.dumps(manifest))
    result = load_sut_knowledge('service-a', 'uat')
    assert len(result['sources']) == 1
    assert result['diagnostics']
    manifest['documents'] = [manifest['documents'][0] | {'id': 'manual'}]
    (path/'manifest.json').write_text(json.dumps(manifest))
    assert not load_sut_knowledge('service-a', 'uat')['sources']


@pytest.mark.parametrize('ids', [['../service'], [''], ['service', 'service'], ['a'*101], ['a']*17])
def test_invalid_sut_ids(ids):
    with pytest.raises(ValidationError):
        Pipeline(selector='smoke', sut_ids=ids)


def test_saved_sut_roundtrip_and_old_plan():
    registry = Registry.model_validate({'projects': {'api': dict(repository='example/automation', default_branch='main',
        allowed_branches=['main'], pipelines={'smoke': dict(selector='smoke', sut_ids=['service-a'])})}})
    plan = resolve_plan(registry, PlanRequest(project_id='api', pipeline_id='smoke', sut_id='service-a'))
    assert type(plan).model_validate_json(plan.model_dump_json()).sut_id == 'service-a'
    old = plan.model_dump(mode='json')
    old.pop('sut_id')
    assert type(plan).model_validate_json(json.dumps(old)).sut_id is None
    with pytest.raises(ValueError):
        resolve_plan(registry, PlanRequest(project_id='api', pipeline_id='smoke', sut_id='other'))
