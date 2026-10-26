"""Operator-reviewed drafts; models can propose data but cannot apply it."""
from datetime import date, datetime, timezone
import difflib
import hashlib
import json
import os
import re
import stat
from uuid import uuid4
from pydantic import Field, model_validator
from .contracts import Contract
from .execution import PlanError
from .knowledge import ALLOWED, MAX_FILE
from . import knowledge_revisions as revisions


class KnowledgeSuggestion(Contract):
    category: str = Field(pattern=r'^(automation|sut)$')
    path: str = Field(max_length=100)
    text: str = Field(min_length=1, max_length=8000)
    rationale: str = Field(min_length=1, max_length=1500)
    artifact_ids: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def allowed_content(self):
        if self.path not in ALLOWED or len(self.text.encode()) > MAX_FILE:
            raise ValueError('Invalid document path or size')
        if '\x00' in self.text or 'PRIVATE KEY-----' in self.text:
            raise ValueError('Excluded content')
        return self


class DraftEdit(Contract):
    expected_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    text: str = Field(min_length=1, max_length=8000)
    rationale: str = Field(min_length=1, max_length=1500)


class ProposalReference(Contract):
    expected_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class Approval(Contract):
    expected_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmed: bool
    verified: bool = False
    actor: str = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def reviewer_label(self):
        if not self.actor.strip() or any(ord(char) < 32 for char in self.actor):
            raise ValueError('Provide a reviewer label without control characters')
        return self


class RollbackDraft(Contract):
    category: str = Field(pattern=r'^(automation|sut)$')
    revision: str = Field(pattern=r'^[a-f0-9]{32}$')
    expected_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


def safe_content(text):
    # Same best-effort redaction boundary as AI context; never silently approve
    # a credential-bearing draft. Operator must remove sensitive values first.
    from .assistant import redact
    if redact(text) != text or re.search(r'(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*["\']?[^\s"\']+', text):
        raise PlanError('knowledge_sensitive', 'Remove sensitive values before saving knowledge.', 422)
    if len(text.encode()) > MAX_FILE or '\x00' in text or 'PRIVATE KEY-----' in text:
        raise PlanError('knowledge_content', 'Document exceeds limits or contains excluded content.', 422)


def safe_error(function):
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise PlanError('knowledge_unavailable', 'Private knowledge storage is unavailable or invalid. Check configuration and migrate legacy bundles before editing.', 422) from None
    return wrapped


class KnowledgeProposals:
    def __init__(self, execution, monitor):
        self.execution, self.monitor = execution, monitor
        with execution.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS knowledge_proposals (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, record TEXT NOT NULL)')
        handle = os.open(execution.path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                raise ValueError('Proposal database must be a regular file')
            os.fchmod(handle, 0o600)
        finally:
            os.close(handle)

    def scope(self, run_id, category):
        self.monitor.get(run_id)
        plan = self.execution.get(run_id)['plan']
        scope = plan['project_id'] if category == 'automation' else plan.get('sut_id')
        specs = plan['project']['pipelines'][plan['pipeline_id']]['parameters']
        envs = [plan['parameters'].get(key) for key, spec in specs.items() if spec['kind'] == 'environment']
        if not scope or len(envs) != 1 or not envs[0]:
            raise PlanError('knowledge_scope', 'Selected run needs an explicit scope and environment.', 422)
        return scope, envs[0]

    def list(self, run_id):
        self.monitor.get(run_id)
        with self.execution.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT record FROM knowledge_proposals WHERE run_id=? ORDER BY rowid DESC LIMIT 30', (run_id,))]

    def get(self, db, run_id, proposal_id):
        row = db.execute('SELECT record FROM knowledge_proposals WHERE id=? AND run_id=?', (proposal_id, run_id)).fetchone()
        if not row:
            raise PlanError('not_found', 'Unknown knowledge proposal', 404)
        return json.loads(row[0])

    def save(self, db, record):
        db.execute('INSERT OR REPLACE INTO knowledge_proposals VALUES (?,?,?)', (record['id'], record['run_id'], json.dumps(record)))

    @safe_error
    def snapshot(self, category, scope):
        try:
            with revisions.scope_directory(category, scope) as fd:
                return revisions.baseline(fd, category, scope)
        except FileNotFoundError:
            # Root must exist. Only absent category/scope means an empty bundle.
            root = os.environ.get('RELAY_KNOWLEDGE_ROOT', '')
            if not os.path.isdir(root):
                raise ValueError('Missing root')
            if category == 'automation' and os.path.lexists(os.path.join(root, scope)):
                raise ValueError('Legacy bundle must be migrated')
            return dict(category=category, scope_id=scope, revision=None, documents=[], hash=revisions.digest([]))

    def draft(self, run_id, suggestion, allowed_artifacts, before=None):
        if not set(suggestion.artifact_ids) <= set(allowed_artifacts):
            raise PlanError('knowledge_citation', 'Knowledge proposal must cite evidence shared from this run.', 422)
        safe_content(suggestion.text)
        scope, environment = self.scope(run_id, suggestion.category)
        before = before or self.snapshot(suggestion.category, scope)
        old = next((doc['text'] for doc in before['documents'] if doc['path'] == suggestion.path), '')
        record = dict(id=uuid4().hex, run_id=run_id, category=suggestion.category, scope_id=scope,
                      path=suggestion.path, document_id=suggestion.path.rsplit('.', 1)[0], text=suggestion.text,
                      rationale=suggestion.rationale, artifact_ids=suggestion.artifact_ids, environment=environment,
                      base_hash=before['hash'], base_revision=before['revision'], operation='update' if old else 'create',
                      status='draft', verification='unverified', source='AI proposal; operator review required',
                      created_at=datetime.now(timezone.utc).isoformat())
        record['diff'] = '\n'.join(difflib.unified_diff(old.splitlines(), suggestion.text.splitlines(), fromfile='current', tofile='proposed', lineterm=''))
        record['hash'] = revisions.digest(record)
        with self.execution.connect() as db:
            self.save(db, record)
        return record

    def edit(self, run_id, proposal_id, body):
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.get(db, run_id, proposal_id)
            if old['hash'] != body.expected_hash or old['status'] not in ('draft', 'stored_unverified') or old['operation'] == 'rollback':
                raise PlanError('knowledge_conflict', 'Proposal changed or cannot be edited; reload it.', 409)
            suggestion = KnowledgeSuggestion(category=old['category'], path=old['path'], text=body.text, rationale=body.rationale, artifact_ids=old['artifact_ids'])
            # New immutable draft; retain original base for conflict detection.
            safe_content(body.text)
            record = dict(old, id=uuid4().hex, text=suggestion.text, rationale=suggestion.rationale, status='draft', supersedes=old['id'], created_at=datetime.now(timezone.utc).isoformat())
            record['diff'] = '\n'.join(difflib.unified_diff(old['text'].splitlines(), body.text.splitlines(), fromfile='previous draft', tofile='edited draft', lineterm=''))
            record.pop('hash')
            record['hash'] = revisions.digest(record)
            self.save(db, dict(old, status='superseded'))
            self.save(db, record)
            return record

    def reject(self, run_id, proposal_id, expected_hash):
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record = self.get(db, run_id, proposal_id)
            if record['hash'] != expected_hash or record['status'] not in ('draft', 'stored_unverified', 'rejected'):
                raise PlanError('knowledge_conflict', 'Proposal changed; reload it.', 409)
            record['status'] = 'rejected'
            record['rejected_at'] = datetime.now(timezone.utc).isoformat()
            self.save(db, record)
            return record

    @safe_error
    def approve(self, run_id, proposal_id, body):
        if not body.confirmed:
            raise PlanError('knowledge_confirmation', 'Explicit approval of the reviewed proposal is required.', 422)
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record = self.get(db, run_id, proposal_id)
            if record['hash'] != body.expected_hash or record['status'] not in ('draft', 'stored_unverified', 'applied'):
                raise PlanError('knowledge_conflict', 'Proposal changed; reload it.', 409)
            if record['status'] == 'applied':
                return record
            scope, environment = self.scope(run_id, record['category'])
            if scope != record['scope_id'] or environment != record['environment']:
                raise PlanError('knowledge_scope', 'Saved run scope does not match the draft.', 409)
            if not body.verified:
                recovered = self.snapshot(record['category'], scope)
                if recovered.get('proposal_id') == record['id']:
                    record.update(status='applied', verification='operator_verified', applied_revision=recovered['revision'],
                                  applied_hash=recovered['hash'], approved_by=recovered['actor'], approved_at=recovered['created_at'])
                    self.save(db, record)
                    return record
                record.update(status='stored_unverified', verification='unverified', stored_by=body.actor, stored_at=datetime.now(timezone.utc).isoformat())
                self.save(db, record)
                return record
            if record['operation'] != 'rollback':
                safe_content(record['text'])
            with revisions.scope_directory(record['category'], scope, create=True) as fd, revisions.bundle_lock(fd):
                before = revisions.baseline(fd, record['category'], scope)
                if before.get('proposal_id') == record['id']:
                    committed = before  # Idempotent recovery after pointer commit / DB interruption.
                else:
                    if before['hash'] != record['base_hash'] or before['revision'] != record['base_revision']:
                        raise PlanError('knowledge_conflict', 'Knowledge changed since this draft. Generate a new proposal.', 409)
                    if record['operation'] == 'rollback':
                        documents = record['documents']
                        for document in documents:
                            safe_content(document['text'])
                    else:
                        documents = [doc for doc in before['documents'] if doc['path'] != record['path']]
                        documents.append(dict(path=record['path'], text=record['text'], sha256=hashlib.sha256(record['text'].encode()).hexdigest(),
                                              environments=[environment], revision=uuid4().hex, owner=body.actor,
                                              source=f"Reviewed proposal {record['id']} from run {run_id}", type=record['document_id'],
                                              verification='verified', reviewed_at=date.today().isoformat()))
                    committed = revisions.commit(fd, before, documents, record['id'], body.actor,
                                                 dict(run_id=run_id, artifact_ids=record['artifact_ids'], before_hash=before['hash']), record['operation'])
                record.update(status='applied', verification='operator_verified', applied_revision=committed['revision'],
                              applied_hash=committed['hash'], approved_by=body.actor, approved_at=committed['created_at'])
                self.save(db, record)
                return record

    @safe_error
    def history(self, run_id, category):
        scope, _ = self.scope(run_id, category)
        snapshot = self.snapshot(category, scope)
        result = []
        if snapshot['revision'] is None:
            return {'current_hash': snapshot['hash'], 'revisions': []}
        with revisions.scope_directory(category, scope) as fd:
            while snapshot and len(result) < 30:
                result.append({key: value for key, value in snapshot.items() if key != 'documents'})
                previous = snapshot.get('previous')
                snapshot = revisions.read_revision(fd, previous, category, scope) if previous else None
        return {'current_hash': result[0]['hash'], 'revisions': result}

    @safe_error
    def rollback_draft(self, run_id, body):
        scope, env = self.scope(run_id, body.category)
        before = self.snapshot(body.category, scope)
        if before['hash'] != body.expected_hash:
            raise PlanError('knowledge_conflict', 'Knowledge changed; reload history.', 409)
        # Only revisions reachable from this scope's current history can be restored.
        history = self.history(run_id, body.category)['revisions']
        if body.revision not in {entry['revision'] for entry in history}:
            raise PlanError('knowledge_revision', 'Revision is not in the recent history of this scope.', 422)
        with revisions.scope_directory(body.category, scope) as fd:
            target = revisions.read_revision(fd, body.revision, body.category, scope)
        record = dict(id=uuid4().hex, run_id=run_id, category=body.category, scope_id=scope, environment=env,
                      path=None, document_id=None, text='', rationale=f"Restore revision {body.revision}; original review dates are preserved.",
                      artifact_ids=[], documents=target['documents'], operation='rollback', base_hash=before['hash'],
                      base_revision=before['revision'], status='draft', verification='unverified', target_revision=body.revision)
        record['diff'] = '\n'.join(difflib.unified_diff(json.dumps(before['documents'], indent=2).splitlines(), json.dumps(target['documents'], indent=2).splitlines(), fromfile='current bundle', tofile='rollback target', lineterm=''))
        record['hash'] = revisions.digest(record)
        with self.execution.connect() as db:
            self.save(db, record)
        return record
