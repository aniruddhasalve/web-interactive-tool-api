from __future__ import annotations

import os
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "160"))
MAX_AGENT_TIMEOUT = int(os.getenv("MAX_AGENT_TIMEOUT_SECONDS", "600"))
DEFAULT_REQUIRE_CONFIRMATION = os.getenv("DEFAULT_REQUIRE_CONFIRMATION", "true").lower() == "true"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class Action(BaseModel):
    type: Literal[
        "wait_for_load",
        "wait_for_selector",
        "extract_title",
        "extract_text",
        "click",
        "fill",
        "screenshot",
    ]
    selector: str | None = None
    text: str | None = None
    full_page: bool = True
    timeout_ms: int = Field(default=10_000, ge=500, le=60_000)


class SiteTarget(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    url: HttpUrl
    actions: list[Action] = Field(default_factory=lambda: [Action(type="extract_title"), Action(type="screenshot")])


class RunRequest(BaseModel):
    run_id: str | None = Field(default=None, max_length=100)
    sites: list[SiteTarget] = Field(min_length=1, max_length=5)
    timeout_seconds: int = Field(default=30, ge=5, le=120)

    @field_validator("sites")
    @classmethod
    def unique_site_names(cls, sites: list[SiteTarget]) -> list[SiteTarget]:
        names = [site.name for site in sites]
        if len(names) != len(set(names)):
            raise ValueError("site names must be unique")
        return sites


class SiteResult(BaseModel):
    site: str
    url: str
    success: bool
    status_code: int | None = None
    page_title: str | None = None
    extracted: list[dict[str, Any]] = Field(default_factory=list)
    screenshot: str | None = None
    duration_ms: int
    error: str | None = None


class JobRecord(BaseModel):
    job_id: str
    run_id: str | None = None
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    results: list[SiteResult] = Field(default_factory=list)
    error: str | None = None


class AgentTaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    waiting_confirmation = "waiting_confirmation"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class AgentTaskRequest(BaseModel):
    url: HttpUrl
    instruction: str = Field(min_length=1, max_length=4_000)
    max_steps: int = Field(default=40, ge=1, le=MAX_AGENT_STEPS)
    timeout_seconds: int = Field(default=120, ge=5, le=MAX_AGENT_TIMEOUT)
    require_confirmation: bool = DEFAULT_REQUIRE_CONFIRMATION


class AgentTaskRecord(BaseModel):
    task_id: str
    url: str
    instruction: str
    status: AgentTaskStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    step_count: int = 0
    events: list[dict[str, Any]] = Field(default_factory=list)
    confirmation: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
