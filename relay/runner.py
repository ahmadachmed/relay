"""Single-process local runner. Only configured suites may execute."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from .parsers import robot_report, playwright_report


class Runner:
    def __init__(self, config_path, data_path):
        self.config_path = Path(config_path).resolve()
        self.config = json.loads(self.config_path.read_text())
        self.data = Path(data_path).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.active = None
        self.process = None
        self.cancelled = set()
        # A previous server's jobs cannot be resumed safely.
        for path in self.data.glob('*/run.json'):
            run = json.loads(path.read_text())
            if run['status'] in ('queued', 'running'):
                run.update(status='interrupted', error='Server restarted; inspect runner/device before retrying.')
                self.save(run)

    def save(self, run):
        folder = self.data / run['id']
        folder.mkdir(exist_ok=True)
        tmp = folder / 'run.tmp'
        tmp.write_text(json.dumps(run, indent=2))
        tmp.replace(folder / 'run.json')

    def get(self, run_id):
        if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in '0123456789abcdef' for c in run_id):
            raise KeyError(run_id)
        try:
            return json.loads((self.data / run_id / 'run.json').read_text())
        except FileNotFoundError:
            raise KeyError(run_id)

    def list(self):
        return sorted([json.loads(p.read_text()) for p in self.data.glob('*/run.json')], key=lambda r: r['created_at'], reverse=True)

    def start(self, project_id, suite):
        project = self.config['projects'].get(project_id)
        if not project or not project.get('enabled', False):
            raise ValueError('Unknown or disabled project')
        if suite not in project['suites']:
            raise ValueError('Unknown suite')
        if project['adapter'] not in ('robot', 'playwright'):
            raise ValueError('Unsupported adapter')
        with self.lock:
            if self.active:
                raise RuntimeError('Runner busy; wait or cancel the active run')
            run = {'id': uuid.uuid4().hex, 'project': project_id, 'suite': suite,
                   'status': 'queued', 'created_at': datetime.now(timezone.utc).isoformat(), 'tests': []}
            self.save(run)
            self.active = run['id']
            threading.Thread(target=self.execute, args=(run, project), daemon=True).start()
            return run.copy()

    def cancel(self, run_id):
        with self.lock:
            run = self.get(run_id)
            if self.active == run_id:
                self.cancelled.add(run_id)
                if self.process and self.process.poll() is None:
                    self.terminate(self.process)
            return run

    @staticmethod
    def terminate(process):
        if os.name == 'posix':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()

    def execute(self, run, project):
        folder = self.data / run['id']
        try:
            cwd = (self.config_path.parent / project['path']).resolve()
            args = project['suites'][run['suite']]
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ValueError('Suite arguments must be a list of strings')
            env = os.environ.copy()
            if project['adapter'] == 'robot':
                report = folder / 'output.xml'
                command = [sys.executable, '-m', 'robot', '--outputdir', str(folder), *args]
                parse = robot_report
            else:
                report = folder / 'results.json'
                env['PLAYWRIGHT_JSON_OUTPUT_FILE'] = str(report)
                command = ['npx', '--no-install', 'playwright', 'test', *args, '--reporter=json', '--output', str(folder / 'artifacts')]
                parse = playwright_report
            with (folder / 'console.log').open('w') as log:
                with self.lock:
                    if run['id'] in self.cancelled:
                        run['status'] = 'cancelled'
                        return
                    run['status'] = 'running'
                    self.save(run)
                    self.process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=(os.name == 'posix'))
                    process = self.process
                try:
                    run['exit_code'] = process.wait(timeout=project.get('timeout_seconds', 600))
                except subprocess.TimeoutExpired:
                    self.terminate(process)
                    process.wait()
                    raise TimeoutError('Suite exceeded configured timeout')
            with self.lock:
                cancelled = run['id'] in self.cancelled
            if cancelled:
                run['status'] = 'cancelled'
            else:
                run['tests'] = parse(report)
                if not run['tests']:
                    raise ValueError('Report contains no tests')
                run['status'] = 'passed' if run['exit_code'] == 0 and all(t['status'] in ('pass', 'skip') for t in run['tests']) else 'failed'
        except Exception as exc:
            run.update(status='error', error=str(exc))
        finally:
            with self.lock:
                if run['id'] in self.cancelled:
                    run['status'] = 'cancelled'
                run['finished_at'] = datetime.now(timezone.utc).isoformat()
                self.save(run)
                self.active = None
                self.process = None
                self.cancelled.discard(run['id'])
