"""Local-only API; do not expose to the internet without authentication."""
import os
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .runner import Runner
from .registry import load_registry, resolve_plan
from .contracts import PlanRequest, Contract
from .execution import ExecutionService, PlanError
from .monitoring import RunMonitor
from .evidence import EvidenceStore
from .report_evidence import MAX_REPORT_BYTES
from uuid import UUID
from pydantic import model_validator
from .providers import ProviderError
from .providers.bitbucket import BitbucketCatalogProvider
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent


def create_app(config=None, data=None, registry_path=None, monitoring=True):
    registry = load_registry(registry_path or os.getenv("RELAY_PROJECTS", ROOT / "projects.example.json"))
    runner = Runner(config or os.getenv('RELAY_CONFIG', ROOT / 'relay.example.json'), data or os.getenv('RELAY_DATA', ROOT / '.relay'))
    execution = ExecutionService(runner.data / 'remote.sqlite3', registry)
    monitor = RunMonitor(execution)
    evidence_store = EvidenceStore(execution, monitor)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(monitor.run()) if monitoring else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title='Relay QA Harness', lifespan=lifespan)
    app.state.runner = runner
    app.state.registry = registry
    app.state.execution = execution
    app.state.monitor = monitor

    @app.middleware('http')
    async def local_guard(request: Request, call_next):
        # Block browser cross-site mutations and DNS rebinding in the local MVP.
        if request.url.hostname not in ('127.0.0.1', 'localhost', 'testserver'):
            return JSONResponse({'detail': 'Local access only'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Cross-origin access denied'}, status_code=403)
        return await call_next(request)

    ui_dir = ROOT / 'frontend' / 'out'
    if ui_dir.is_dir():
        app.mount('/ui', StaticFiles(directory=ui_dir, html=True), name='workspace-ui')

    @app.get('/')
    def index():
        if (ui_dir / 'index.html').is_file():
            return RedirectResponse('/ui/')
        return FileResponse(ROOT / 'web' / 'index.html')

    @app.get('/legacy')
    def legacy_index():
        return FileResponse(ROOT / 'web' / 'index.html')

    @app.exception_handler(PlanError)
    async def plan_error(request, exc):
        return JSONResponse({'detail': {'code': exc.code, 'message': str(exc)}}, status_code=exc.status)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({'detail': {'code': exc.code, 'message': str(exc)}}, status_code=502)

    @app.post('/api/plans/preview')
    async def preview_plan(body: PlanRequest):
        try:
            return await execution.preview(body, BitbucketCatalogProvider.from_environment())
        except ValueError:
            raise HTTPException(422, 'Invalid project, branch, pipeline or parameters') from None

    @app.get('/api/plans/{plan_id}')
    def get_plan(plan_id: str):
        return execution.recovery_snapshot(plan_id)

    @app.post('/api/plans/{plan_id}/execute')
    async def execute_plan(plan_id: str, body: Confirmation):
        return await execution.execute(plan_id, BitbucketCatalogProvider.from_environment())

    @app.post('/api/plans/{plan_id}/reconcile')
    async def reconcile_plan(plan_id: str, body: Reconciliation):
        return await execution.reconcile(plan_id, str(body.pipeline_uuid),
                                         BitbucketCatalogProvider.from_environment())

    @app.get('/api/remote-runs')
    def remote_runs(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        return monitor.list(limit, offset)

    @app.get('/api/remote-runs/{run_id}')
    def remote_run(run_id: str):
        return monitor.get(run_id)

    @app.post('/api/remote-runs/{run_id}/refresh')
    async def refresh_remote_run(run_id: str):
        return await monitor.refresh(run_id)

    @app.post('/api/remote-runs/{run_id}/cancel')
    async def cancel_remote_run(run_id: str, body: Confirmation):
        return await monitor.cancel(run_id)

    @app.get('/api/remote-runs/{run_id}/evidence')
    def remote_evidence(run_id: str):
        return evidence_store.summary(run_id)

    @app.post('/api/remote-runs/{run_id}/evidence')
    async def upload_remote_evidence(run_id: str, request: Request, report_path: str):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_REPORT_BYTES:
                raise HTTPException(413, 'Report exceeds 10 MB')
            body.extend(chunk)
        return evidence_store.upload(run_id, report_path, bytes(body))

    @app.get('/api/remote-runs/{run_id}/evidence/{artifact_id}')
    def download_remote_evidence(run_id: str, artifact_id: str):
        return Response(evidence_store.download(run_id, artifact_id), media_type='application/octet-stream',
            headers={'Content-Disposition': 'attachment; filename="evidence.bin"',
                     'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "sandbox; default-src 'none'"})

    @app.get('/api/projects')
    def projects():
        return [{'id': key, 'adapter': p['adapter'], 'enabled': p.get('enabled', False), 'suites': list(p['suites'])} for key, p in runner.config['projects'].items()]

    @app.get('/api/project-registry')
    def project_registry():
        return registry.model_dump(mode='json')

    @app.get('/api/project-registry/{project_id}/catalog')
    async def catalog(project_id: str):
        project = registry.projects.get(project_id)
        if project is None:
            raise HTTPException(404, 'Unknown project')
        if not project.enabled:
            raise HTTPException(409, 'Project is disabled')
        if project.provider != 'bitbucket':
            raise HTTPException(501, 'Provider is not installed')
        provider = None
        try:
            provider = BitbucketCatalogProvider.from_environment()
            return await provider.catalog(project)
        except ProviderError as exc:
            status = {'not_configured':503, 'unauthorized':502, 'forbidden':502,
                      'not_found':502, 'rate_limited':503}.get(exc.code,502)
            raise HTTPException(status, {'code':exc.code, 'message':str(exc)})

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


class Confirmation(Contract):
    confirmed: bool = Field(strict=True)

    @model_validator(mode='after')
    def must_confirm(self):
        if not self.confirmed:
            raise ValueError('Explicit confirmation is required')
        return self


class Reconciliation(Confirmation):
    # Operator attests that the remote variables match the reviewed preview.
    pipeline_uuid: UUID = Field(strict=False)
    verified_parameters: bool = Field(strict=True)

    @model_validator(mode='after')
    def must_verify_parameters(self):
        if not self.verified_parameters:
            raise ValueError('Verify pipeline variables in Bitbucket before reconciliation')
        return self
