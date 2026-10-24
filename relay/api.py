"""Local-only API; do not expose to the internet without authentication."""
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from .runner import Runner
from .registry import load_registry, resolve_plan
from .contracts import PlanRequest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent


def create_app(config=None, data=None, registry_path=None):
    app = FastAPI(title='Relay QA Harness')
    registry = load_registry(registry_path or os.getenv("RELAY_PROJECTS", ROOT / "projects.example.json"))
    runner = Runner(config or os.getenv('RELAY_CONFIG', ROOT / 'relay.example.json'), data or os.getenv('RELAY_DATA', ROOT / '.relay'))
    app.state.runner = runner
    app.state.registry = registry

    @app.middleware('http')
    async def local_guard(request: Request, call_next):
        # Block browser cross-site mutations and DNS rebinding in the local MVP.
        if request.url.hostname not in ('127.0.0.1', 'localhost', 'testserver'):
            return JSONResponse({'detail': 'Local access only'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Cross-origin access denied'}, status_code=403)
        return await call_next(request)

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'web' / 'index.html')

    @app.get('/api/projects')
    def projects():
        return [{'id': key, 'adapter': p['adapter'], 'enabled': p.get('enabled', False), 'suites': list(p['suites'])} for key, p in runner.config['projects'].items()]

    @app.get('/api/project-registry')
    def project_registry():
        return registry.model_dump(mode='json')

    @app.post('/api/plans/validate')
    def validate_plan(body: PlanRequest):
        try:
            return resolve_plan(registry, body).model_dump(mode='json')
        except ValidationError as exc:
            raise HTTPException(422, [{'loc': e['loc'], 'msg': e['msg']} for e in exc.errors()])
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get('/api/runs')
    def runs():
        return runner.list()

    def get_run(run_id):
        try:
            return runner.get(run_id)
        except KeyError:
            raise HTTPException(404, 'Run not found')

    @app.get('/api/runs/{run_id}')
    def detail(run_id: str):
        return get_run(run_id)

    @app.post('/api/runs/{run_id}/cancel')
    def cancel(run_id: str):
        get_run(run_id)
        return runner.cancel(run_id)

    @app.get('/api/runs/{run_id}/evidence/{name}')
    def evidence(run_id: str, name: str):
        get_run(run_id)
        if name not in ('output.xml', 'results.json', 'console.log', 'log.html', 'report.html'):
            raise HTTPException(404, 'Unknown artifact')
        path = runner.data / run_id / name
        if not path.is_file():
            raise HTTPException(404, 'Artifact not available')
        return FileResponse(path, filename=name)  # Download untrusted report HTML.

    @app.post('/api/chat')
    def chat(body: Message):
        parts = body.message.strip().split()
        if parts == ['help']:
            return {'message': 'Commands: run <project> <suite>, status <run-id>, cancel <run-id>. This MVP uses explicit commands; no model is connected.'}
        if len(parts) == 2 and parts[0] in ('status', 'cancel'):
            run = get_run(parts[1])
            if parts[0] == 'cancel':
                run = runner.cancel(parts[1])
            return {'message': f"Run {run['id']}: {run['status']}", 'run': run}
        if len(parts) == 3 and parts[0] == 'run':
            try:
                run = runner.start(parts[1], parts[2])
                return {'message': f"Started {run['project']} / {run['suite']}", 'run': run}
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            except RuntimeError as exc:
                raise HTTPException(409, str(exc))
        raise HTTPException(400, 'Use: run <project> <suite>, status <run-id>, cancel <run-id>, or help')
    return app


class Message(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
