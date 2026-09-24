from __future__ import annotations

import asyncio
import os
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .agent import AgentRunner, AgentSession
from .browser import BrowserRunner
from .models import AgentTaskRecord, AgentTaskRequest, AgentTaskStatus, JobRecord, JobStatus, RunRequest
from .security import UnsafeUrlError, validate_configured_url

load_dotenv()

ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "/artifacts")).resolve()
AGENT_FILE_DIR = Path(os.getenv("AGENT_FILE_DIR", "/agent-files")).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_SITES = int(os.getenv("MAX_SITES_PER_JOB", "5"))
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT_SECONDS", "30"))

app = FastAPI(
    title="Web Interactive Tools API",
    version="0.2.0",
    description="A browser automation API with an AWS Bedrock tool-calling agent layer.",
)
runner = BrowserRunner(ARTIFACT_DIR)
agent_runner: AgentRunner | None = None
jobs: dict[str, JobRecord] = {}
agent_tasks: dict[str, AgentTaskRecord] = {}
agent_sessions: dict[str, AgentSession] = {}
agent_locks: dict[str, asyncio.Lock] = {}
profile_lock = asyncio.Lock()
background_tasks: set[asyncio.Task] = set()


def now() -> datetime:
    return datetime.now(timezone.utc)


def track(task: asyncio.Task) -> None:
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


@app.on_event("startup")
async def startup() -> None:
    global agent_runner
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_FILE_DIR.mkdir(parents=True, exist_ok=True)
    await runner.start()
    if runner._browser is None:
        raise RuntimeError("Browser did not start")
    agent_runner = AgentRunner(runner._browser, ARTIFACT_DIR, runner.new_agent_context)


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in list(background_tasks):
        task.cancel()
    for session in list(agent_sessions.values()):
        await close_agent_session(session)
    await runner.stop()


@app.get("/health")
async def health() -> dict[str, str | bool]:
    return {"service": "tools-engine", "status": "ok", "browser": "available", "ai_agent": bool(os.getenv("AWS_REGION") and os.getenv("BEDROCK_MODEL_ID")), "model_provider": "bedrock", "persistent_profile": bool(os.getenv("BROWSER_PROFILE_DIR"))}


@app.get("/v1/browser/session")
async def browser_session_status() -> dict[str, object]:
    pages = []
    if runner._agent_context:
        pages = [{"url": page.url, "title": await page.title()} for page in runner._agent_context.pages]
    return {"persistent": bool(runner._agent_context), "profile_dir": os.getenv("BROWSER_PROFILE_DIR"), "pages": pages}


@app.post("/v1/runs", response_model=dict[str, str], status_code=202)
async def create_run(request: RunRequest) -> dict[str, str]:
    if len(request.sites) > MAX_SITES:
        raise HTTPException(status_code=400, detail=f"A maximum of {MAX_SITES} sites is allowed")
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    jobs[job_id] = JobRecord(job_id=job_id, run_id=request.run_id, status=JobStatus.queued, created_at=now())
    track(asyncio.create_task(execute_job(job_id, request)))
    return {"job_id": job_id, "status": JobStatus.queued.value}


@app.get("/v1/jobs/{job_id}", response_model=JobRecord)
async def get_job(job_id: str) -> JobRecord:
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@app.post("/v1/agent/tasks", response_model=dict[str, str], status_code=202)
async def create_agent_task(request: AgentTaskRequest) -> dict[str, str]:
    try:
        validate_configured_url(str(request.url))
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Browser agent is not ready")
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    agent_locks[task_id] = asyncio.Lock()
    agent_tasks[task_id] = AgentTaskRecord(task_id=task_id, url=str(request.url), instruction=request.instruction, status=AgentTaskStatus.queued, created_at=now())
    track(asyncio.create_task(execute_agent_task(task_id, request)))
    return {"task_id": task_id, "status": AgentTaskStatus.queued.value}


@app.get("/v1/agent/tasks/{task_id}", response_model=AgentTaskRecord)
async def get_agent_task(task_id: str) -> AgentTaskRecord:
    if task_id not in agent_tasks:
        raise HTTPException(status_code=404, detail="Agent task not found")
    return agent_tasks[task_id]


@app.get("/v1/agent/tasks/{task_id}/events")
async def get_agent_events(task_id: str) -> list[dict]:
    return (await get_agent_task(task_id)).events


@app.post("/v1/agent/tasks/{task_id}/confirm", response_model=dict[str, str])
async def confirm_agent_task(task_id: str) -> dict[str, str]:
    task = await get_agent_task(task_id)
    if task.status != AgentTaskStatus.waiting_confirmation:
        raise HTTPException(status_code=409, detail="Task is not waiting for confirmation")
    session = agent_sessions.get(task_id)
    if session is None or agent_runner is None:
        raise HTTPException(status_code=410, detail="Agent session is no longer available")
    task.status = AgentTaskStatus.running
    pending = session.pending_confirmation or {}
    session.messages.append({
        "role": "user",
        "content": [{"toolResult": {
            "toolUseId": pending.get("tool_call_id", "confirmation"),
            "content": [{"text": '{"confirmed": true, "proceed": true}'}],
        }}],
    })
    session.pending_confirmation = None
    track(asyncio.create_task(resume_agent_task(task_id)))
    return {"task_id": task_id, "status": task.status.value}


@app.post("/v1/agent/tasks/{task_id}/cancel", response_model=dict[str, str])
async def cancel_agent_task(task_id: str) -> dict[str, str]:
    task = await get_agent_task(task_id)
    task.status = AgentTaskStatus.cancelled
    task.finished_at = now()
    session = agent_sessions.pop(task_id, None)
    if session:
        await close_agent_session(session)
    agent_locks.pop(task_id, None)
    return {"task_id": task_id, "status": task.status.value}


@app.get("/v1/jobs/{job_id}/artifacts/{filename}")
async def get_artifact(job_id: str, filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid artifact filename")
    artifact = (ARTIFACT_DIR / job_id / filename).resolve()
    job_dir = (ARTIFACT_DIR / job_id).resolve()
    if job_dir not in artifact.parents or not artifact.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type = mimetypes.guess_type(artifact.name)[0] or "application/octet-stream"
    return FileResponse(artifact, media_type=media_type, filename=artifact.name)


@app.get("/v1/agent/tasks/{task_id}/artifacts/{filename}")
async def get_agent_artifact(task_id: str, filename: str) -> FileResponse:
    return await get_artifact(task_id, filename)


@app.post("/v1/agent/files", response_model=dict[str, str], status_code=201)
async def upload_agent_file(file: UploadFile = File(...)) -> dict[str, str]:
    safe_name = Path(file.filename or "upload.bin").name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")
    destination = (AGENT_FILE_DIR / safe_name).resolve()
    if AGENT_FILE_DIR not in destination.parents:
        raise HTTPException(status_code=400, detail="Invalid filename")
    total = 0
    with destination.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_UPLOAD_BYTES} byte limit")
            output.write(chunk)
    return {"filename": safe_name, "path": f"/agent-files/{safe_name}"}


async def close_agent_session(session: AgentSession) -> None:
    if session.shared_context:
        await session.page.close()
    else:
        await session.context.close()


async def execute_job(job_id: str, request: RunRequest) -> None:
    record = jobs[job_id]
    record.status = JobStatus.running
    record.started_at = now()
    try:
        record.results = [await runner.run_site(job_id, target, request.timeout_seconds or DEFAULT_TIMEOUT) for target in request.sites]
        record.status = JobStatus.completed
    except Exception as exc:
        record.status = JobStatus.failed
        record.error = str(exc)[:500]
    finally:
        record.finished_at = now()


async def execute_agent_task(task_id: str, request: AgentTaskRequest) -> None:
    task = agent_tasks[task_id]
    task.status = AgentTaskStatus.running
    task.started_at = now()
    session: AgentSession | None = None
    try:
        if agent_runner is None:
            raise RuntimeError("AI agent is not configured")
        start_lock = profile_lock if os.getenv("BROWSER_PROFILE_DIR") else agent_locks[task_id]
        async with start_lock:
            session = await agent_runner.start_session(
                task_id,
                str(request.url),
                request.instruction,
                request.max_steps,
                request.timeout_seconds,
                request.require_confirmation,
            )
        agent_sessions[task_id] = session
        async with (profile_lock if session.shared_context else agent_locks[task_id]):
            apply_agent_result(task, await agent_runner.run_until_pause(session), session)
    except Exception as exc:
        if session is not None:
            task.step_count = session.step_count
            task.events = session.events
        task.status = AgentTaskStatus.failed
        task.error = str(exc)[:1_000]
        task.finished_at = now()


async def resume_agent_task(task_id: str) -> None:
    task = agent_tasks[task_id]
    session = agent_sessions[task_id]
    try:
        async with (profile_lock if session.shared_context else agent_locks[task_id]):
            apply_agent_result(task, await agent_runner.run_until_pause(session, allow_submission=True), session)  # type: ignore[union-attr]
    except Exception as exc:
        task.step_count = session.step_count
        task.events = session.events
        task.status = AgentTaskStatus.failed
        task.error = str(exc)[:1_000]
        task.finished_at = now()


def apply_agent_result(task: AgentTaskRecord, result: dict, session: AgentSession) -> None:
    task.step_count = session.step_count
    task.events = session.events
    task.result = result
    if result.get("status") == "waiting_confirmation":
        task.status = AgentTaskStatus.waiting_confirmation
        task.confirmation = result.get("confirmation")
    elif result.get("status") == "completed":
        task.status = AgentTaskStatus.completed
        task.finished_at = now()
    else:
        task.status = AgentTaskStatus.failed
        task.error = result.get("error", "Agent stopped")
        task.finished_at = now()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
