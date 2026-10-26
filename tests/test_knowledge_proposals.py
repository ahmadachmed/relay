import json
from datetime import date, timedelta
import pytest
from relay.knowledge_proposals import KnowledgeSuggestion, Approval, DraftEdit, RollbackDraft
from relay.knowledge import load_sut_knowledge
from relay.execution import PlanError
from relay import knowledge_revisions as revisions
from test_assistant import harness, evidence, post, answer, RUN, ART
from test_scoped_knowledge import bundle


@pytest.fixture
def proposals(harness, monkeypatch, tmp_path):
    app, client, fake = harness
    evidence(app, monkeypatch)
    saved = app.state.monitor.execution.get(RUN)
    saved['plan']['sut_id'] = 'service-a'
    monkeypatch.setattr(app.state.monitor.execution, 'get', lambda _: saved)
    root = tmp_path/'private'
    root.mkdir()
    monkeypatch.setenv('RELAY_KNOWLEDGE_ROOT', str(root))
    return app.state.assistant.knowledge_proposals, root, client, fake


def draft(store, text='A repeated setup failure can block dependent scenarios.', path='troubleshooting.md'):
    return store.draft(RUN, KnowledgeSuggestion(category='sut', path=path, text=text, rationale='Reusable troubleshooting hypothesis; verify first.', artifact_ids=[ART]), [ART])


def approve(store, proposal, verified=True):
    return store.approve(RUN, proposal['id'], Approval(expected_hash=proposal['hash'], confirmed=True, verified=verified, actor='QA operator'))


def test_draft_unverified_approval_and_verified_apply(proposals):
    store, root, _, _ = proposals
    proposal = draft(store)
    assert store.execution.path.stat().st_mode & 0o777 == 0o600
    assert not (root/'sut').exists()
    assert not load_sut_knowledge('service-a', 'uat')['sources']
    saved = approve(store, proposal, False)
    assert saved['status'] == 'stored_unverified'
    assert not (root/'sut').exists()
    applied = approve(store, proposal)
    source = load_sut_knowledge('service-a', 'uat')['sources'][0]
    assert source['text'] == proposal['text']
    assert source['document_id'] == 'troubleshooting'
    assert source['bundle_revision'] == applied['applied_revision']
    assert approve(store, proposal)['applied_revision'] == applied['applied_revision']
    assert not load_sut_knowledge('service-a', 'dev')['sources']


def test_exact_approval_edit_reject_and_cross_run(proposals):
    store, _, _, _ = proposals
    proposal = draft(store)
    edited = store.edit(RUN, proposal['id'], DraftEdit(expected_hash=proposal['hash'], text='Edited hypothesis requiring review.', rationale='Clarify uncertainty.'))
    with pytest.raises(PlanError):
        approve(store, proposal)
    with pytest.raises(PlanError):
        store.approve(RUN, edited['id'], Approval(expected_hash=proposal['hash'], confirmed=True, actor='QA'))
    rejected = store.reject(RUN, edited['id'], edited['hash'])
    assert rejected['status'] == 'rejected'
    with pytest.raises(PlanError):
        approve(store, edited)
    with store.execution.connect() as db, pytest.raises(PlanError):
        store.get(db, 'b'*32, proposal['id'])


def test_stale_proposal_and_external_edit(proposals):
    store, root, _, _ = proposals
    path, manifest = bundle(root, 'sut', 'service-a')
    first = draft(store)
    second = draft(store, 'Another hypothesis.')
    approve(store, first)
    with pytest.raises(PlanError, match='Knowledge changed'):
        approve(store, second)
    # Managed revisions are authoritative after first commit; flat-file edits
    # cannot mix new bytes with the committed manifest.
    (path/'overview.md').write_text('Unreviewed local edit')
    assert len(load_sut_knowledge('service-a', 'uat')['sources']) == 2


def test_rollback_preserves_review_date_and_history(proposals):
    store, root, _, _ = proposals
    path, manifest = bundle(root, 'sut', 'service-a')
    manifest['documents'][0]['reviewed_at'] = (date.today()-timedelta(days=181)).isoformat()
    (path/'manifest.json').write_text(json.dumps(manifest))
    first = draft(store)
    approve(store, first)
    history = store.history(RUN, 'sut')
    imported = history['revisions'][-1]['revision']
    rollback = store.rollback_draft(RUN, RollbackDraft(category='sut', revision=imported, expected_hash=history['current_hash']))
    assert rollback['documents'][0]['reviewed_at'] == manifest['documents'][0]['reviewed_at']
    applied = approve(store, rollback)
    assert not load_sut_knowledge('service-a', 'uat')['sources']
    assert len(store.history(RUN, 'sut')['revisions']) == 3
    assert approve(store, rollback)['applied_revision'] == applied['applied_revision']


def test_crash_before_pointer_and_retry_after_pointer(proposals, monkeypatch):
    store, _, _, _ = proposals
    first = draft(store)
    original = revisions.atomic_json
    def fail_pointer(fd, name, value):
        if name == revisions.POINTER:
            raise OSError('simulated crash')
        return original(fd, name, value)
    monkeypatch.setattr(revisions, 'atomic_json', fail_pointer)
    with pytest.raises(PlanError):
        approve(store, first)
    assert not load_sut_knowledge('service-a', 'uat')['sources']
    monkeypatch.setattr(revisions, 'atomic_json', original)
    save = store.save
    def fail_db(db, record):
        if record['status'] == 'applied':
            raise OSError('interrupted DB update after pointer commit')
        save(db, record)
    monkeypatch.setattr(store, 'save', fail_db)
    with pytest.raises(PlanError):
        approve(store, first)
    assert load_sut_knowledge('service-a', 'uat')['sources']
    monkeypatch.setattr(store, 'save', save)
    assert approve(store, first)['status'] == 'applied'
    assert len(store.history(RUN, 'sut')['revisions']) == 2


@pytest.mark.parametrize('target', ['scope', 'pointer', 'lock'])
def test_symlink_writes_rejected(proposals, target):
    store, root, _, _ = proposals
    proposal = draft(store)
    path = root/'sut'/'service-a'
    path.mkdir(parents=True)
    outside = root/'outside'
    outside.mkdir()
    if target == 'scope':
        path.rmdir()
        path.symlink_to(outside, target_is_directory=True)
    else:
        (path/('.relay-current.json' if target == 'pointer' else '.relay-lock')).symlink_to(outside/'secret')
    with pytest.raises(PlanError):
        approve(store, proposal)
    assert list(outside.iterdir()) == []


def test_path_credentials_and_wrong_evidence_rejected(proposals):
    store, _, _, _ = proposals
    with pytest.raises(ValueError):
        draft(store, path='../overview.md')
    with pytest.raises(PlanError):
        draft(store, text='password=private-value')
    with pytest.raises(PlanError):
        store.draft(RUN, KnowledgeSuggestion(category='sut', path='overview.md', text='Unknown.', rationale='Test', artifact_ids=['other-run']), [ART])


def test_assistant_proposal_opt_in_and_api_approval(proposals):
    store, root, client, fake = proposals
    fake.result = answer(action='analyze', knowledge_suggestion=dict(category='sut', path='troubleshooting.md', text='Setup failures may block child tests; check the shared report.', rationale='Hypothesis from setup evidence.', artifact_ids=[ART]))
    assert post(client, run_id=RUN, include_failure_details=True, include_sut_knowledge=True).status_code == 502
    response = post(client, run_id=RUN, include_failure_details=True, include_sut_knowledge=True, propose_knowledge=True)
    assert response.status_code == 200, response.text
    proposal = response.json()['knowledge_proposal']
    assert not (root/'sut').exists()
    base = f'/api/remote-runs/{RUN}/knowledge-proposals/{proposal["id"]}'
    body = dict(expected_hash=proposal['hash'], confirmed=True, verified=False, actor='QA')
    assert client.post(base+'/approve', json=body).json()['status'] == 'stored_unverified'
    assert not (root/'sut').exists()
    body['verified'] = True
    assert client.post(base+'/approve', json=body).json()['status'] == 'applied'
    assert client.get(f'/api/remote-runs/{RUN}/knowledge-proposals').json()[0]['status'] == 'applied'
    assert client.get(f'/api/remote-runs/{RUN}/knowledge-history?category=sut').status_code == 200


def test_unshared_failure_cannot_back_proposal(proposals):
    _, _, client, fake = proposals
    fake.result = answer(action='analyze', knowledge_suggestion=dict(category='sut', path='overview.md', text='A tentative rule.', rationale='Hypothesis.', artifact_ids=[ART]))
    assert post(client, run_id=RUN, include_sut_knowledge=True, propose_knowledge=True).status_code == 422


@pytest.mark.parametrize('automation,sut', [(True, False), (False, True), (True, True)])
def test_both_categories_and_snapshot_identity(proposals, automation, sut):
    store, root, client, fake = proposals
    bundle(root, 'automation', 'api')
    bundle(root, 'sut', 'service-a')
    fake.result = answer(action='analyze')
    result = post(client, run_id=RUN, context={'project_id':'web','pipeline_id':'smoke'}, include_automation_knowledge=automation, include_sut_knowledge=sut)
    assert result.status_code == 200
    context = fake.calls[-1]
    assert bool(context['project_knowledge']) == automation
    assert bool(context['sut_knowledge']) == sut
    assert context['selected_run']['identity']['sut_id'] == 'service-a'
    if sut:
        assert context['sut_knowledge']['sources'][0]['scope_id'] == 'service-a'
        assert 'text' not in result.json()['facts']['sut_knowledge']['sources'][0]
    assert len(json.dumps(context).encode()) <= 48000


def test_optional_repository_provenance(proposals):
    _, root, _, _ = proposals
    path, manifest = bundle(root, 'sut', 'service-a')
    manifest['documents'][0].update(source_repository='example/service', source_path='docs/contract.md', source_commit='a'*40)
    (path/'manifest.json').write_text(json.dumps(manifest))
    source = load_sut_knowledge('service-a', 'uat')['sources'][0]
    assert source['source_commit'] == 'a'*40
    manifest['documents'][0]['source_commit'] = 'main'
    (path/'manifest.json').write_text(json.dumps(manifest))
    assert not load_sut_knowledge('service-a', 'uat')['sources']


def test_file_limits_and_external_change_conflict(proposals):
    store, root, _, _ = proposals
    path, manifest = bundle(root, 'sut', 'service-a')
    proposal = draft(store)
    (path/'overview.md').write_text('Changed without a new approved manifest hash.')
    with pytest.raises(PlanError):
        approve(store, proposal)
    assert not (path/revisions.POINTER).exists()


def test_lock_conflict_and_stale_rollback(proposals):
    store, _, _, _ = proposals
    proposal = draft(store)
    with revisions.scope_directory('sut','service-a',create=True) as fd, revisions.bundle_lock(fd):
        with pytest.raises(PlanError):
            approve(store, proposal)
    approve(store, proposal)
    history = store.history(RUN, 'sut')
    target = history['revisions'][-1]['revision']
    rollback = store.rollback_draft(RUN, RollbackDraft(category='sut',revision=target,expected_hash=history['current_hash']))
    approve(store, draft(store, text='A revised and independently reviewed hypothesis.'))
    with pytest.raises(PlanError, match='Knowledge changed'):
        approve(store, rollback)


@pytest.mark.parametrize('long_chat', [False, True])
def test_two_full_unicode_bundles_fit_final_budget(proposals, long_chat):
    import hashlib
    _, root, client, fake = proposals
    for category, scope in [('automation','api'), ('sut','service-a')]:
        path, manifest = bundle(root, category, scope)
        template = manifest['documents'][0]
        documents = []
        for name in ['overview.md', 'business-rules.md', 'troubleshooting.md']:
            text = '界' * 2000
            (path/name).write_text(text)
            documents.append(dict(template, path=name, sha256=hashlib.sha256(text.encode()).hexdigest()))
        manifest['documents'] = documents
        (path/'manifest.json').write_text(json.dumps(manifest))
    fake.result = answer(action='analyze')
    extra = {'history':[{'role':'user','text':'x'*3000} for _ in range(8)], 'message':'界'*1000} if long_chat else {}
    response = post(client, run_id=RUN, include_automation_knowledge=True, include_sut_knowledge=True, **extra)
    assert response.status_code == 200, response.text
    context = fake.calls[-1]
    assert len(json.dumps(context).encode()) <= 48000
    sent = [source for key in ['project_knowledge','sut_knowledge'] for source in (context.get(key) or {}).get('sources', [])]
    assert sum(len(json.dumps(source).encode()) for source in sent) <= 20000
    assert len(sent) < 6
    local = [source for key in ['knowledge','sut_knowledge'] for source in (response.json()['facts'].get(key) or {}).get('sources', [])]
    assert {source['id'] for source in local} == {source['id'] for source in sent}
    assert all('text' not in source for source in local)
