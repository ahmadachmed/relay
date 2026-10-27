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

    def robot_evidence(self, run_id, artifact_id, expected_sha=None):
        self.monitor.get(run_id)
        self.expire()
        with self.execution.connect() as db:
            row = db.execute('SELECT metadata,body FROM evidence WHERE id=? AND run_id=?', (artifact_id,run_id)).fetchone()
        if row is None:
            raise PlanError('not_found', 'Unknown artifact for this run', 404)
        metadata, body = json.loads(row[0]), row[1]
        if body is None:
            raise PlanError('expired_evidence', 'Evidence retention expired', 410)
        if metadata.get('format') != 'robot_xml':
            raise PlanError('unsupported_report', 'This evidence is not a Robot XML report.', 415)
        sha = hashlib.sha256(body).hexdigest()
        if sha != metadata.get('sha256') or (expected_sha is not None and sha != expected_sha):
            raise PlanError('evidence_changed', 'Evidence changed. Review and select it again.', 409)
        return body, sha, metadata

    def robot_report(self, run_id, artifact_id, offset=0, limit=20, test_id=None, sanitize=None, section_id=None, text_offset=0, expected_sha=None):
        from .report_sections import inspect_robot
        body, sha, metadata = self.robot_evidence(run_id, artifact_id, expected_sha)
        result = inspect_robot(body, sha, offset, limit, test_id, sanitize, section_id, text_offset)
        result.update(scope='saved_run', run_id=run_id, artifact_id=artifact_id, association=metadata.get('association', 'unverified'))
        return result

    def search_robot_report(self, run_id, artifact_id, query, offset=0, limit=20, sanitize=None, include_suites=False, expected_sha=None):
        from .report_search import search_robot
        body, sha, metadata = self.robot_evidence(run_id, artifact_id, expected_sha)
        result = search_robot(body, sha, query, offset, limit, sanitize, include_suites)
        result.update(scope='saved_run', run_id=run_id, artifact_id=artifact_id, association=metadata.get('association', 'unverified'))
        return result

    def robot_suites(self, run_id, artifact_id, offset=0, limit=20, suite_id=None, sanitize=None, expected_sha=None):
        from .suite_sections import inspect_suites
        body, sha, metadata = self.robot_evidence(run_id, artifact_id, expected_sha)
        result = inspect_suites(body, sha, offset, limit, suite_id, sanitize)
        result.update(scope='saved_run', run_id=run_id, artifact_id=artifact_id, association=metadata.get('association', 'unverified'))
        return result

    def run_identity(self, run_id):
        self.monitor.get(run_id)
        record=self.execution.get(run_id)
        plan=record['plan']
        project=plan['project']
        specs=project['pipelines'][plan['pipeline_id']]['parameters']
        keys=[key for key,spec in specs.items() if spec.get('kind')=='environment']
        environment=plan.get('parameters',{}).get(keys[0]) if len(keys)==1 else None
        return {'run_id':run_id, 'project_id':plan['project_id'], 'repository':project['repository'],
                'framework':project['framework'], 'pipeline_id':plan['pipeline_id'], 'sut_id':plan.get('sut_id'),
                'parameters':plan.get('parameters'), 'branch':plan['branch'], 'commit':record.get('commit'),
                'attempted_at':record.get('attempted_at'),
                'environment':environment}

    def compare_saved_reports(self, first_run_id, first_id, first_sha, second_run_id, second_id, second_sha, offset=0, limit=20, sanitize=None):
        from .report_compare import compare_robot
        if first_run_id==second_run_id:
            raise PlanError('comparison_same_run', 'Choose evidence from two different runs.', 422)
        first, sha1, meta1=self.robot_evidence(first_run_id,first_id,first_sha)
        second, sha2, meta2=self.robot_evidence(second_run_id,second_id,second_sha)
        identity1,identity2=self.run_identity(first_run_id),self.run_identity(second_run_id)
        keys=('project_id','repository','framework','pipeline_id','sut_id','branch','commit','environment')
        missing=[key for key in keys if not identity1.get(key) or not identity2.get(key)]
        different=[key for key in keys if identity1.get(key) and identity2.get(key) and identity1[key]!=identity2[key]]
        if identity1['parameters'] is None or identity2['parameters'] is None:
            missing.append('parameters')
        elif identity1['parameters'] != identity2['parameters']:
            different.append('parameters')
        status='different' if different else 'unknown' if missing else 'matching_metadata_unverified'
        first_time,second_time=identity1.get('attempted_at'),identity2.get('attempted_at')
        recorded_order=('unknown' if not isinstance(first_time,(int,float)) or not isinstance(second_time,(int,float)) or first_time==second_time
                        else 'first_before_second' if first_time<second_time else 'first_after_second')
        result=compare_robot(first,second,sha1,sha2,offset,limit,sanitize,allow_identical=True)
        clean=sanitize or (lambda value:value)
        safe_identity=lambda identity: {key:(value if key=='run_id' else clean(value)) for key,value in identity.items() if key!='parameters'}
        result.update(first_run_id=first_run_id, second_run_id=second_run_id,
                      first_artifact_id=first_id, second_artifact_id=second_id,
                      comparability={'status':status,'different_fields':different,'missing_fields':missing,'recorded_order':recorded_order,
                                     'first_identity':safe_identity(identity1),'second_identity':safe_identity(identity2),
                                     'first_association':meta1.get('association','unverified'),
                                     'second_association':meta2.get('association','unverified')})
        result['limitations'].append('Run metadata and trigger attempt order are recorded, but report association is operator-asserted; execution chronology and comparability are not verified.')
        return result

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
