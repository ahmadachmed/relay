"""Durable preview/claim boundary for explicitly confirmed remote executions."""
import json
from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path

from .contracts import ExecutionPlan
from .registry import resolve_plan
from .providers import ProviderError


class PlanError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.status = code, status
        super().__init__(message)


class ExecutionService:
    def __init__(self, path, registry):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, record TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, plan_id):
        with self.connect() as db:
            row = db.execute('SELECT record FROM plans WHERE id=?', (plan_id,)).fetchone()
        if row is None:
            raise PlanError('not_found', 'Unknown plan', 404)
        return json.loads(row[0])

    def recovery_snapshot(self, plan_id):
        # Serialize with claim: a reader must not certify an old preview while
        # an in-flight writer is committing its trigger intent.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM plans WHERE id=?', (plan_id,)).fetchone()
            if row is None:
                raise PlanError('not_found', 'Unknown plan', 404)
            record = json.loads(row[0])
            if record['status'] == 'preview':
                record['expired_unclaimed'] = record['expires_at'] <= time.time()
            return record

    def save(self, record):
        with self.connect() as db:
            db.execute('UPDATE plans SET record=? WHERE id=?', (json.dumps(record), record['id']))

    async def preview(self, request, provider):
        plan = resolve_plan(self.registry, request)
        if plan.project.provider != 'bitbucket':
            raise PlanError('unsupported_provider', 'Provider is not installed', 501)
        probe = plan.project.model_copy(deep=True)
        probe.default_branch = plan.branch
        catalog = await provider.catalog(probe)
        if not any(p['id'] == plan.pipeline_id and p['available'] for p in catalog['pipelines']):
            raise PlanError('unavailable_selector', 'Selected pipeline is unavailable on this branch')
        record = dict(id=plan.id, plan=plan.model_dump(mode='json'), commit=catalog['commit'],
                      selector=plan.project.pipelines[plan.pipeline_id].selector,
                      expires_at=time.time() + 900, status='preview', pipeline=None, error=None)
        with self.connect() as db:
            db.execute('INSERT INTO plans VALUES (?,?)', (plan.id, json.dumps(record)))
        return record

    def claim(self, plan_id):
        # BEGIN IMMEDIATE serializes claims across threads, workers and restarts.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM plans WHERE id=?', (plan_id,)).fetchone()
            if row is None:
                raise PlanError('not_found', 'Unknown plan', 404)
            record = json.loads(row[0])
            if record['status'] != 'preview':
                return record, False
            if record['expires_at'] <= time.time():
                raise PlanError('expired_plan', 'Preview expired; create and review a new plan')
            plan = ExecutionPlan.model_validate_json(json.dumps(record['plan']))
            current = self.registry.projects.get(plan.project_id)
            if current is None or current != plan.project:
                raise PlanError('stale_plan', 'Project configuration changed; preview again')
            # Persist uncertainty BEFORE issuing POST. Never reset this on startup.
            record['attempted_at'] = time.time()
            record['status'] = 'unknown'
            record['error'] = 'Trigger may be in progress or interrupted; inspect Bitbucket before any new run.'
            db.execute('UPDATE plans SET record=? WHERE id=?', (json.dumps(record), plan_id))
        return record, True

    async def execute(self, plan_id, provider):
        record, claimed = self.claim(plan_id)
        if not claimed:
            return record
        try:
            pipeline = await provider.trigger(record)
        except ProviderError as exc:
            record['error'] = str(exc)
            if exc.code == 'trigger_rejected':
                record['status'] = 'rejected'
        else:
            record.update(status='submitted', pipeline=pipeline, error=None)
        self.save(record)
        return record


    async def reconcile(self, plan_id, pipeline_uuid, provider):
        record = self.get(plan_id)
        if record['status'] != 'unknown':
            raise PlanError('not_unknown', 'Only an unknown trigger can be reconciled')
        if time.time() - record['attempted_at'] < 70:
            raise PlanError('trigger_in_progress', 'Wait at least 70 seconds after the trigger before reconciliation')
        pipeline = await provider.reconcile(record, pipeline_uuid)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = json.loads(db.execute('SELECT record FROM plans WHERE id=?', (plan_id,)).fetchone()[0])
            if current['status'] != 'unknown':
                raise PlanError('state_changed', 'Trigger state changed; refresh the plan')
            current.update(status='submitted', pipeline=pipeline, error=None, reconciled=True)
            db.execute('UPDATE plans SET record=? WHERE id=?', (json.dumps(current), plan_id))
        return current
