"""Local SQLite monitoring with durable schedules, fenced leases and cancel intent."""
import asyncio
import json
import logging
import time
from uuid import uuid4

from .execution import PlanError
from .providers import ProviderError
from .providers.bitbucket_runs import BitbucketRunProvider

POLL_INTERVAL = 10
LEASE_SECONDS = 90
MONITOR_WINDOW = 24 * 60 * 60
logger = logging.getLogger(__name__)


class RunMonitor:
    def __init__(self, execution, provider_factory=None):
        self.execution = execution
        self.provider_factory = provider_factory or BitbucketRunProvider.from_environment
        with execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE IF NOT EXISTS remote_runs ('
                       'id TEXT PRIMARY KEY, record TEXT NOT NULL, next_poll REAL, '
                       'lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS remote_runs_due ON remote_runs(next_poll, lease_until)')
            db.execute('INSERT OR IGNORE INTO schema_migrations VALUES (1)')
        self.adopt()

    def adopt(self):
        # Includes pre-existing submitted plans and manually reconciled submissions.
        # A missing run row after a crash is recovered without ever triggering again.
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT p.id, p.record FROM plans p WHERE "
                "json_extract(p.record, '$.status')='submitted' AND NOT EXISTS "
                "(SELECT 1 FROM remote_runs r WHERE r.id=p.id) LIMIT 100").fetchall()
            now = time.time()
            for plan_id, raw in rows:
                plan = json.loads(raw)
                record = dict(id=plan_id, plan_id=plan_id, project_id=plan['plan']['project_id'], sut_id=plan['plan'].get('sut_id'),
                    pipeline=plan['pipeline'], pipeline_status='unknown', test_status='not_collected',
                    remote_state=None, result=None, terminal=False, steps=[], artifacts=[],
                    monitoring_status='active', monitoring_started_at=now, last_checked_at=None,
                    last_attempt_at=None, consecutive_errors=0, error=None,
                    cancel_status='none', cancel_error=None, cancel_requested_at=None)
                db.execute('INSERT OR IGNORE INTO remote_runs(id,record,next_poll) VALUES (?,?,?)',
                           (plan_id, json.dumps(record), now))

    def get(self, run_id):
        self.adopt()
        with self.execution.connect() as db:
            row = db.execute('SELECT record FROM remote_runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise PlanError('not_found', 'Unknown remote run', 404)
        return json.loads(row[0])

    def list(self, limit=50, offset=0):
        self.adopt()
        with self.execution.connect() as db:
            return [dict(json.loads(row[0]), sut_id=json.loads(row[1])['plan'].get('sut_id') if row[1] else None) for row in db.execute(
                'SELECT r.record,p.record FROM remote_runs r LEFT JOIN plans p ON p.id=r.id ORDER BY r.rowid DESC LIMIT ? OFFSET ?', (limit, offset))]

    def claim_poll(self, run_id, force=False):
        now = time.time()
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record,next_poll,lease_until FROM remote_runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise PlanError('not_found', 'Unknown remote run', 404)
            record = json.loads(row[0])
            if row[2] > now or (not force and (row[1] is None or row[1] > now)):
                return None
            if force:
                record['monitoring_started_at'] = now
            elif now - record['monitoring_started_at'] >= MONITOR_WINDOW:
                record['monitoring_status'] = 'timed_out'
                db.execute('UPDATE remote_runs SET record=?,next_poll=NULL WHERE id=?',
                           (json.dumps(record), run_id))
                return None
            token = uuid4().hex
            db.execute('UPDATE remote_runs SET record=?,lease_until=?,lease_token=? WHERE id=?',
                       (json.dumps(record), now + LEASE_SECONDS, token, run_id))
            return token

    async def refresh(self, run_id, force=True):
        self.get(run_id)
        token = self.claim_poll(run_id, force)
        if token is None:
            return self.get(run_id)
        snapshot, error = None, None
        try:
            # Bound the entire provider call even for replacement provider implementations.
            async with asyncio.timeout(65):
                snapshot = await self.provider_factory().status(self.execution.get(run_id))
        except ProviderError as exc:
            error = dict(code=exc.code, message=str(exc))
        except TimeoutError:
            error = dict(code='timeout', message='Remote monitoring timed out.')
        except Exception:
            error = dict(code='monitor_error', message='Remote monitoring failed; retry scheduled.')
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record,lease_token FROM remote_runs WHERE id=?', (run_id,)).fetchone()
            current = json.loads(row[0])
            if row[1] != token:
                return current  # A newer worker owns the result; discard this stale response.
            now = time.time()
            current['last_attempt_at'] = now
            if (current['cancel_status'] == 'requested'
                    and now - current['cancel_requested_at'] >= 70):
                current.update(cancel_status='unknown', cancel_error=dict(
                    code='cancel_unknown', message='Cancellation was interrupted; inspect remote status.'))
            if error:
                current['consecutive_errors'] += 1
                current.update(error=error, monitoring_status='retrying')
                delay = min(300, POLL_INTERVAL * 2 ** min(current['consecutive_errors'], 5))
                next_poll = now + delay
            else:
                current.update(snapshot)
                current.update(last_checked_at=now, consecutive_errors=0, error=None,
                    monitoring_status='complete' if snapshot['terminal'] else 'active')
                next_poll = None if snapshot['terminal'] else now + POLL_INTERVAL
                if current['cancel_status'] != 'none' and snapshot['terminal']:
                    current['cancel_status'] = ('confirmed' if snapshot['pipeline_status'] == 'stopped' else 'not_stopped')
            db.execute('UPDATE remote_runs SET record=?,next_poll=?,lease_until=0,lease_token=NULL WHERE id=?',
                       (json.dumps(current), next_poll, run_id))
        return current

    async def cancel(self, run_id):
        self.get(run_id)
        provider = self.provider_factory()  # Missing credentials must not consume the cancel intent.
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = json.loads(db.execute('SELECT record FROM remote_runs WHERE id=?', (run_id,)).fetchone()[0])
            if current['terminal'] or current['cancel_status'] != 'none':
                return current
            now = time.time()
            current.update(cancel_status='requested', cancel_requested_at=now,
                           monitoring_started_at=now, monitoring_status='active')
            db.execute('UPDATE remote_runs SET record=?,next_poll=? WHERE id=?',
                       (json.dumps(current), now, run_id))
        status, error = 'acknowledged', None
        try:
            async with asyncio.timeout(65):
                await provider.cancel(self.execution.get(run_id))
        except ProviderError as exc:
            status = 'rejected' if exc.code == 'cancel_rejected' else 'unknown'
            error = dict(code=exc.code, message=str(exc))
        except Exception:
            status, error = 'unknown', dict(code='cancel_unknown', message='Cancellation outcome unknown; inspect remote status.')
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = json.loads(db.execute('SELECT record FROM remote_runs WHERE id=?', (run_id,)).fetchone()[0])
            if current['cancel_status'] == 'requested':
                current.update(cancel_status=status, cancel_error=error)
            # Never overwrite a terminal observation that arrived during POST.
            db.execute('UPDATE remote_runs SET record=?,next_poll=? WHERE id=?',
                (json.dumps(current), None if current['terminal'] else time.time(), run_id))
        return current

    async def tick(self):
        self.adopt()
        now = time.time()
        with self.execution.connect() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM remote_runs WHERE next_poll<=? '
                'AND lease_until<=? ORDER BY next_poll LIMIT 4', (now, now))]
        await asyncio.gather(*(self.refresh(run_id, force=False) for run_id in ids))

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                # Do not log upstream bodies, parameters or credentials.
                logger.error('Remote monitor cycle failed; retrying.')
            await asyncio.sleep(2)
