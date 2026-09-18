#!/usr/bin/env python3
"""Merge Codex JSONL and verified Skill-load signals into one event stream."""

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


def run(command: list[str], events_path: Path = EVENTS, stderr_path: Path = STDERR,
        skills_root: Path = SKILLS) -> int:
    lock = threading.Lock()
    loaded = set()
    with events_path.open("wb") as events, stderr_path.open("wb") as errors:
        process = subprocess.Popen(command, stdin=sys.stdin.buffer, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)

        def copy_stdout() -> None:
            for line in process.stdout:
                with lock:
                    events.write(line)
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
                if not (skills_root / skill_id / "SKILL.md").is_file():
                    continue
                loaded.add(skill_id)
                event = {"type": "skill.loaded", "skill_id": skill_id}
                with lock:
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
        return code


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
