from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse

from .browser import BrowserRunner
from .models import JobRecord, JobStatus, RunRequest

ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "/artifacts")).resolve()
MAX_SITES = int(os.getenv("MAX_SITES_PER_JOB", "5"))
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT_SECONDS", "30"))

app = FastAPI(
    title="Tools Engine",
    version="0.1.0",
    description="A safe MVP browser automation service that visits public websites and captures snapshots.",
)
runner = BrowserRunner(ARTIFACT_DIR)
jobs: dict[str, JobRecord] = {}
job_tasks: set[asyncio.Task] = set()


def now() -> datetime:
    return datetime.now(timezone.utc)


@app.on_event("startup")
async def startup() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    await runner.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in list(job_tasks):
        task.cancel()
    await runner.stop()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"service": "tools-engine", "status": "ok", "browser": "available"}


@app.post("/v1/runs", response_model=dict[str, str], status_code=202)
async def create_run(request: RunRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    if len(request.sites) > MAX_SITES:
        raise HTTPException(status_code=400, detail=f"A maximum of {MAX_SITES} sites is allowed")

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    record = JobRecord(
        job_id=job_id,
        run_id=request.run_id,
        status=JobStatus.queued,
        created_at=now(),
    )
    jobs[job_id] = record
    task = asyncio.create_task(execute_job(job_id, request))
    job_tasks.add(task)
    task.add_done_callback(job_tasks.discard)
    return {"job_id": job_id, "status": record.status.value}


@app.get("/v1/jobs/{job_id}", response_model=JobRecord)
async def get_job(job_id: str) -> JobRecord:
    record = jobs.get(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    return record


@app.get("/v1/jobs/{job_id}/artifacts/{filename}")
async def get_artifact(job_id: str, filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid artifact filename")
    artifact = (ARTIFACT_DIR / job_id / filename).resolve()
    job_dir = (ARTIFACT_DIR / job_id).resolve()
    if job_dir not in artifact.parents or not artifact.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(artifact, media_type="image/png", filename=artifact.name)


async def execute_job(job_id: str, request: RunRequest) -> None:
    record = jobs[job_id]
    record.status = JobStatus.running
    record.started_at = now()
    timeout = request.timeout_seconds or DEFAULT_TIMEOUT
    try:
        # Sequential execution keeps browser resource use predictable in the MVP.
        record.results = [
            await runner.run_site(job_id, target, timeout)
            for target in request.sites
        ]
        record.status = JobStatus.completed
    except Exception as exc:
        record.status = JobStatus.failed
        record.error = str(exc)[:500]
    finally:
        record.finished_at = now()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
