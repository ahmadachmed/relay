"""Source authorization contracts, separate from reviewed knowledge bundles.

Physical roots are backend configuration and must never be serialized to AI.
This foundation does not read files or grant model sharing consent.
"""
import re
from typing import Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .contracts import Contract, Name, SutId, Value, sensitive_source_component


class Source(Contract):
    category: Literal['automation', 'sut']
    scope_id: str = Field(min_length=1, max_length=100)
    environments: list[Value] = Field(min_length=1, max_length=32)
    provider: Literal['local', 'bitbucket']
    location: str = Field(min_length=1, max_length=1000, exclude=True)
    files: list[str] = Field(min_length=1, max_length=128)
    format: Literal['openapi', 'markdown', 'text', 'automation']
    enabled: bool = True
    max_file_bytes: int = Field(default=1_000_000, ge=1, le=5_000_000)

    @field_validator('files')
    @classmethod
    def safe_files(cls, values):
        if len(set(values)) != len(values):
            raise ValueError('duplicate source files')
        for value in values:
            if len(value) > 300 or not re.fullmatch(r'[A-Za-z0-9_./-]+', value):
                raise ValueError('invalid source path')
            parts = value.split('/')
            if any(not p or p.startswith('.') for p in parts):
                raise ValueError('invalid source path')
            if any(sensitive_source_component(p) for p in parts):
                raise ValueError('excluded source path')
        return values

    @model_validator(mode='after')
    def valid_location(self):
        TypeAdapter(SutId if self.category == 'sut' else Name).validate_python(self.scope_id)
        if '\x00' in self.location:
            raise ValueError('invalid source location')
        if len(set(self.environments)) != len(self.environments):
            raise ValueError('duplicate environments')
        if self.provider == 'local':
            if not self.location.startswith('/') or any(p in ('.', '..') for p in self.location.split('/')):
                raise ValueError('local root must be absolute without traversal')
        elif not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.location):
            raise ValueError('invalid repository')
        if self.provider == 'bitbucket' and any(p in ('.', '..') for p in self.location.split('/')):
            raise ValueError('invalid repository')
        return self


class SourceRegistry(Contract):
    sources: dict[SutId, Source] = Field(default_factory=dict, max_length=128)

    def authorize(self, source_id: str, category: str, scope_id: str, environment: str) -> Source:
        source = self.sources.get(source_id)
        if (source is None or not source.enabled or source.category != category
                or source.scope_id != scope_id or environment not in source.environments):
            raise ValueError('source unavailable for selected context')
        return source.model_copy(deep=True)
