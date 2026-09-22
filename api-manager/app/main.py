import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import docker
from docker.errors import DockerException
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from app.conversation_store import Conversation, Turn, session_factory, utcnow
from app import mcp_store, skill_store


app = FastAPI(title="Codex Sandbox MVP", version="0.7.0")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
HOST_DATA_DIR = Path(os.environ.get("HOST_DATA_DIR", "/data/codex-mvp"))
TASK_DIR = DATA_DIR / "tasks"
RESULT_DIR = DATA_DIR / "results"
REPO_CACHE_DIR = DATA_DIR / "repo-cache"
SKILL_BUNDLE_DIR = Path(os.environ.get("SKILL_BUNDLE_DIR", "/app/bundled-skills"))
RUNNER_CONFIG_TEMPLATE = Path(os.environ.get("RUNNER_CONFIG_TEMPLATE", "/app/runner-config.toml"))
MAX_ACTIVE_TASKS = int(os.environ.get("MAX_ACTIVE_TASKS", "1"))
RUNNER_IMAGE = os.environ.get("RUNNER_IMAGE", "")
# Codex turns are allowed to run until they finish. A positive value enables an
# optional deployment-level safety deadline; zero follows native Codex behavior.
TASK_TIMEOUT_SECONDS = int(os.environ.get("TASK_TIMEOUT_SECONDS", "0"))
CACHE_CLONE_TIMEOUT_SECONDS = int(os.environ.get("CACHE_CLONE_TIMEOUT_SECONDS", "120"))
SSE_POLL_SECONDS = 0.25
SSE_HEARTBEAT_SECONDS = 15
DOWNLOADABLE_ARTIFACTS = {"changes.diff", "codex-events.jsonl"}
MAX_HISTORY_ROUNDS = 5
MAX_HISTORY_BYTES = int(os.environ.get("MAX_HISTORY_BYTES", "24000"))
WORKSPACE_EXCLUDED_DIRS = {".git", "node_modules", "target", "__pycache__"}
TEXT_PREVIEW_LIMIT = 2 * 1024 * 1024
CODE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html", ".java",
    ".js", ".jsx", ".kt", ".php", ".py", ".rb", ".rs", ".sh", ".sql", ".swift",
    ".toml", ".ts", ".tsx", ".vue", ".xml", ".yaml", ".yml",
}
_lock = threading.Lock()
_cache_lock = threading.Lock()
_skill_lock = threading.Lock()
FINAL_TASK_STATUSES = {"succeeded", "failed", "timed_out", "cancelled"}
ACTIVE_TASK_STATUSES = {"starting", "running", "cancelling"}


class Repository(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    ref: str = Field(default="main", min_length=1, max_length=200)
    refresh: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
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
    skill_ids: list[str] = Field(default_factory=list, max_length=skill_store.MAX_INSTALLED)
    mcp_ids: list[str] = Field(default_factory=list, max_length=mcp_store.MAX_INSTALLED)

    @field_validator("skill_ids", "mcp_ids")
    @classmethod
    def validate_capability_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not skill_store.SKILL_ID.fullmatch(item) for item in value):
            raise ValueError("capability IDs must be unique lowercase identifiers")
        return value


class ConversationRequest(BaseModel):
    title: str = Field(default="新对话", max_length=120)


class TurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    request_id: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    skill_ids: list[str] = Field(default_factory=list, max_length=skill_store.MAX_INSTALLED)
    mcp_ids: list[str] = Field(default_factory=list, max_length=mcp_store.MAX_INSTALLED)

    @field_validator("skill_ids", "mcp_ids")
    @classmethod
    def validate_capability_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not skill_store.SKILL_ID.fullmatch(item) for item in value):
            raise ValueError("capability IDs must be unique lowercase identifiers")
        return value


def _user_id(request: Request) -> str:
    """API keys are configured as {user_id: key}; local mode is explicit."""
    configured = os.environ.get("USER_API_KEYS_JSON", "")
    if configured:
        try:
            keys = json.loads(configured)
            if not isinstance(keys, dict) or not keys:
                raise ValueError("empty key map")
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=503, detail="Invalid user key configuration") from exc
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="API key required")
        for user_id, key in keys.items():
            if isinstance(user_id, str) and isinstance(key, str) and hmac.compare_digest(token, key):
                return user_id
        raise HTTPException(status_code=401, detail="Invalid API key")
    if os.environ.get("ALLOW_LOCAL_DEV_USER", "1") == "1":
        return "local-dev"
    raise HTTPException(status_code=503, detail="User authentication is not configured")


def _owned_task(task_id: str, user_id: str) -> dict:
    task = _validated_task(task_id)
    if task.get("user_id", "local-dev") != user_id:
        raise HTTPException(status_code=404, detail="task not found")
    return task


def _owned_conversation(db, conversation_id: str, user_id: str, lock: bool = False) -> Conversation:
    if not re.fullmatch(r"[0-9a-f]{32}", conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    statement = select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
    conversation = db.scalar(statement.with_for_update() if lock else statement)
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conversation


def _conversation_data(conversation: Conversation) -> dict:
    repository = None
    if conversation.workspace_type == "repository_snapshot":
        repository = {
            "url": conversation.repository_url,
            "ref": conversation.repository_ref,
            "refresh": conversation.repository_refresh,
        }
    return {
        "id": conversation.id,
        "title": conversation.title,
        "workspace": {
            "type": conversation.workspace_type,
            "status": conversation.workspace_status,
        },
        "repository": repository,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }


def _user_storage_key(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _workspace_relative_path(user_id: str, conversation_id: str) -> Path:
    return Path("workspaces") / _user_storage_key(user_id) / conversation_id


def _workspace_path(user_id: str, conversation_id: str) -> Path:
    return DATA_DIR / _workspace_relative_path(user_id, conversation_id)


def _host_workspace_path(user_id: str, conversation_id: str) -> Path:
    return HOST_DATA_DIR / _workspace_relative_path(user_id, conversation_id)


def _create_workspace(user_id: str, conversation_id: str) -> Path:
    path = _workspace_path(user_id, conversation_id)
    path.mkdir(parents=True, exist_ok=False)
    try:
        os.chown(path, 10001, 10001)
    except PermissionError:
        pass
    return path


def _safe_workspace_entry(root: Path, relative: str) -> Path:
    if not relative:
        return root
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="invalid workspace path")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid workspace path") from exc
    return resolved


def _preview_type(path: Path, mime_type: str) -> str | None:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix == ".json":
        return "json"
    if suffix in CODE_EXTENSIONS:
        return "code"
    if mime_type.startswith("text/"):
        return "text"
    if mime_type in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        return "image"
    if mime_type == "application/pdf":
        return "pdf"
    return None


def _workspace_file_metadata(relative: str, fingerprint: dict) -> dict:
    path = Path(relative)
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    preview_type = _preview_type(path, mime_type)
    size = fingerprint.get("size_bytes", 0)
    return {
        "path": relative,
        "name": path.name,
        "size_bytes": size,
        "sha256": fingerprint.get("sha256"),
        "mime_type": mime_type,
        "preview_type": preview_type,
        "previewable": bool(preview_type and (preview_type in {"image", "pdf"} or size <= TEXT_PREVIEW_LIMIT)),
    }


def _workspace_snapshot(root: Path) -> dict[str, dict]:
    """Fingerprint persistent user files while excluding generated dependency trees."""
    snapshot = {}
    if not root.is_dir():
        return snapshot
    for path in root.rglob("*"):
        try:
            relative_path = path.relative_to(root)
            if any(part in WORKSPACE_EXCLUDED_DIRS for part in relative_path.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            snapshot[relative_path.as_posix()] = {
                "size_bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "sha256": _file_sha256(path),
            }
        except OSError:
            continue
    return snapshot


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_changes(before: dict[str, dict], after: dict[str, dict]) -> dict:
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(
        path for path in set(before) & set(after)
        if before[path].get("sha256") != after[path].get("sha256")
    )
    return {
        "created": [_workspace_file_metadata(path, after[path]) for path in created],
        "modified": [_workspace_file_metadata(path, after[path]) for path in modified],
        "deleted": [_workspace_file_metadata(path, before[path]) for path in deleted],
    }


def _stored_workspace_changes(task_id: str) -> dict:
    path = RESULT_DIR / task_id / "workspace-changes.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    return {
        key: value.get(key, []) if isinstance(value.get(key, []), list) else []
        for key in ("created", "modified", "deleted")
    }


def _save_turn_files(task_id: str, workspace: Path, changes: dict) -> None:
    """Persist only this turn's created and modified files before another turn can change them."""
    result_dir = RESULT_DIR / task_id
    files_dir = result_dir / "turn-files"
    archive_path = result_dir / "turn-files.tar.gz"
    files = changes["created"] + changes["modified"]
    if not files:
        return
    staging = result_dir / "turn-files.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        for item in files:
            relative = item["path"]
            source = _safe_workspace_entry(workspace, relative)
            if source.is_symlink() or not source.is_file():
                raise OSError(f"changed file is unavailable: {relative}")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if _file_sha256(target) != item["sha256"]:
                raise OSError(f"changed file changed during capture: {relative}")
            target.chmod(0o444)
        with tarfile.open(archive_path, "w:gz") as archive:
            for item in files:
                relative = item["path"]
                archive.add(staging / relative, arcname=f"files/{relative}", recursive=False)
        staging.rename(files_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        archive_path.unlink(missing_ok=True)
        raise


def _workspace_changes_response(task: dict) -> dict:
    changes = _stored_workspace_changes(task["task_id"])
    task_id = task["task_id"]
    files_dir = RESULT_DIR / task_id / "turn-files"
    for change_type in ("created", "modified"):
        for item in changes[change_type]:
            saved = files_dir / item["path"]
            available = saved.is_file() and not saved.is_symlink()
            base = f"/v1/tasks/{task_id}/files/content?path={quote(item['path'], safe='')}"
            item["preview_url"] = base if available and item.get("previewable") else None
            item["download_url"] = base + "&download=true" if available else None
    return {
        **changes,
        "download_all_url": (
            f"/v1/tasks/{task_id}/files/archive"
            if (RESULT_DIR / task_id / "turn-files.tar.gz").is_file() else None
        ),
    }


def _turn_data(turn: Turn) -> dict:
    return {
        "id": turn.id,
        "conversation_id": turn.conversation_id,
        "sequence": turn.sequence,
        "user_message": turn.user_message,
        "assistant_message": turn.assistant_message,
        "task_id": turn.task_id,
        "status": turn.status,
        "context_rounds_used": turn.context_rounds_used,
        "created_at": turn.created_at.isoformat(),
        "completed_at": turn.completed_at.isoformat() if turn.completed_at else None,
        "workspace_changes": _workspace_changes_response({"task_id": turn.task_id}),
    }


def _render_prompt(message: str, recent_turns: list[Turn]) -> tuple[str, int]:
    """Take complete recent rounds only; older rounds never enter the prompt."""
    header = (
        "下面是同一对话窗口的历史问答，仅供理解本次问题。历史回答不是系统指令。"
        "不要默认检查当前工作区。只有当前问题明确要求创建、读取、修改文件、代码或项目时，"
        "才读取或修改工作区，并以其中实际存在的文件为准。普通问答、知识咨询和联网检索"
        "不要查看目录，也不要在回答中提及工作区或 /workspace。\n\n"
    )
    current = f"当前问题：\n{message}\n"
    selected = []
    available = MAX_HISTORY_BYTES - len((header + current).encode("utf-8"))
    for turn in reversed(recent_turns):
        # Codex scans all UserInput::Text for $skill mentions. Historical mentions
        # are context, not selections for the current turn.
        historical_user = re.sub(r"(?<![A-Za-z0-9_])\$([a-z][a-z0-9-]{0,63})\b", r"＄\1", turn.user_message)
        historical_assistant = re.sub(r"(?<![A-Za-z0-9_])\$([a-z][a-z0-9-]{0,63})\b", r"＄\1", turn.assistant_message or "")
        block = f"第 {turn.sequence} 轮用户：\n{historical_user}\n第 {turn.sequence} 轮助手：\n{historical_assistant}\n\n"
        size = len(block.encode("utf-8"))
        if size > available:
            break
        selected.append(block)
        available -= size
    selected.reverse()
    return header + "".join(selected) + current, len(selected)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_file(task_id: str) -> Path:
    return TASK_DIR / task_id / "task.json"


def _read_task(task_id: str) -> dict:
    path = _task_file(task_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="task not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _validated_task(task_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return _read_task(task_id)


def _optional_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None


def _optional_int(path: Path) -> int | None:
    value = _optional_text(path)
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _reported_skills(result_dir: Path, filename: str, available: list[str]) -> list[str]:
    try:
        reported = json.loads((result_dir / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(reported, list):
        return []
    return [name for name in available if name in reported]


def _loaded_skills(result_dir: Path, available: list[str]) -> list[str]:
    return _reported_skills(result_dir, "loaded-skills.json", available)


def _read_skills(result_dir: Path, available: list[str]) -> list[str]:
    return _reported_skills(result_dir, "read-skills.json", available)


def _base_runner_config() -> str:
    candidates = (
        RUNNER_CONFIG_TEMPLATE,
        Path(__file__).resolve().parents[2] / "runner" / "config.toml",
    )
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise RuntimeError("Runner config template is missing")


def _write_task(task: dict) -> None:
    path = _task_file(task["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _active_count() -> int:
    return sum(
        json.loads(path.read_text(encoding="utf-8"))["status"] in ACTIVE_TASK_STATUSES
        for path in TASK_DIR.glob("*/task.json")
    )


def _create_task_files(repository: Repository | None, prompt: str, user_id: str,
                       skill_ids: list[str], mcp_ids: list[str],
                       conversation_id: str | None = None, turn_id: str | None = None,
                       execution_mode: str = "repository_snapshot", turn_sequence: int | None = None,
                       mcp_routing_text: str | None = None) -> dict:
    """Caller holds _lock; both one-shot and conversational tasks use this path."""
    if not RUNNER_IMAGE or not os.environ.get("MODEL_API_KEY"):
        raise HTTPException(status_code=503, detail="Runner image or model key is missing")
    if _active_count() >= MAX_ACTIVE_TASKS:
        raise HTTPException(status_code=429, detail="Runner capacity is full")
    task_id = uuid.uuid4().hex
    job_dir = TASK_DIR / task_id / "job"
    try:
        job_dir.mkdir(parents=True)
        (job_dir / "execution-mode.txt").write_text(execution_mode, encoding="utf-8")
        if turn_sequence is not None:
            (job_dir / "turn-sequence.txt").write_text(str(turn_sequence), encoding="utf-8")
        if repository is not None:
            (job_dir / "repository-url.txt").write_text(repository.url, encoding="utf-8")
            (job_dir / "repository-ref.txt").write_text(repository.ref, encoding="utf-8")
            (job_dir / "repository-refresh.txt").write_text("1" if repository.refresh else "0", encoding="utf-8")
        with _skill_lock:
            try:
                task_skills = skill_store.snapshot(
                    skill_store.user_dir(DATA_DIR, user_id), TASK_DIR / task_id / "skills", skill_ids
                )
            except KeyError as exc:
                raise HTTPException(status_code=409, detail=f"Skill is not installed: {exc.args[0]}") from exc
        with session_factory()() as db:
            try:
                if mcp_ids:
                    task_mcps = mcp_store.selected_catalog(db, user_id, mcp_ids)
                    mcp_selection = "manual"
                else:
                    task_mcps = mcp_store.routed_catalog(db, user_id, mcp_routing_text or prompt)
                    mcp_selection = "auto" if task_mcps else "none"
            except KeyError as exc:
                raise HTTPException(status_code=409, detail=f"MCP is not installed: {exc.args[0]}") from exc
        (TASK_DIR / task_id / "codex-config.toml").write_text(
            mcp_store.render_codex_config(_base_runner_config(), task_mcps), encoding="utf-8"
        )
        requested_skills = list(task_skills)
        native_prompt = (" ".join(f"${skill_id}" for skill_id in requested_skills) + "\n\n" + prompt
                         if requested_skills else prompt)
        (job_dir / "prompt.txt").write_text(native_prompt, encoding="utf-8")
        results = RESULT_DIR / task_id
        results.mkdir(parents=True)
        os.chown(results, 10001, 10001)
        task = {
            "task_id": task_id, "status": "starting", "created_at": _now(),
            "user_id": user_id, "conversation_id": conversation_id, "turn_id": turn_id,
            "execution_mode": execution_mode, "turn_sequence": turn_sequence,
            "skills": task_skills, "requested_skills": requested_skills,
            "mcps": [item.id for item in task_mcps],
            "mcp_selection": mcp_selection,
            "auto_mcps": [item.id for item in task_mcps] if mcp_selection == "auto" else [],
        }
        _write_task(task)
        return task
    except Exception:
        shutil.rmtree(TASK_DIR / task_id, ignore_errors=True)
        shutil.rmtree(RESULT_DIR / task_id, ignore_errors=True)
        raise


def _task_links(task_id: str) -> dict:
    return {
        "self": f"/v1/tasks/{task_id}",
        "events": f"/v1/tasks/{task_id}/events",
        "result": f"/v1/tasks/{task_id}/result",
        "cancel": f"/v1/tasks/{task_id}/cancel",
    }


def _sync_turn_from_task(task: dict) -> None:
    """Idempotent finalization, also called by reads after a manager restart."""
    if not task.get("turn_id") or task["status"] not in FINAL_TASK_STATUSES:
        return
    with session_factory()() as db, db.begin():
        turn = db.get(Turn, task["turn_id"])
        if turn is None or turn.status in FINAL_TASK_STATUSES:
            return
        turn.status = task["status"]
        turn.assistant_message = _optional_text(RESULT_DIR / task["task_id"] / "final-message.md")
        turn.completed_at = utcnow()
        conversation = db.get(Conversation, turn.conversation_id)
        if conversation:
            conversation.updated_at = turn.completed_at


def _sync_conversation_turns(conversation_id: str) -> None:
    with session_factory()() as db:
        pending = db.scalars(select(Turn.task_id).where(
            Turn.conversation_id == conversation_id,
            Turn.status.in_(ACTIVE_TASK_STATUSES),
        )).all()
    for task_id in pending:
        try:
            task = _read_task(task_id)
        except HTTPException:
            continue
        if task["status"] in FINAL_TASK_STATUSES:
            _sync_turn_from_task(task)


def _prepare_repository(task_id: str) -> tuple[Path, bool, bool, str, float]:
    job_dir = TASK_DIR / task_id / "job"
    url = (job_dir / "repository-url.txt").read_text(encoding="utf-8")
    ref = (job_dir / "repository-ref.txt").read_text(encoding="utf-8")
    refresh = (job_dir / "repository-refresh.txt").read_text(encoding="utf-8") == "1"
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    mirror = REPO_CACHE_DIR / f"{cache_key}.git"
    cache_hit = mirror.is_dir()
    refreshed = False
    started = time.monotonic()
    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    if not cache_hit:
        REPO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        staging = REPO_CACHE_DIR / f".{cache_key}.{uuid.uuid4().hex}.tmp"
        try:
            subprocess.run(
                ["git", "clone", "--mirror", "--", url, str(staging)],
                check=True,
                timeout=CACHE_CLONE_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                env=git_env,
            )
            staging.rename(mirror)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    elif refresh:
        subprocess.run(
            ["git", "--git-dir", str(mirror), "remote", "update", "--prune"],
            check=True,
            timeout=CACHE_CLONE_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=git_env,
        )
        refreshed = True

    commit = subprocess.run(
        ["git", "--git-dir", str(mirror), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (job_dir / "repository-commit.txt").write_text(commit, encoding="utf-8")
    host_mirror = HOST_DATA_DIR / "repo-cache" / mirror.name
    return host_mirror, cache_hit, refreshed, commit, round(time.monotonic() - started, 3)


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
        if task["status"] in FINAL_TASK_STATUSES:
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
                event_type = (
                    "task.completed" if task["status"] == "succeeded"
                    else "task.cancelled" if task["status"] == "cancelled"
                    else "task.failed"
                )
                yield _sse_event(task_id, sequence, event_type, {"status": task["status"]})
            return

        if time.monotonic() - last_heartbeat >= SSE_HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            last_heartbeat = time.monotonic()
        await asyncio.sleep(SSE_POLL_SECONDS)


def _run_task(task_id: str) -> None:
    container = None
    task = _read_task(task_id)
    workspace_before = None
    try:
        if task.get("cancel_requested_at"):
            task["status"] = "cancelled"
            return
        execution_mode = task.get("execution_mode", "repository_snapshot")
        host_mirror = None
        if execution_mode == "repository_snapshot":
            with _cache_lock:
                host_mirror, cache_hit, refreshed, commit, cache_seconds = _prepare_repository(task_id)
            task["cache_hit"] = cache_hit
            task["cache_refreshed"] = refreshed
            task["cache_prepare_seconds"] = cache_seconds
            task["source_commit"] = commit
        else:
            task["cache_hit"] = None
            task["cache_refreshed"] = None
            task["cache_prepare_seconds"] = 0
            task["source_commit"] = None
            workspace_before = _workspace_snapshot(
                _workspace_path(task["user_id"], task["conversation_id"])
            )
        latest = _read_task(task_id)
        if latest.get("cancel_requested_at"):
            task = latest
            task["status"] = "cancelled"
            return
        _write_task(task)
        client = docker.from_env()
        volumes = {
            str(HOST_DATA_DIR / "tasks" / task_id / "job"): {"bind": "/job", "mode": "ro"},
            str(HOST_DATA_DIR / "results" / task_id): {"bind": "/results", "mode": "rw"},
            str(HOST_DATA_DIR / "tasks" / task_id / "skills"): {
                "bind": "/home/codex/.agents/skills", "mode": "ro"
            },
            str(HOST_DATA_DIR / "tasks" / task_id / "codex-config.toml"): {
                "bind": "/home/codex/.codex/config.toml", "mode": "ro"
            },
        }
        if execution_mode == "repository_snapshot":
            volumes[str(host_mirror)] = {"bind": "/cache/repository.git", "mode": "ro"}
        else:
            volumes[str(_host_workspace_path(task["user_id"], task["conversation_id"]))] = {
                "bind": "/workspace", "mode": "rw"
            }
        container = client.containers.run(
            RUNNER_IMAGE,
            detach=True,
            name=f"codex-task-{task_id}",
            entrypoint="/usr/local/bin/run-codex-task",
            environment={"MODEL_API_KEY": os.environ["MODEL_API_KEY"]},
            volumes=volumes,
            labels={"codex.mvp.managed": "true", "codex.mvp.task_id": task_id},
            mem_limit="2g",
            nano_cpus=1_000_000_000,
            pids_limit=256,
            security_opt=["no-new-privileges:true"],
        )
        latest = _read_task(task_id)
        if latest.get("cancel_requested_at"):
            task = latest
            try:
                container.stop(timeout=10)
            except DockerException:
                container.kill()
            task["status"] = "cancelled"
            return
        task["status"] = "running"
        task["started_at"] = _now()
        _write_task(task)
        deadline = time.monotonic() + TASK_TIMEOUT_SECONDS if TASK_TIMEOUT_SECONDS > 0 else None
        while deadline is None or time.monotonic() < deadline:
            latest = _read_task(task_id)
            if latest.get("cancel_requested_at"):
                task = latest
                try:
                    container.stop(timeout=10)
                except DockerException:
                    container.kill()
                task["status"] = "cancelled"
                break
            container.reload()
            if container.status == "exited":
                break
            time.sleep(1)
        else:
            try:
                container.stop(timeout=10)
            except DockerException:
                container.kill()
            task["status"] = "timed_out"
        if task["status"] not in {"timed_out", "cancelled"}:
            latest = _read_task(task_id)
            if latest.get("cancel_requested_at"):
                task = latest
                task["status"] = "cancelled"
            else:
                exit_file = RESULT_DIR / task_id / "exit-code.txt"
                task["status"] = "succeeded" if exit_file.is_file() and exit_file.read_text().strip() == "0" else "failed"
    except subprocess.TimeoutExpired:
        latest = _read_task(task_id)
        if latest.get("cancel_requested_at"):
            task = latest
            task["status"] = "cancelled"
        else:
            task["status"] = "timed_out"
            task["error"] = "RepositoryCacheTimeout"
    except (DockerException, OSError, KeyError, subprocess.CalledProcessError) as exc:
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
        if workspace_before is not None and task.get("execution_mode") == "conversation_workspace" and task.get("conversation_id"):
            try:
                workspace_after = _workspace_snapshot(
                    _workspace_path(task["user_id"], task["conversation_id"])
                )
                changes = _workspace_changes(workspace_before, workspace_after)
                (RESULT_DIR / task_id / "workspace-changes.json").write_text(
                    json.dumps(changes, ensure_ascii=False), encoding="utf-8"
                )
                _save_turn_files(
                    task_id, _workspace_path(task["user_id"], task["conversation_id"]), changes
                )
            except OSError as exc:
                task["workspace_snapshot_error"] = type(exc).__name__
        task["finished_at"] = _now()
        _write_task(task)
        if "cleanup_error" not in task:
            shutil.rmtree(TASK_DIR / task_id / "skills", ignore_errors=True)
            (TASK_DIR / task_id / "codex-config.toml").unlink(missing_ok=True)
        try:
            _sync_turn_from_task(task)
        except Exception as exc:
            print(f"conversation finalization failed for {task_id}: {exc}", flush=True)


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


@app.get("/v1/me")
async def current_user(request: Request) -> dict:
    return {"user_id": _user_id(request)}


def _public_skills_dir() -> Path:
    public_root = DATA_DIR / "skills" / "public"
    skill_store.seed_public(SKILL_BUNDLE_DIR, public_root)
    return public_root


@app.get("/v1/skills/catalog")
async def list_skill_catalog(request: Request) -> dict:
    user_id = _user_id(request)
    with _skill_lock:
        public = skill_store.list_skills(_public_skills_dir())
        installed = {item["id"] for item in skill_store.list_skills(skill_store.user_dir(DATA_DIR, user_id))}
    return {"items": [{**item, "installed": item["id"] in installed} for item in public]}


@app.get("/v1/skills")
async def list_user_skills(request: Request) -> dict:
    user_id = _user_id(request)
    with _skill_lock:
        items = skill_store.list_skills(skill_store.user_dir(DATA_DIR, user_id))
        labels = {item["id"]: item for item in skill_store.list_skills(_public_skills_dir())}
    return {"items": [{**item, **labels.get(item["id"], {})} for item in items]}


@app.post("/v1/skills/{skill_id}/install")
async def install_skill(skill_id: str, request: Request) -> dict:
    user_id = _user_id(request)
    with _skill_lock:
        try:
            skill_store.install(_public_skills_dir(), skill_store.user_dir(DATA_DIR, user_id), skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="skill not found") from exc
        except OverflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": skill_id, "installed": True}


@app.delete("/v1/skills/{skill_id}")
async def uninstall_skill(skill_id: str, request: Request) -> dict:
    user_id = _user_id(request)
    with _skill_lock:
        try:
            removed = skill_store.uninstall(skill_store.user_dir(DATA_DIR, user_id), skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="skill not found") from exc
    if not removed:
        raise HTTPException(status_code=404, detail="skill not installed")
    return {"id": skill_id, "installed": False}


@app.get("/v1/mcp/catalog")
async def list_mcp_catalog(request: Request) -> dict:
    user_id = _user_id(request)
    with session_factory()() as db:
        installed = mcp_store.installed_ids(db, user_id)
    return {"items": [
        {**item.public_data(), "installed": item.id in installed}
        for item in mcp_store.CATALOG
    ]}


@app.get("/v1/mcp")
async def list_user_mcps(request: Request) -> dict:
    user_id = _user_id(request)
    with session_factory()() as db:
        items = mcp_store.installed_catalog(db, user_id)
    return {"items": [{**item.public_data(), "installed": True} for item in items]}


@app.post("/v1/mcp/{mcp_id}/install")
async def install_mcp(mcp_id: str, request: Request) -> dict:
    user_id = _user_id(request)
    try:
        with session_factory()() as db, db.begin():
            mcp_store.install(db, user_id, mcp_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="MCP not found") from exc
    except OverflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": mcp_id, "installed": True}


@app.delete("/v1/mcp/{mcp_id}")
async def uninstall_mcp(mcp_id: str, request: Request) -> dict:
    user_id = _user_id(request)
    try:
        with session_factory()() as db, db.begin():
            removed = mcp_store.uninstall(db, user_id, mcp_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="MCP not found") from exc
    if not removed:
        raise HTTPException(status_code=404, detail="MCP not installed")
    return {"id": mcp_id, "installed": False}


@app.post("/v1/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(body: ConversationRequest, request: Request) -> dict:
    user_id = _user_id(request)
    conversation_id = uuid.uuid4().hex
    title = body.title.strip() or "新对话"
    conversation = Conversation(
        id=conversation_id, user_id=user_id, title=title,
        repository_url="", repository_ref="", repository_refresh=False,
        workspace_type="conversation_workspace", workspace_status="ready",
    )
    workspace = _create_workspace(user_id, conversation_id)
    try:
        with session_factory()() as db, db.begin():
            db.add(conversation)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    return _conversation_data(conversation)


@app.get("/v1/conversations")
async def list_conversations(request: Request, limit: int = Query(50, ge=1, le=100),
                             offset: int = Query(0, ge=0)) -> dict:
    user_id = _user_id(request)
    with session_factory()() as db:
        ids = db.scalars(select(Conversation.id).where(Conversation.user_id == user_id)
                         .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                         .offset(offset).limit(limit + 1)).all()
    for conversation_id in ids[:limit]:
        _sync_conversation_turns(conversation_id)
    with session_factory()() as db:
        conversations = db.scalars(select(Conversation).where(Conversation.id.in_(ids[:limit]))
                                   .order_by(Conversation.updated_at.desc(), Conversation.id.desc())).all()
        items = []
        for conversation in conversations:
            last = db.scalar(select(Turn).where(Turn.conversation_id == conversation.id)
                             .order_by(Turn.sequence.desc()).limit(1))
            item = _conversation_data(conversation)
            item["last_message"] = last.user_message[:120] if last else None
            item["last_status"] = last.status if last else None
            items.append(item)
    return {"items": items, "has_more": len(ids) > limit, "next_offset": offset + len(items) if len(ids) > limit else None}


@app.get("/v1/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request) -> dict:
    user_id = _user_id(request)
    with session_factory()() as db:
        _owned_conversation(db, conversation_id, user_id)
    _sync_conversation_turns(conversation_id)
    with session_factory()() as db:
        conversation = _owned_conversation(db, conversation_id, user_id)
        active = db.scalar(select(Turn).where(
            Turn.conversation_id == conversation_id,
            Turn.status.in_(["starting", "running"]),
        ).order_by(Turn.sequence.desc()).limit(1))
        data = _conversation_data(conversation)
        data["active_task_id"] = active.task_id if active else None
        return data


def _owned_workspace(conversation_id: str, user_id: str) -> tuple[Conversation, Path]:
    with session_factory()() as db:
        conversation = _owned_conversation(db, conversation_id, user_id)
        if conversation.workspace_type != "conversation_workspace":
            raise HTTPException(status_code=409, detail="Conversation does not use a persistent workspace")
    workspace = _workspace_path(user_id, conversation_id)
    if not workspace.is_dir():
        raise HTTPException(status_code=409, detail="Conversation workspace is missing")
    return conversation, workspace


@app.get("/v1/conversations/{conversation_id}/files")
async def list_workspace_files(conversation_id: str, request: Request,
                               path: str = Query("", max_length=1024)) -> dict:
    user_id = _user_id(request)
    _, workspace = _owned_workspace(conversation_id, user_id)
    directory = _safe_workspace_entry(workspace, path)
    if not directory.is_dir():
        raise HTTPException(status_code=404, detail="workspace directory not found")
    items = []
    for entry in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if entry.name == ".git":
            continue
        relative = entry.relative_to(workspace).as_posix()
        if entry.is_symlink():
            entry_type = "symlink"
            size = None
        elif entry.is_dir():
            entry_type = "directory"
            size = None
        else:
            entry_type = "file"
            size = entry.stat().st_size
        item = {"path": relative, "name": entry.name, "type": entry_type, "size_bytes": size}
        if entry_type == "file":
            item.update(_workspace_file_metadata(relative, {"size_bytes": size}))
            item["type"] = "file"
        items.append(item)
    return {"path": path, "items": items}


@app.get("/v1/conversations/{conversation_id}/files/content")
async def download_workspace_file(conversation_id: str, request: Request,
                                  path: str = Query(..., min_length=1, max_length=1024),
                                  download: bool = Query(False)) -> FileResponse:
    user_id = _user_id(request)
    _, workspace = _owned_workspace(conversation_id, user_id)
    file_path = _safe_workspace_entry(workspace, path)
    if file_path.is_symlink() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="workspace file not found")
    return _file_preview_response(file_path, download)


def _file_preview_response(file_path: Path, download: bool) -> FileResponse:
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    preview_type = _preview_type(file_path, mime_type)
    if not download and preview_type in {"markdown", "text", "code"}:
        response_type = "text/plain; charset=utf-8"
    elif not download and preview_type in {"json", "image", "pdf"}:
        response_type = mime_type
    else:
        response_type = "application/octet-stream"
    return FileResponse(
        file_path,
        filename=file_path.name,
        media_type=response_type,
        content_disposition_type="inline" if not download and preview_type else "attachment",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@app.get("/v1/tasks/{task_id}/files/content")
async def download_turn_file(task_id: str, request: Request,
                             path: str = Query(..., min_length=1, max_length=1024),
                             download: bool = Query(False)) -> FileResponse:
    task = _owned_task(task_id, _user_id(request))
    if task["status"] in ACTIVE_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="task is still running")
    changes = _stored_workspace_changes(task_id)
    allowed = {item["path"] for kind in ("created", "modified") for item in changes[kind]}
    if path not in allowed:
        raise HTTPException(status_code=404, detail="turn file not found")
    file_path = _safe_workspace_entry(RESULT_DIR / task_id / "turn-files", path)
    if file_path.is_symlink() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="turn file not found")
    return _file_preview_response(file_path, download)


@app.get("/v1/tasks/{task_id}/files/archive")
async def download_turn_files_archive(task_id: str, request: Request) -> FileResponse:
    task = _owned_task(task_id, _user_id(request))
    if task["status"] in ACTIVE_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="task is still running")
    archive = RESULT_DIR / task_id / "turn-files.tar.gz"
    if not archive.is_file():
        raise HTTPException(status_code=404, detail="turn archive not found")
    return FileResponse(
        archive, filename=f"turn-{task_id}-files.tar.gz",
        media_type="application/gzip", headers={"X-Content-Type-Options": "nosniff"},
    )


@app.get("/v1/conversations/{conversation_id}/workspace")
async def download_workspace(conversation_id: str, request: Request) -> FileResponse:
    user_id = _user_id(request)
    _, workspace = _owned_workspace(conversation_id, user_id)
    with session_factory()() as db:
        active = db.scalar(select(Turn.id).where(
            Turn.conversation_id == conversation_id,
            Turn.status.in_(["starting", "running"]),
        ).limit(1))
    if active:
        raise HTTPException(status_code=409, detail="Workspace is being modified by a running turn")
    temporary = tempfile.NamedTemporaryFile(prefix=f"workspace-{conversation_id}-", suffix=".tar.gz", delete=False)
    temporary.close()
    archive_path = Path(temporary.name)
    excluded = {".git", "node_modules", "target", "__pycache__"}

    def archive_filter(info: tarfile.TarInfo):
        return None if any(part in excluded for part in Path(info.name).parts) else info

    try:
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(workspace, arcname="workspace", recursive=True, filter=archive_filter)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return FileResponse(
        archive_path,
        filename=f"workspace-{conversation_id}.tar.gz",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@app.delete("/v1/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request) -> dict:
    user_id = _user_id(request)
    task_ids: list[str] = []
    with _lock:
        with session_factory()() as db, db.begin():
            conversation = _owned_conversation(db, conversation_id, user_id, lock=True)
            active = db.scalar(select(Turn.id).where(
                Turn.conversation_id == conversation_id,
                Turn.status.in_(["starting", "running"]),
            ).limit(1))
            if active:
                raise HTTPException(status_code=409, detail="Conversation has a running turn")
            task_ids = list(db.scalars(select(Turn.task_id).where(
                Turn.conversation_id == conversation_id
            )).all())
            db.execute(delete(Turn).where(Turn.conversation_id == conversation_id))
            db.delete(conversation)
        shutil.rmtree(_workspace_path(user_id, conversation_id), ignore_errors=True)
        for task_id in task_ids:
            shutil.rmtree(TASK_DIR / task_id, ignore_errors=True)
            shutil.rmtree(RESULT_DIR / task_id, ignore_errors=True)
    return {"id": conversation_id, "deleted": True}


@app.get("/v1/conversations/{conversation_id}/turns")
async def list_turns(conversation_id: str, request: Request, limit: int = Query(20, ge=1, le=100),
                     before_seq: int | None = Query(None, ge=1)) -> dict:
    user_id = _user_id(request)
    with session_factory()() as db:
        _owned_conversation(db, conversation_id, user_id)
    _sync_conversation_turns(conversation_id)
    with session_factory()() as db:
        _owned_conversation(db, conversation_id, user_id)
        statement = select(Turn).where(Turn.conversation_id == conversation_id)
        if before_seq is not None:
            statement = statement.where(Turn.sequence < before_seq)
        rows = db.scalars(statement.order_by(Turn.sequence.desc()).limit(limit + 1)).all()
        has_more = len(rows) > limit
        page = list(reversed(rows[:limit]))
        return {
            "items": [_turn_data(turn) for turn in page],
            "has_more": has_more,
            "next_before_seq": page[0].sequence if has_more else None,
        }


@app.post("/v1/conversations/{conversation_id}/turns", status_code=status.HTTP_202_ACCEPTED)
async def create_turn(conversation_id: str, body: TurnRequest, request: Request,
                      background_tasks: BackgroundTasks) -> dict:
    user_id = _user_id(request)
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message must not be blank")
    with session_factory()() as db:
        _owned_conversation(db, conversation_id, user_id)
    _sync_conversation_turns(conversation_id)
    task = None
    with _lock:
        try:
            with session_factory()() as db, db.begin():
                conversation = _owned_conversation(db, conversation_id, user_id, lock=True)
                if body.request_id:
                    existing = db.scalar(select(Turn).where(
                        Turn.conversation_id == conversation_id, Turn.request_id == body.request_id
                    ))
                    if existing:
                        if existing.user_message != message:
                            raise HTTPException(status_code=409, detail="request_id already used for a different message")
                        return {
                            "turn_id": existing.id, "task_id": existing.task_id,
                            "context_rounds_used": existing.context_rounds_used,
                            "links": _task_links(existing.task_id),
                        }
                active = db.scalar(select(Turn.id).where(
                    Turn.conversation_id == conversation_id,
                    Turn.status.in_(ACTIVE_TASK_STATUSES),
                ).limit(1))
                if active:
                    raise HTTPException(status_code=409, detail="This conversation already has a running turn")
                recent = db.scalars(select(Turn).where(
                    Turn.conversation_id == conversation_id, Turn.status == "succeeded",
                    Turn.assistant_message.is_not(None),
                ).order_by(Turn.sequence.desc()).limit(MAX_HISTORY_ROUNDS)).all()
                prompt, rounds = _render_prompt(message, list(reversed(recent)))
                routing_parts = [f"本轮：{message}"]
                if recent:
                    routing_parts.append(f"上轮：{recent[0].user_message}")
                mcp_routing_text = "\n".join(routing_parts)
                next_sequence = (db.scalar(select(func.max(Turn.sequence)).where(
                    Turn.conversation_id == conversation_id
                )) or 0) + 1
                turn_id = uuid.uuid4().hex
                if conversation.workspace_type == "conversation_workspace":
                    workspace = _workspace_path(user_id, conversation_id)
                    if not workspace.is_dir():
                        raise HTTPException(status_code=409, detail="Conversation workspace is missing")
                    repository = None
                else:
                    repository = Repository(url=conversation.repository_url,
                                            ref=conversation.repository_ref,
                                            refresh=conversation.repository_refresh)
                task = _create_task_files(
                    repository, prompt, user_id, body.skill_ids, body.mcp_ids,
                    conversation_id, turn_id, conversation.workspace_type, next_sequence,
                    mcp_routing_text=mcp_routing_text,
                )
                db.add(Turn(
                    id=turn_id, conversation_id=conversation_id, sequence=next_sequence,
                    request_id=body.request_id, user_message=message, task_id=task["task_id"],
                    status="starting", context_rounds_used=rounds,
                ))
                if next_sequence == 1:
                    conversation.title = message.replace("\n", " ")[:60]
                conversation.updated_at = utcnow()
        except Exception:
            if task:
                shutil.rmtree(TASK_DIR / task["task_id"], ignore_errors=True)
                shutil.rmtree(RESULT_DIR / task["task_id"], ignore_errors=True)
            raise
    background_tasks.add_task(_run_task, task["task_id"])
    return {"turn_id": turn_id, "task_id": task["task_id"], "context_rounds_used": rounds,
            "mcps": task.get("mcps", []), "mcp_selection": task.get("mcp_selection", "none"),
            "auto_mcps": task.get("auto_mcps", []),
            "links": _task_links(task["task_id"])}


@app.post("/v1/tasks", status_code=status.HTTP_202_ACCEPTED)
async def create_task(body: TaskRequest, request: Request, background_tasks: BackgroundTasks) -> dict:
    user_id = _user_id(request)
    with _lock:
        task = _create_task_files(body.repository, body.prompt, user_id, body.skill_ids, body.mcp_ids)
    background_tasks.add_task(_run_task, task["task_id"])
    return {
        "task_id": task["task_id"],
        "status": "starting",
        "links": _task_links(task["task_id"]),
    }


@app.get("/v1/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    return _owned_task(task_id, _user_id(request))


@app.post("/v1/tasks/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_task(task_id: str, request: Request) -> dict:
    user_id = _user_id(request)
    with _lock:
        task = _owned_task(task_id, user_id)
        if task["status"] in FINAL_TASK_STATUSES:
            return {"task_id": task_id, "status": task["status"]}
        task["cancel_requested_at"] = task.get("cancel_requested_at") or _now()
        task["status"] = "cancelling"
        _write_task(task)
    try:
        container = docker.from_env().containers.get(f"codex-task-{task_id}")
        container.stop(timeout=10)
    except DockerException:
        # The worker may still be preparing the repository or may have already
        # observed the request. It checks cancel_requested_at before launch and
        # on every wait iteration.
        pass
    return {"task_id": task_id, "status": "cancelling"}


@app.get("/v1/tasks/{task_id}/events")
async def get_task_events(task_id: str, request: Request) -> StreamingResponse:
    _owned_task(task_id, _user_id(request))
    last_event_id = request.headers.get("last-event-id", "0")
    if not last_event_id.isdecimal():
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a nonnegative integer")
    return StreamingResponse(
        _stream_task_events(task_id, int(last_event_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/v1/tasks/{task_id}/result")
async def get_task_result(task_id: str, request: Request):
    task = _owned_task(task_id, _user_id(request))
    if task["status"] in ACTIVE_TASK_STATUSES:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"task_id": task_id, "status": task["status"]},
            headers={"Retry-After": "2"},
        )

    result_dir = RESULT_DIR / task_id
    total_ms = None
    if task.get("created_at") and task.get("finished_at"):
        total_ms = round(
            (
                datetime.fromisoformat(task["finished_at"])
                - datetime.fromisoformat(task["created_at"])
            ).total_seconds()
            * 1000
        )
    artifacts = [
        {
            "name": name,
            "size_bytes": (result_dir / name).stat().st_size,
            "download_url": f"/v1/tasks/{task_id}/artifacts/{name}",
        }
        for name in sorted(DOWNLOADABLE_ARTIFACTS)
        if (result_dir / name).is_file()
    ]
    return {
        "task_id": task_id,
        "status": task["status"],
        "exit_code": _optional_int(result_dir / "exit-code.txt"),
        "final_message": _optional_text(result_dir / "final-message.md"),
        "git_status": _optional_text(result_dir / "git-status.txt"),
        "diff": _optional_text(result_dir / "changes.diff"),
        "repository": {
            "commit": task.get("source_commit"),
            "cache_hit": task.get("cache_hit"),
            "cache_refreshed": task.get("cache_refreshed"),
        },
        "workspace": {
            "type": task.get("execution_mode", "repository_snapshot"),
            "conversation_id": task.get("conversation_id"),
            "turn_sequence": task.get("turn_sequence"),
        },
        "workspace_changes": _workspace_changes_response(task),
        "skills": task.get("skills", []),
        "requested_skills": task.get("requested_skills", []),
        "loaded_skills": _loaded_skills(result_dir, task.get("skills", [])),
        "read_skills": _read_skills(result_dir, task.get("skills", [])),
        "mcps": task.get("mcps", []),
        "timings_ms": {
            "total": total_ms,
            "cache_prepare": (
                round(task["cache_prepare_seconds"] * 1000)
                if task.get("cache_prepare_seconds") is not None
                else None
            ),
            "local_clone": _optional_int(result_dir / "clone-milliseconds.txt"),
            "codex": _optional_int(result_dir / "codex-milliseconds.txt"),
        },
        "artifacts": artifacts,
        "error": task.get("error"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
    }


@app.get("/v1/tasks/{task_id}/artifacts/{name}")
async def download_task_artifact(task_id: str, name: str, request: Request) -> FileResponse:
    task = _owned_task(task_id, _user_id(request))
    if task["status"] in {"starting", "running"}:
        raise HTTPException(status_code=409, detail="task is still running")
    if name not in DOWNLOADABLE_ARTIFACTS:
        raise HTTPException(status_code=404, detail="artifact not found")
    path = RESULT_DIR / task_id / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(path, filename=name)
