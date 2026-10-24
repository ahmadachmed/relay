"""Execution-provider contracts, independent of connection transport."""
from typing import Protocol
from ..contracts import Project


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class CatalogProvider(Protocol):
    """Read-only capabilities implemented in issue #2.

    Trigger/status/cancel/artifact capabilities are added by their owning issues.
    REST and MCP implementations must return this same normalized catalog.
    """
    def catalog(self, project: Project) -> dict: ...
