#!/usr/bin/env python3
"""Merge Codex JSONL with native Skill loads and observed Skill file reads."""

import json
import re
import subprocess
import sys
import threading
from pathlib import Path


EVENTS = Path("/results/codex-events.jsonl")
STDERR = Path("/results/codex-stderr.log")
SKILLS = Path("/home/codex/.agents/skills")
MARKER = "CODEX_MVP_SKILL_LOADED\t"
SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def _read_skill_docs(skills_root: Path) -> dict[str, str]:
    if not skills_root.is_dir():
        return {}
    docs = {}
    for directory in skills_root.iterdir():
        path = directory / "SKILL.md"
        if SKILL_ID.fullmatch(directory.name) and path.is_file():
            docs[directory.name] = path.read_text(encoding="utf-8").strip()
    return docs


def _skills_read_by_command(line: bytes, skills_root: Path,
                            docs: dict[str, str]) -> list[str]:
    try:
        event = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    item = event.get("item") or {}
    if event.get("type") != "item.completed" or item.get("type") != "command_execution":
        return []
    if item.get("exit_code") != 0:
        return []
    command = item.get("command") or ""
    output = item.get("aggregated_output") or ""
    return [skill_id for skill_id, doc in docs.items()
            if doc and str(skills_root / skill_id / "SKILL.md") in command and doc in output]


def run(command: list[str], events_path: Path = EVENTS, stderr_path: Path = STDERR,
        skills_root: Path = SKILLS) -> int:
    lock = threading.Lock()
    loaded = set()
    read = set()
    skill_docs = _read_skill_docs(skills_root)
    with events_path.open("wb") as events, stderr_path.open("wb") as errors:
        process = subprocess.Popen(command, stdin=sys.stdin.buffer, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)

        def copy_stdout() -> None:
            for line in process.stdout:
                read_skills = _skills_read_by_command(line, skills_root, skill_docs)
                with lock:
                    events.write(line)
                    for skill_id in read_skills:
                        if skill_id not in read:
                            read.add(skill_id)
                            event = {"type": "skill.read", "skill_id": skill_id,
                                     "source": "command_output"}
                            events.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                    events.flush()

        def copy_stderr() -> None:
            for line in process.stderr:
                errors.write(line)
                errors.flush()
                decoded = line.decode("utf-8", errors="replace").strip()
                if not decoded.startswith(MARKER):
                    continue
                skill_id = decoded[len(MARKER):]
                if not SKILL_ID.fullmatch(skill_id) or skill_id in loaded:
                    continue
                if skill_id not in skill_docs:
                    continue
                with lock:
                    if skill_id not in loaded:
                        loaded.add(skill_id)
                        event = {"type": "skill.loaded", "skill_id": skill_id,
                                 "source": "codex_native"}
                        events.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                        events.flush()

        stdout_thread = threading.Thread(target=copy_stdout)
        stderr_thread = threading.Thread(target=copy_stderr)
        stdout_thread.start()
        stderr_thread.start()
        code = process.wait()
        stdout_thread.join()
        stderr_thread.join()
        process.stdout.close()
        process.stderr.close()
        events_path.with_name("loaded-skills.json").write_text(
            json.dumps(sorted(loaded), ensure_ascii=False), encoding="utf-8"
        )
        events_path.with_name("read-skills.json").write_text(
            json.dumps(sorted(read), ensure_ascii=False), encoding="utf-8"
        )
        return code


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
