"""TypedDict definitions for common API response shapes.

These are additive documentation types — handlers can gradually adopt them
to improve type safety and IDE completion for API responses.
"""

from __future__ import annotations

from typing import Any, TypedDict


class HealthResponse(TypedDict):
    """GET /api/health response."""

    status: str
    workers: int
    version: str
    build_sha: str
    uptime: float
    test_mode: bool


class WorkerResponse(TypedDict):
    """Single worker entry in GET /api/workers response."""

    name: str
    state: str
    state_duration: float
    path: str
    description: str
    provider: str
    assigned_task: str | None


class TaskResponse(TypedDict, total=False):
    """Single task entry in GET /api/tasks response."""

    id: str
    title: str
    description: str
    status: str
    priority: str
    task_type: str
    assigned_worker: str
    created_at: float
    completed_at: float | None
    resolution: str


class ProposalResponse(TypedDict):
    """Single proposal entry in GET /api/proposals response."""

    id: str
    type: str
    worker_name: str
    task_id: str
    task_title: str
    message: str
    status: str
    created_at: float
    confidence: float


class ErrorResponse(TypedDict):
    """Standard error response body."""

    error: str


class StatusResponse(TypedDict):
    """Generic action result (e.g. kill, revive, continue)."""

    status: str


class WorkerListResponse(TypedDict):
    """GET /api/workers response."""

    workers: list[dict[str, Any]]


class TaskListResponse(TypedDict):
    """GET /api/tasks response."""

    tasks: list[dict[str, Any]]


class ProposalListResponse(TypedDict):
    """GET /api/proposals response."""

    proposals: list[dict[str, Any]]
    pending_count: int
