import asyncio
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import docker
from docker.errors import DockerException
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from starlette.responses import StreamingResponse


app = FastAPI(title="Codex Sandbox MVP", version="0.3.0")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
HOST_DATA_DIR = Path(os.environ.get("HOST_DATA_DIR", "/data/codex-mvp"))
TASK_DIR = DATA_DIR / "tasks"
RESULT_DIR = DATA_DIR / "results"
MAX_ACTIVE_TASKS = int(os.environ.get("MAX_ACTIVE_TASKS", "1"))
RUNNER_IMAGE = os.environ.get("RUNNER_IMAGE", "")
TASK_TIMEOUT_SECONDS = int(os.environ.get("TASK_TIMEOUT_SECONDS", "180"))
SSE_POLL_SECONDS = 0.25
SSE_HEARTBEAT_SECONDS = 15
_lock = threading.Lock()


class Repository(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    ref: str = Field(default="main", min_length=1, max_length=200)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("repository.url must be an HTTPS URL without embedded credentials")
        return value

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", value) or ".." in value or value.startswith("-"):
            raise ValueError("repository.ref contains invalid characters")
        return value


class TaskRequest(BaseModel):
    repository: Repository
    prompt: str = Field(min_length=1, max_length=20000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_file(task_id: str) -> Path:
    return TASK_DIR / task_id / "task.json"


def _read_task(task_id: str) -> dict:
    path = _task_file(task_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="task not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_task(task: dict) -> None:
    path = _task_file(task["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _active_count() -> int:
    return sum(
        json.loads(path.read_text(encoding="utf-8"))["status"] in {"starting", "running"}
        for path in TASK_DIR.glob("*/task.json")
    )


def _sse_event(task_id: str, sequence: int, event_type: str, data: dict) -> str:
    envelope = {
        "task_id": task_id,
        "sequence": sequence,
        "timestamp": _now(),
        "type": event_type,
        "data": data,
    }
    payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence}\nevent: {event_type}\ndata: {payload}\n\n"


async def _stream_task_events(task_id: str, after_sequence: int):
    events_path = RESULT_DIR / task_id / "codex-events.jsonl"
    offset = 0
    pending = b""
    sequence = 0
    last_heartbeat = time.monotonic()

    while True:
        if events_path.is_file():
            with events_path.open("rb") as events_file:
                events_file.seek(offset)
                chunk = events_file.read()
                offset = events_file.tell()
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            for line in lines:
                if not line:
                    continue
                sequence += 1
                if sequence > after_sequence:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = {"raw": line.decode("utf-8", errors="replace")}
                    yield _sse_event(task_id, sequence, "codex.event", event)

        task = _read_task(task_id)
        if task["status"] in {"succeeded", "failed", "timed_out"}:
            if pending.strip():
                sequence += 1
                if sequence > after_sequence:
                    try:
                        event = json.loads(pending)
                    except json.JSONDecodeError:
                        event = {"raw": pending.decode("utf-8", errors="replace")}
                    yield _sse_event(task_id, sequence, "codex.event", event)
            sequence += 1
            if sequence > after_sequence:
                event_type = "task.completed" if task["status"] == "succeeded" else "task.failed"
                yield _sse_event(task_id, sequence, event_type, {"status": task["status"]})
            return

        if time.monotonic() - last_heartbeat >= SSE_HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            last_heartbeat = time.monotonic()
        await asyncio.sleep(SSE_POLL_SECONDS)


def _run_task(task_id: str) -> None:
    container = None
    task = _read_task(task_id)
    try:
        client = docker.from_env()
        container = client.containers.run(
            RUNNER_IMAGE,
            detach=True,
            name=f"codex-task-{task_id}",
            entrypoint="/usr/local/bin/run-codex-task",
            environment={"MODEL_API_KEY": os.environ["MODEL_API_KEY"]},
            volumes={
                str(HOST_DATA_DIR / "tasks" / task_id / "job"): {"bind": "/job", "mode": "ro"},
                str(HOST_DATA_DIR / "results" / task_id): {"bind": "/results", "mode": "rw"},
            },
            labels={"codex.mvp.managed": "true", "codex.mvp.task_id": task_id},
            mem_limit="2g",
            nano_cpus=1_000_000_000,
            pids_limit=256,
            security_opt=["no-new-privileges:true"],
        )
        task["status"] = "running"
        task["started_at"] = _now()
        _write_task(task)
        deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            container.reload()
            if container.status == "exited":
                break
            time.sleep(1)
        else:
            container.kill()
            task["status"] = "timed_out"
        if task["status"] != "timed_out":
            exit_file = RESULT_DIR / task_id / "exit-code.txt"
            task["status"] = "succeeded" if exit_file.is_file() and exit_file.read_text().strip() == "0" else "failed"
    except (DockerException, OSError, KeyError) as exc:
        task["status"] = "failed"
        task["error"] = type(exc).__name__
    finally:
        if container is not None:
            try:
                (RESULT_DIR / task_id / "runner.log").write_bytes(
                    container.logs(stdout=True, stderr=True, tail=200)
                )
            except DockerException:
                pass
            try:
                container.remove(force=True)
            except DockerException as exc:
                task["cleanup_error"] = type(exc).__name__
        task["finished_at"] = _now()
        _write_task(task)


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict[str, str]:
    if not RUNNER_IMAGE or not os.environ.get("MODEL_API_KEY"):
        raise HTTPException(status_code=503, detail="Runner image or model key is missing")
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(RUNNER_IMAGE)
    except DockerException as exc:
        raise HTTPException(status_code=503, detail=type(exc).__name__) from exc
    return {"status": "ready"}


@app.get("/v1/capacity")
async def capacity() -> dict[str, int]:
    return {
        "max_active_tasks": MAX_ACTIVE_TASKS,
        "running_tasks": _active_count(),
        "queued_tasks": 0,
    }


@app.post("/v1/tasks", status_code=status.HTTP_202_ACCEPTED)
async def create_task(request: TaskRequest, background_tasks: BackgroundTasks) -> dict:
    if not RUNNER_IMAGE or not os.environ.get("MODEL_API_KEY"):
        raise HTTPException(status_code=503, detail="Runner image or model key is missing")
    with _lock:
        if _active_count() >= MAX_ACTIVE_TASKS:
            raise HTTPException(status_code=429, detail="Runner capacity is full")
        task_id = uuid.uuid4().hex
        job_dir = TASK_DIR / task_id / "job"
        job_dir.mkdir(parents=True)
        (job_dir / "repository-url.txt").write_text(request.repository.url, encoding="utf-8")
        (job_dir / "repository-ref.txt").write_text(request.repository.ref, encoding="utf-8")
        (job_dir / "prompt.txt").write_text(request.prompt, encoding="utf-8")
        results = RESULT_DIR / task_id
        results.mkdir(parents=True)
        os.chown(results, 10001, 10001)
        _write_task({"task_id": task_id, "status": "starting", "created_at": _now()})
    background_tasks.add_task(_run_task, task_id)
    return {
        "task_id": task_id,
        "status": "starting",
        "links": {
            "self": f"/v1/tasks/{task_id}",
            "events": f"/v1/tasks/{task_id}/events",
        },
    }


@app.get("/v1/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return _read_task(task_id)


@app.get("/v1/tasks/{task_id}/events")
async def get_task_events(task_id: str, request: Request) -> StreamingResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise HTTPException(status_code=404, detail="task not found")
    _read_task(task_id)
    last_event_id = request.headers.get("last-event-id", "0")
    if not last_event_id.isdecimal():
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a nonnegative integer")
    return StreamingResponse(
        _stream_task_events(task_id, int(last_event_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
