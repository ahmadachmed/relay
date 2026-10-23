"""Project registry and pure plan validation; no network or execution side effects."""
from pathlib import Path
from .contracts import ExecutionPlan, PlanRequest, Registry


def load_registry(path: str | Path) -> Registry:
    return Registry.model_validate_json(Path(path).read_text())


def resolve_plan(registry: Registry, request: PlanRequest) -> ExecutionPlan:
    project = registry.projects.get(request.project_id)
    if project is None:
        raise ValueError('unknown project')
    return ExecutionPlan(
        project_id=request.project_id, pipeline_id=request.pipeline_id,
        project=project.model_copy(deep=True),
        branch=request.branch or project.default_branch,
        parameters=dict(request.parameters),
    )
