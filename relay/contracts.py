"""Versioned contracts for pipeline orchestration, independent of local execution."""
from datetime import datetime, timezone
from typing import Literal, Annotated
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')]
Value = Annotated[str, Field(min_length=1, max_length=256)]
TestIdentity = Annotated[str, Field(min_length=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Parameter(Contract):
    kind: Name  # UI metadata; validation is driven by this parameter's configuration.
    allowed_values: list[Value] = Field(min_length=1)
    required: bool = True
    default: Value | None = None

    @model_validator(mode='after')
    def valid_default(self):
        if len(set(self.allowed_values)) != len(self.allowed_values):
            raise ValueError('allowed_values must be unique')
        if self.default is not None and self.default not in self.allowed_values:
            raise ValueError('default must be in allowed_values')
        return self


class Pipeline(Contract):
    selector: Name
    parameters: dict[Name, Parameter] = Field(default_factory=dict)


class Report(Contract):
    format: Name  # Parser identifier, independent of framework; unknown formats are allowed.
    path: Value

    @model_validator(mode='after')
    def relative_path(self):
        if self.path.startswith(('/', '\\')) or '\\' in self.path or ':' in self.path or '..' in self.path.split('/'):
            raise ValueError('report path must be relative without parent traversal')
        return self


class Project(Contract):
    provider: Name = 'bitbucket'  # Provider availability is checked by the execution layer.
    framework: Value | None = None  # Informational only.
    repository: Annotated[str, Field(pattern=r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    default_branch: Value
    allowed_branches: list[Value] = Field(min_length=1)
    reports: list[Report] = Field(default_factory=list)
    enabled: bool = True
    pipelines: dict[Name, Pipeline] = Field(min_length=1)

    @model_validator(mode='after')
    def coherent(self):
        if self.default_branch not in self.allowed_branches:
            raise ValueError('default_branch must be in allowed_branches')
        selectors = [p.selector for p in self.pipelines.values()]
        if len(set(selectors)) != len(selectors):
            raise ValueError('pipeline selectors must be unique within a project')
        return self


class Registry(Contract):
    schema_version: Literal[1] = 1
    projects: dict[Name, Project] = Field(min_length=1)


class PlanRequest(Contract):
    project_id: Name
    pipeline_id: Name
    branch: Value | None = None
    parameters: dict[Name, Value] = Field(default_factory=dict)


class ExecutionPlan(Contract):
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    project_id: Name
    pipeline_id: Name
    project: Project  # Validated snapshot; never contains runtime credentials.
    branch: Value
    parameters: dict[Name, Value]

    @model_validator(mode='after')
    def validate_selection(self):
        p = self.project
        if not p.enabled:
            raise ValueError('project is disabled')
        if self.branch not in p.allowed_branches:
            raise ValueError('branch is not allowed')
        if self.pipeline_id not in p.pipelines:
            raise ValueError('unknown pipeline')
        specs = p.pipelines[self.pipeline_id].parameters
        if set(self.parameters) - set(specs):
            raise ValueError('unknown pipeline parameter')
        for key, spec in specs.items():
            if key not in self.parameters:
                if spec.default is not None:
                    self.parameters[key] = spec.default
                elif spec.required:
                    raise ValueError(f'missing required parameter: {key}')
            if key in self.parameters and self.parameters[key] not in spec.allowed_values:
                raise ValueError(f'parameter {key} has a value outside allowed_values')
        return self


PipelineStatus = Literal['not_started', 'pending', 'running', 'successful', 'failed', 'stopped', 'unknown']
TestStatus = Literal['pass', 'fail', 'skip', 'error', 'flaky', 'unknown']


class Artifact(Contract):
    id: Name
    name: Value
    kind: Name
    parse_status: Literal['not_attempted', 'parsed', 'unsupported', 'failed'] = 'not_attempted'
    availability: Literal['pending', 'available', 'missing', 'expired'] = 'pending'
    storage_key: Value | None = None  # Opaque storage reference, not a signed URL.


class TestResult(Contract):
    id: TestIdentity
    name: TestIdentity
    status: TestStatus
    message: str = ''
    artifact_ids: list[Name] = Field(default_factory=list)
    attempts: int | None = Field(default=None, ge=1)


class PipelineStep(Contract):
    id: Value
    name: Value
    status: PipelineStatus = 'unknown'
    artifact_ids: list[Name] = Field(default_factory=list)


class Run(Contract):
    id: str = Field(default_factory=lambda: uuid4().hex)
    plan: ExecutionPlan
    pipeline_id: Value | None = None
    pipeline_status: PipelineStatus = 'not_started'
    test_status: Literal['not_collected', 'passed', 'failed', 'incomplete', 'unknown'] = 'not_collected'
    steps: list[PipelineStep] = Field(default_factory=list)
    tests: list[TestResult] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
