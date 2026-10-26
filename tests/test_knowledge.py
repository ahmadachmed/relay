from datetime import date, timedelta
import json
import hashlib
import pytest
from relay.knowledge import load_knowledge, MAX_FILE


def provision(tmp_path, monkeypatch):
    root = tmp_path/'knowledge'
    project = root/'api'
    project.mkdir(parents=True)
    entry = dict(id='overview',path='overview.md',project_id='api',environments=['uat'],verification='verified',owner='QA',source='maintained by QA',type='overview',revision='1',reviewed_at=date.today().isoformat())
    (project/'overview.md').write_text('Expected HTTP 200. password="private-value"')
    entry['sha256']=hashlib.sha256((project/'overview.md').read_bytes()).hexdigest()
    (project/'manifest.json').write_text(json.dumps([entry]))
    monkeypatch.setenv('RELAY_KNOWLEDGE_ROOT',str(root))
    return project, entry


def test_safe_snapshot_refresh_and_project_scope(tmp_path, monkeypatch):
    project, entry = provision(tmp_path,monkeypatch)
    first = load_knowledge('api','uat')
    assert len(first['sources']) == 1
    (project/'overview.md').write_text('Changed contract.')
    assert not load_knowledge('api','uat')['sources']
    entry['sha256']=hashlib.sha256((project/'overview.md').read_bytes()).hexdigest()
    entry['revision']='2'
    (project/'manifest.json').write_text(json.dumps([entry]))
    second = load_knowledge('api','uat')
    assert first['sources'][0]['id'] != second['sources'][0]['id']
    assert not load_knowledge('mobile','uat')['sources']
    assert not load_knowledge('../api','uat')['sources']
    assert not load_knowledge('api','dev')['sources']
    assert not load_knowledge('api',None)['sources']

@pytest.mark.parametrize('change', [dict(path='../outside.md'),dict(path='.env'),dict(project_id='mobile'),dict(verification='unknown'),dict(reviewed_at=(date.today()-timedelta(days=181)).isoformat()),dict(reviewed_at=(date.today()+timedelta(days=1)).isoformat()),dict(owner='')])
def test_invalid_or_stale_document_is_omitted(tmp_path,monkeypatch,change):
    project, entry=provision(tmp_path,monkeypatch)
    (project/'manifest.json').write_text(json.dumps([entry|change]))
    result=load_knowledge('api','uat')
    assert result['sources']==[]
    assert result['diagnostics']

@pytest.mark.parametrize('kind',['file','project','root'])
def test_symlink_rejected(tmp_path,monkeypatch,kind):
    project, entry=provision(tmp_path,monkeypatch)
    if kind=='file':
        outside=tmp_path/'outside.md';outside.write_text('private')
        (project/'overview.md').unlink();(project/'overview.md').symlink_to(outside)
    elif kind=='project':
        renamed=project.with_name('actual');project.rename(renamed);project.symlink_to(renamed,target_is_directory=True)
    else:
        alias=tmp_path/'alias';alias.symlink_to(project.parent,target_is_directory=True);monkeypatch.setenv('RELAY_KNOWLEDGE_ROOT',str(alias))
    assert not load_knowledge('api','uat')['sources']


def test_limits_and_invalid_manifest(tmp_path,monkeypatch):
    project, entry=provision(tmp_path,monkeypatch)
    (project/'overview.md').write_bytes(b'x'*(MAX_FILE+1))
    assert not load_knowledge('api','uat')['sources']
    (project/'manifest.json').write_text('{}')
    assert load_knowledge('api','uat')['diagnostics']
