"""Persist collection diagnostics and serialize per-run report imports."""
import asyncio
import json
import logging
import time
from uuid import uuid4

from .execution import PlanError
from .providers import ProviderError
from .providers.bitbucket_evidence import BitbucketEvidenceProvider

logger = logging.getLogger(__name__)


class EvidenceCollector:
    def __init__(self, store, provider_factory=None):
        self.store = store
        self.execution = store.execution
        self.provider_factory = provider_factory or BitbucketEvidenceProvider.from_environment
        with self.execution.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS evidence_collection ('
                       'run_id TEXT PRIMARY KEY, lease_until REAL NOT NULL, token TEXT, record TEXT NOT NULL)')

    def get(self, run_id):
        self.store.monitor.get(run_id)
        with self.execution.connect() as db:
            row = db.execute('SELECT record FROM evidence_collection WHERE run_id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else dict(run_id=run_id, status='not_collected', reports=[])

    async def collect(self, run_id):
        run = self.store.monitor.get(run_id)
        if not run['terminal']:
            raise PlanError('run_not_terminal', 'Refresh remote status and wait for pipeline completion before collecting reports')
        plan = self.execution.get(run_id)
        specs = plan['plan']['project']['reports']
        if len(specs) > 20:
            raise PlanError('too_many_reports', 'At most 20 configured reports can be collected per run')
        token, now = uuid4().hex, time.time()
        pending = dict(run_id=run_id, status='collecting', reports=[], attempted_at=now)
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT lease_until,record FROM evidence_collection WHERE run_id=?', (run_id,)).fetchone()
            if row and row[0] > now:
                return json.loads(row[1])
            # Each report has a 60 s timeout; the overall collection is capped at 65 s below.
            db.execute('INSERT INTO evidence_collection VALUES (?,?,?,?) ON CONFLICT(run_id) '
                       'DO UPDATE SET lease_until=excluded.lease_until,token=excluded.token,record=excluded.record',
                       (run_id, now + 90, token, json.dumps(pending)))
        results = []
        try:
            async with asyncio.timeout(65):
                for spec in specs:
                    result = dict(path=spec['path'], status='not_configured', error=None)
                    if spec.get('download_name'):
                        try:
                            # Immutable slots already imported are not fetched/replaced again.
                            existing = next((a for a in self.store.summary(run_id)['artifacts']
                                             if a['name'] == spec['path'] and a.get('id')), None)
                            if existing:
                                result.update(status=existing['availability'], artifact_id=existing['id'])
                            else:
                                data = await self.provider_factory().fetch_report(plan, spec)
                                # A late worker must not mutate evidence after losing its lease.
                                with self.execution.connect() as db:
                                    owner = db.execute('SELECT token FROM evidence_collection WHERE run_id=?', (run_id,)).fetchone()
                                if owner[0] != token:
                                    return self.get(run_id)
                                artifact = self.store.upload(run_id, spec['path'], data, source='bitbucket_downloads')
                                result.update(status='imported', artifact_id=artifact['id'], parse_status=artifact['parse_status'])
                        except ProviderError as exc:
                            result.update(status='missing' if exc.code == 'missing_evidence' else 'failed',
                                          error=dict(code=exc.code, message=str(exc)))
                        except PlanError as exc:
                            result.update(status='failed', error=dict(code=exc.code, message=str(exc)))
                    results.append(result)
        except TimeoutError:
            pending['error'] = dict(code='timeout', message='Collection timed out; completed imports are retained.')
        except Exception:
            pending['error'] = dict(code='collection_failed', message='Collection failed; completed imports are retained.')
        pending.update(status='finished', reports=results, completed_at=time.time())
        with self.execution.connect() as db:
            db.execute('UPDATE evidence_collection SET record=?,lease_until=0 WHERE run_id=? AND token=?',
                       (json.dumps(pending), run_id, token))
        return self.get(run_id)

    async def maintenance(self):
        while True:
            try:
                # Sweeps delete expired payloads even when no evidence API is accessed.
                self.store.expire()
            except Exception:
                logger.error('Evidence retention sweep failed; retry scheduled.')
            await asyncio.sleep(60)
