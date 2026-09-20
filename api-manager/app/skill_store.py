"""Filesystem-backed public Skill catalog and isolated per-user installations."""

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path

import yaml


SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
MAX_INSTALLED = 20
MAX_SKILL_BYTES = 1_000_000


def user_dir(data_dir: Path, user_id: str) -> Path:
    # Never interpolate an externally configured user ID into a filesystem path.
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return data_dir / "skills" / "users" / digest / "skills"


def _check_tree(skill_dir: Path) -> None:
    if skill_dir.is_symlink() or not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
        raise ValueError("Invalid Skill directory")
    total = 0
    for path in skill_dir.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Skill contains an unsupported file")
        if path.is_file():
            total += path.stat().st_size
            if total > MAX_SKILL_BYTES:
                raise ValueError("Skill exceeds size limit")


def _skill_metadata(skill_id: str, skill_dir: Path, labels: dict | None = None) -> dict:
    _check_tree(skill_dir)
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    if not content.startswith("---\n") or "\n---\n" not in content[4:]:
        raise ValueError("Skill has no YAML frontmatter")
    header = yaml.safe_load(content.split("\n---\n", 1)[0][4:])
    if not isinstance(header, dict) or header.get("name") != skill_id or not isinstance(header.get("description"), str):
        raise ValueError("Skill metadata does not match its directory")
    labels = labels or {}
    return {
        "id": skill_id,
        "name": labels.get("display_name", skill_id),
        "description": labels.get("summary", header["description"]),
        "category": labels.get("category", "其他"),
    }


def list_skills(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    labels_path = root / "catalog.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.is_file() else {}
    return [_skill_metadata(path.name, path, labels.get(path.name))
            for path in sorted(root.iterdir())
            if path.is_dir() and not path.is_symlink() and SKILL_ID.fullmatch(path.name)
            and (path / "SKILL.md").is_file()]


def seed_public(bundled_root: Path, public_root: Path) -> None:
    """Populate missing bundled Skills while preserving administrator edits."""
    marker = public_root / ".seeded"
    if marker.is_file():
        return
    public_root.mkdir(parents=True, exist_ok=True)
    if not bundled_root.is_dir():
        return
    for skill in list_skills(bundled_root):
        source = bundled_root / skill["id"]
        destination = public_root / skill["id"]
        if not destination.exists():
            shutil.copytree(source, destination)
    source_labels = bundled_root / "catalog.json"
    destination_labels = public_root / "catalog.json"
    if source_labels.is_file() and not destination_labels.exists():
        shutil.copy2(source_labels, destination_labels)
    marker.write_text("1\n", encoding="utf-8")


def install(public_root: Path, user_root: Path, skill_id: str) -> bool:
    if not SKILL_ID.fullmatch(skill_id):
        raise KeyError(skill_id)
    source = public_root / skill_id
    if not source.is_dir() or source.is_symlink():
        raise KeyError(skill_id)
    _skill_metadata(skill_id, source)
    destination = user_root / skill_id
    if destination.is_dir():
        return False
    if len(list_skills(user_root)) >= MAX_INSTALLED:
        raise OverflowError("Too many installed Skills")
    user_root.mkdir(parents=True, exist_ok=True)
    temporary = user_root / f".{skill_id}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copytree(source, temporary)
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return True


def uninstall(user_root: Path, skill_id: str) -> bool:
    if not SKILL_ID.fullmatch(skill_id):
        raise KeyError(skill_id)
    destination = user_root / skill_id
    if not destination.is_dir() or destination.is_symlink():
        return False
    shutil.rmtree(destination)
    return True


def snapshot(user_root: Path, task_root: Path, selected_ids: list[str]) -> list[str]:
    """Freeze only the selected installed Skills for a new Runner."""
    task_root.mkdir(parents=True)
    installed = {skill["id"]: skill for skill in list_skills(user_root)}
    missing = [skill_id for skill_id in selected_ids if skill_id not in installed]
    if missing:
        raise KeyError(missing[0])
    for skill_id in selected_ids:
        shutil.copytree(user_root / skill_id, task_root / skill_id)
    return list(selected_ids)
