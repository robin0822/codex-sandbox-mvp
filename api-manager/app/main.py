import os

import docker
from fastapi import FastAPI


app = FastAPI(title="Codex Sandbox MVP", version="0.1.0")


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict[str, str]:
    client = docker.from_env()
    client.ping()
    image = os.environ.get("RUNNER_IMAGE", "")
    if image:
        client.images.get(image)
    return {"status": "ready"}


@app.get("/v1/capacity")
async def capacity() -> dict[str, int]:
    return {
        "max_active_tasks": int(os.environ.get("MAX_ACTIVE_TASKS", "1")),
        "running_tasks": 0,
        "queued_tasks": 0,
    }

