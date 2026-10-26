"""Local report evidence, explicitly uploaded for a known remote run."""
import hashlib
import json
import time
from uuid import uuid4

from .execution import PlanError
from .report_evidence import parse_report, MAX_REPORT_BYTES

RETENTION = 7 * 86400


def report_readiness(artifacts, tests):
    """Evidence availability, not a model job state or permission to share."""
    usable = {a.get('id') for a in artifacts
              if a.get('id') and a.get('availability') == 'available' and a.get('parse_status') == 'parsed'}
    ready = any(set(t.get('artifact_ids', [])) & usable for t in tests)
    limitations = []
    for availability, parse_status, message in (
        ('missing', None, 'Upload the missing configured report.'),
        ('expired', None, 'Report retention expired; evidence is no longer available.'),
        ('available', 'failed', 'A report failed parsing; inspect the report format and parser error.'),
        ('available', 'unsupported', 'A report format is unsupported; use Robot XML, JUnit XML or an Allure test-result JSON file.'),
    ):
        if any(a.get('availability') == availability and (parse_status is None or a.get('parse_status') == parse_status) for a in artifacts):
            limitations.append(message)
    if any(a.get('format') == 'allure_json' for a in artifacts):
        limitations.append('Allure result files represent individual test executions; full launch coverage, containers, attachments and retries are not verified.')
    if not ready:
        limitations.append('No usable parsed test results; provide a supported report for this run.')
    return dict(analysis_ready=ready, evidence_complete=ready and not limitations,
                evidence_limitations=limitations)


class EvidenceStore:
    def __init__(self, execution, monitor):
        self.execution, self.monitor = execution, monitor
        with execution.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS evidence ('
                'id TEXT PRIMARY KEY, run_id TEXT NOT NULL, report_path TEXT NOT NULL, '
                'metadata TEXT NOT NULL, body BLOB, results TEXT NOT NULL, expires_at REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS evidence_run ON evidence(run_id)')

    def expire(self):
        with self.execution.connect() as db:
            db.execute('UPDATE evidence SET body=NULL,results=? WHERE expires_at<=? AND body IS NOT NULL',
                       ('[]', time.time()))

    def upload(self, run_id, report_path, body):
        self.monitor.get(run_id)
        plan = self.execution.get(run_id)
        reports = plan['plan']['project']['reports']
        spec = next((s for s in reports if s['path'] == report_path), None)
        if spec is None:
            raise PlanError('unknown_report', 'Report path is not configured for this run', 422)
        if len(body) > MAX_REPORT_BYTES:
            raise PlanError('report_too_large', 'Report exceeds 10 MB', 413)
        artifact_id = uuid4().hex
        metadata = dict(id=artifact_id, name=report_path, format=spec['format'],
            sha256=hashlib.sha256(body).hexdigest(), size=len(body),
            source='manual_upload', association='operator_asserted', uploaded_at=time.time(),
            parse_status='unsupported', error=None)
        results = []
        if spec['format'] in ('robot_xml', 'junit_xml', 'allure_json'):
            try:
                results = parse_report(body, spec['format'], artifact_id)
                metadata['parse_status'] = 'parsed'
            except (ValueError, RecursionError):
                metadata.update(parse_status='failed', error='Malformed, unsafe, empty or unsupported report structure')
        with self.execution.connect() as db:
            # A report slot is immutable. Retrying identical uploads returns its original evidence.
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT metadata FROM evidence WHERE run_id=? AND report_path=?',
                             (run_id, report_path)).fetchone()
            if row:
                existing = json.loads(row[0])
                if existing['sha256'] != metadata['sha256']:
                    raise PlanError('evidence_conflict', 'This report already has different evidence')
                return existing
            db.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?,?)', (artifact_id, run_id,
                report_path, json.dumps(metadata), body, json.dumps(results), time.time() + RETENTION))
        return metadata

    def summary(self, run_id):
        self.monitor.get(run_id)
        self.expire()
        plan = self.execution.get(run_id)
        with self.execution.connect() as db:
            rows = db.execute('SELECT report_path,metadata,results,body IS NOT NULL FROM evidence WHERE run_id=?',
                              (run_id,)).fetchall()
        artifacts, tests, present = [], [], set()
        incomplete = False
        for path, raw, result, available in rows:
            metadata = json.loads(raw)
            metadata['availability'] = 'available' if available else 'expired'
            artifacts.append(metadata)
            present.add(path)
            tests.extend(json.loads(result))
            if metadata['parse_status'] != 'parsed' or not available or metadata.get('format') == 'allure_json':
                incomplete = True
        for spec in plan['plan']['project']['reports']:
            if spec['path'] not in present:
                artifacts.append(dict(name=spec['path'], format=spec['format'],
                    availability='missing', parse_status='not_attempted'))
                incomplete = True
        outcome = 'not_collected'
        if any(t['status'] in ('fail', 'error') for t in tests):
            outcome = 'failed'
        elif incomplete or (tests and all(t['status'] == 'skip' for t in tests)):
            outcome = 'incomplete'
        elif tests:
            outcome = 'passed' if all(t['status'] in ('pass', 'skip') for t in tests) else 'unknown'
        return dict(run_id=run_id, test_status=outcome, artifacts=artifacts, tests=tests,
                    **report_readiness(artifacts, tests))

    def download(self, run_id, artifact_id):
        self.monitor.get(run_id)
        self.expire()
        with self.execution.connect() as db:
            row = db.execute('SELECT body FROM evidence WHERE id=? AND run_id=?', (artifact_id, run_id)).fetchone()
        if row is None:
            raise PlanError('not_found', 'Unknown artifact for this run', 404)
        if row[0] is None:
            raise PlanError('expired_evidence', 'Evidence retention expired', 410)
        return row[0]
