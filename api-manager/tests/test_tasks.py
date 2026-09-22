import asyncio
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.conversation_store import session_factory


class FakeContainer:
    def __init__(self, status="exited", exit_after_reloads=None):
        self.status = status
        self.exit_after_reloads = exit_after_reloads
        self.reloads = 0
        self.removed = False
        self.stopped = False
        self.killed = False

    def reload(self):
        self.reloads += 1
        if self.exit_after_reloads is not None and self.reloads >= self.exit_after_reloads:
            self.status = "exited"

    def stop(self, timeout=10):
        self.stopped = True
        self.status = "exited"

    def kill(self):
        self.killed = True
        self.status = "exited"

    def logs(self, **kwargs):
        return b"runner test log"

    def remove(self, force=False):
        self.removed = force


class FakeDocker:
    def __init__(self, container):
        self.container = container
        self.options = None
        self.containers = self
        self.on_run = None

    def run(self, image, **options):
        self.options = options
        self.image = image
        if self.on_run:
            self.on_run()
        return self.container

    def get(self, name):
        return self.container


class TaskLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.container = FakeContainer()
        self.docker = FakeDocker(self.container)
        patches = [
            patch.object(main, "DATA_DIR", self.root),
            patch.object(main, "HOST_DATA_DIR", self.root),
            patch.object(main, "TASK_DIR", self.root / "tasks"),
            patch.object(main, "RESULT_DIR", self.root / "results"),
            patch.object(main, "REPO_CACHE_DIR", self.root / "repo-cache"),
            patch.object(main, "RUNNER_IMAGE", "runner:test"),
            patch.object(main, "MAX_ACTIVE_TASKS", 1),
            patch.object(main.docker, "from_env", return_value=self.docker),
            patch.object(main.os, "chown"),
            patch.dict(main.os.environ, {
                "MODEL_API_KEY": "test-key",
                "DATABASE_URL": f"sqlite:///{self.root / 'conversations.sqlite3'}",
            }),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        session_factory.cache_clear()
        self.addCleanup(session_factory.cache_clear)
        self.client = TestClient(main.app)

    def test_task_starts_runner_and_removes_it(self):
        with patch.object(
            main,
            "_prepare_repository",
            return_value=(self.root / "repo-cache" / "mirror.git", True, False, "a" * 40, 0.002),
        ):
            response = self.client.post(
                "/v1/tasks",
                json={
                    "repository": {"url": "https://example.com/repo.git", "ref": "main"},
                    "prompt": "Review the README",
                },
            )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        self.assertEqual(self.docker.image, "runner:test")
        self.assertEqual(
            self.docker.options["volumes"][str(self.root / "tasks" / task_id / "job")]["bind"],
            "/job",
        )
        self.assertEqual(self.docker.options["environment"]["MODEL_API_KEY"], "test-key")
        self.assertEqual(
            self.docker.options["volumes"][str(self.root / "tasks" / task_id / "skills")],
            {"bind": "/home/codex/.agents/skills", "mode": "ro"},
        )
        self.assertEqual(
            self.docker.options["volumes"][str(self.root / "tasks" / task_id / "codex-config.toml")],
            {"bind": "/home/codex/.codex/config.toml", "mode": "ro"},
        )
        self.assertEqual(
            self.docker.options["volumes"][str(self.root / "repo-cache" / "mirror.git")]["mode"],
            "ro",
        )
        self.assertTrue(self.container.removed)
        self.assertEqual(self.client.get(f"/v1/tasks/{task_id}").json()["status"], "failed")
        self.assertTrue(self.client.get(f"/v1/tasks/{task_id}").json()["cache_hit"])
        self.assertEqual(self.client.get("/v1/capacity").json()["running_tasks"], 0)

    def test_conversation_task_mounts_persistent_workspace_read_write(self):
        conversation_id = "c" * 32
        workspace = main._create_workspace("local-dev", conversation_id)
        task = main._create_task_files(
            None, "Create a file", "local-dev", [], [], conversation_id, "d" * 32,
            "conversation_workspace", 1,
        )
        main._run_task(task["task_id"])
        host_workspace = main._host_workspace_path("local-dev", conversation_id)
        self.assertEqual(
            self.docker.options["volumes"][str(host_workspace)],
            {"bind": "/workspace", "mode": "rw"},
        )
        self.assertNotIn("/cache/repository.git", {
            item["bind"] for item in self.docker.options["volumes"].values()
        })
        self.assertTrue(workspace.is_dir())

    def test_conversation_task_reports_created_modified_and_deleted_files(self):
        conversation_id = "6" * 32
        workspace = main._create_workspace("local-dev", conversation_id)
        (workspace / "same.txt").write_text("same", encoding="utf-8")
        (workspace / "modify.md").write_text("old", encoding="utf-8")
        (workspace / "delete.txt").write_text("remove", encoding="utf-8")
        (workspace / ".git").mkdir()
        (workspace / ".git" / "ignored").write_text("old", encoding="utf-8")
        task = main._create_task_files(
            None, "Update files", "local-dev", [], [], conversation_id, None,
            "conversation_workspace", 1,
        )
        (self.root / "results" / task["task_id"] / "exit-code.txt").write_text("0")

        def mutate_workspace():
            (workspace / "modify.md").write_text("new", encoding="utf-8")
            (workspace / "delete.txt").unlink()
            (workspace / "report.json").write_text('{"ok": true}', encoding="utf-8")
            (workspace / "pixel.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (workspace / ".git" / "ignored").write_text("new", encoding="utf-8")

        self.docker.on_run = mutate_workspace
        main._run_task(task["task_id"])
        result = self.client.get(f"/v1/tasks/{task['task_id']}/result")
        self.assertEqual(result.status_code, 200, result.text)
        changes = result.json()["workspace_changes"]
        self.assertEqual([item["path"] for item in changes["created"]], ["pixel.png", "report.json"])
        self.assertEqual([item["path"] for item in changes["modified"]], ["modify.md"])
        self.assertEqual([item["path"] for item in changes["deleted"]], ["delete.txt"])
        self.assertEqual(changes["created"][0]["preview_type"], "image")
        self.assertEqual(changes["created"][1]["preview_type"], "json")
        self.assertTrue(changes["created"][1]["preview_url"].endswith("path=report.json"))
        self.assertIn(f"/v1/tasks/{task['task_id']}/files/content", changes["created"][1]["preview_url"])
        self.assertTrue(changes["created"][1]["download_url"].endswith("&download=true"))
        self.assertEqual(
            changes["download_all_url"], f"/v1/tasks/{task['task_id']}/files/archive"
        )
        archive = self.client.get(changes["download_all_url"])
        self.assertEqual(archive.status_code, 200)
        with tarfile.open(fileobj=io.BytesIO(archive.content), mode="r:gz") as bundle:
            self.assertEqual(bundle.getnames(), ["files/pixel.png", "files/report.json", "files/modify.md"])
            self.assertEqual(bundle.extractfile("files/modify.md").read(), b"new")
        old_preview = changes["modified"][0]["preview_url"]
        self.assertEqual(self.client.get(old_preview).text, "new")
        old_download = self.client.get(changes["modified"][0]["download_url"])
        self.assertIn("attachment", old_download.headers["content-disposition"])
        self.assertEqual(old_download.content, b"new")
        self.assertEqual(
            self.client.get(f"/v1/tasks/{task['task_id']}/files/content?path=delete.txt").status_code,
            404,
        )

        second = main._create_task_files(
            None, "Update files again", "local-dev", [], [], conversation_id, None,
            "conversation_workspace", 2,
        )
        (self.root / "results" / second["task_id"] / "exit-code.txt").write_text("0")
        self.docker.on_run = lambda: (
            (workspace / "modify.md").write_text("newer", encoding="utf-8"),
            (workspace / "report.json").unlink(),
        )
        main._run_task(second["task_id"])
        second_changes = self.client.get(f"/v1/tasks/{second['task_id']}/result").json()["workspace_changes"]
        self.assertEqual([item["path"] for item in second_changes["modified"]], ["modify.md"])
        self.assertEqual([item["path"] for item in second_changes["deleted"]], ["report.json"])
        self.assertEqual(self.client.get(old_preview).text, "new")
        self.assertEqual(self.client.get(second_changes["modified"][0]["preview_url"]).text, "newer")
        self.assertEqual(self.client.get(changes["created"][1]["preview_url"]).json(), {"ok": True})
        with tarfile.open(fileobj=io.BytesIO(self.client.get(second_changes["download_all_url"]).content), mode="r:gz") as bundle:
            self.assertEqual(bundle.getnames(), ["files/modify.md"])

    def test_first_phase_preview_types(self):
        expected = {
            "README.md": "markdown",
            "notes.txt": "text",
            "main.py": "code",
            "data.json": "json",
            "photo.jpg": "image",
            "report.pdf": "pdf",
            "archive.zip": None,
        }
        for path, preview_type in expected.items():
            with self.subTest(path=path):
                metadata = main._workspace_file_metadata(path, {"size_bytes": 12})
                self.assertEqual(metadata["preview_type"], preview_type)
                self.assertEqual(metadata["previewable"], preview_type is not None)

    def test_zero_task_timeout_waits_for_runner_to_finish(self):
        self.container.status = "running"
        self.container.exit_after_reloads = 3
        conversation_id = "8" * 32
        main._create_workspace("local-dev", conversation_id)
        task = main._create_task_files(
            None, "Take as long as needed", "local-dev", [], [], conversation_id, None,
            "conversation_workspace", 1,
        )
        (self.root / "results" / task["task_id"] / "exit-code.txt").write_text("0")
        with patch.object(main, "TASK_TIMEOUT_SECONDS", 0), patch.object(main.time, "sleep"):
            main._run_task(task["task_id"])
        finished = main._read_task(task["task_id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(self.container.reloads, 3)
        self.assertFalse(self.container.stopped)
        self.assertFalse(self.container.killed)

    def test_cancel_endpoint_requests_runner_stop(self):
        task_id = "9" * 32
        task_dir = self.root / "tasks" / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({
            "task_id": task_id, "status": "running", "user_id": "local-dev",
        }))
        self.container.status = "running"
        response = self.client.post(f"/v1/tasks/{task_id}/cancel")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "cancelling")
        cancelling = main._read_task(task_id)
        self.assertEqual(cancelling["status"], "cancelling")
        self.assertIn("cancel_requested_at", cancelling)
        self.assertTrue(self.container.stopped)

    def test_worker_finishes_cancelled_task_without_starting_container(self):
        conversation_id = "7" * 32
        main._create_workspace("local-dev", conversation_id)
        task = main._create_task_files(
            None, "Stop this", "local-dev", [], [], conversation_id, None,
            "conversation_workspace", 1,
        )
        task["status"] = "cancelling"
        task["cancel_requested_at"] = main._now()
        main._write_task(task)
        main._run_task(task["task_id"])
        self.assertEqual(main._read_task(task["task_id"])["status"], "cancelled")
        self.assertIsNone(self.docker.options)

    def test_repository_cache_cold_warm_and_explicit_refresh(self):
        source = self.root / "source"
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / "README.md").write_text("first\n")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.com",
             "commit", "-qm", "first"],
            check=True,
        )
        subprocess.run(["git", "-C", str(source), "branch", "-M", "main"], check=True)

        def prepare(task_id, refresh=False):
            job = self.root / "tasks" / task_id / "job"
            job.mkdir(parents=True)
            (job / "repository-url.txt").write_text(str(source))
            (job / "repository-ref.txt").write_text("main")
            (job / "repository-refresh.txt").write_text("1" if refresh else "0")
            return main._prepare_repository(task_id)

        cache_path, cold_hit, cold_refresh, old_commit, _ = prepare("1" * 32)
        self.assertFalse(cold_hit)
        self.assertFalse(cold_refresh)
        self.assertTrue((self.root / "repo-cache" / cache_path.name).is_dir())
        (source / "README.md").write_text("second\n")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.com",
             "commit", "-qm", "second"],
            check=True,
        )
        _, warm_hit, warm_refresh, cached_commit, _ = prepare("2" * 32)
        self.assertTrue(warm_hit)
        self.assertFalse(warm_refresh)
        self.assertEqual(cached_commit, old_commit)
        _, refresh_hit, refreshed, new_commit, _ = prepare("3" * 32, refresh=True)
        self.assertTrue(refresh_hit)
        self.assertTrue(refreshed)
        self.assertNotEqual(new_commit, old_commit)
        shutil.rmtree(source)
        _, offline_hit, _, offline_commit, _ = prepare("4" * 32)
        self.assertTrue(offline_hit)
        self.assertEqual(offline_commit, new_commit)

    def test_capacity_and_repository_validation(self):
        self.assertEqual(
            self.client.post(
                "/v1/tasks",
                json={"repository": {"url": "file:///etc/passwd"}, "prompt": "test"},
            ).status_code,
            422,
        )
        occupied = self.root / "tasks" / ("a" * 32)
        occupied.mkdir(parents=True)
        (occupied / "task.json").write_text(json.dumps({"status": "running"}))
        response = self.client.post(
            "/v1/tasks",
            json={"repository": {"url": "https://example.com/repo.git"}, "prompt": "test"},
        )
        self.assertEqual(response.status_code, 429)

    def test_sse_replays_events_and_resumes_after_last_event_id(self):
        task_id = "b" * 32
        task_dir = self.root / "tasks" / task_id
        result_dir = self.root / "results" / task_id
        task_dir.mkdir(parents=True)
        result_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(
            json.dumps({"task_id": task_id, "status": "succeeded"})
        )
        (result_dir / "codex-events.jsonl").write_text(
            '{"type":"thread.started"}\n{"type":"turn.completed"}\n'
        )

        response = self.client.get(f"/v1/tasks/{task_id}/events")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("id: 1\nevent: codex.event", response.text)
        self.assertIn("id: 2\nevent: codex.event", response.text)
        self.assertIn("id: 3\nevent: task.completed", response.text)

        resumed = self.client.get(
            f"/v1/tasks/{task_id}/events", headers={"Last-Event-ID": "1"}
        )
        self.assertNotIn("id: 1\n", resumed.text)
        self.assertIn("id: 2\nevent: codex.event", resumed.text)
        self.assertIn("id: 3\nevent: task.completed", resumed.text)
        invalid = self.client.get(
            f"/v1/tasks/{task_id}/events", headers={"Last-Event-ID": "abc"}
        )
        self.assertEqual(invalid.status_code, 400)

    def test_sse_reports_cancelled_terminal_status(self):
        task_id = "6" * 32
        task_dir = self.root / "tasks" / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({
            "task_id": task_id, "status": "cancelled", "user_id": "local-dev",
        }))
        response = self.client.get(f"/v1/tasks/{task_id}/events")
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: task.cancelled", response.text)
        self.assertIn('\"status\":\"cancelled\"', response.text)

    def test_sse_follows_new_lines_without_emitting_partial_json(self):
        task_id = "c" * 32
        task_dir = self.root / "tasks" / task_id
        result_dir = self.root / "results" / task_id
        task_dir.mkdir(parents=True)
        result_dir.mkdir(parents=True)
        task_file = task_dir / "task.json"
        task_file.write_text(json.dumps({"task_id": task_id, "status": "running"}))
        events_file = result_dir / "codex-events.jsonl"
        events_file.write_bytes(b'{"type":"thread.started"}\n')

        async def consume():
            stream = main._stream_task_events(task_id, 0)
            first = await asyncio.wait_for(anext(stream), 1)
            with events_file.open("ab") as output:
                output.write(b'{"type":"turn.')
            second_pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.03)
            self.assertFalse(second_pending.done())
            with events_file.open("ab") as output:
                output.write(b'completed"}\n')
            task_file.write_text(json.dumps({"task_id": task_id, "status": "succeeded"}))
            second = await asyncio.wait_for(second_pending, 1)
            terminal = await asyncio.wait_for(anext(stream), 1)
            return first, second, terminal

        with patch.object(main, "SSE_POLL_SECONDS", 0.005):
            first, second, terminal = asyncio.run(consume())
        self.assertIn("id: 1\nevent: codex.event", first)
        self.assertIn("id: 2\nevent: codex.event", second)
        self.assertIn("id: 3\nevent: task.completed", terminal)

    def test_result_waits_for_completion_then_returns_outputs(self):
        task_id = "e" * 32
        task_dir = self.root / "tasks" / task_id
        result_dir = self.root / "results" / task_id
        task_dir.mkdir(parents=True)
        result_dir.mkdir(parents=True)
        task_file = task_dir / "task.json"
        task_file.write_text(json.dumps({"task_id": task_id, "status": "running"}))
        pending = self.client.get(f"/v1/tasks/{task_id}/result")
        self.assertEqual(pending.status_code, 202)
        self.assertEqual(pending.headers["retry-after"], "2")
        self.assertEqual(
            self.client.get(f"/v1/tasks/{task_id}/artifacts/changes.diff").status_code,
            409,
        )

        task_file.write_text(json.dumps({"task_id": task_id, "status": "succeeded",
                                         "skills": ["awesome-api-design", "awesome-bug-fix"]}))
        (result_dir / "exit-code.txt").write_text("0")
        (result_dir / "final-message.md").write_text("Done")
        (result_dir / "git-status.txt").write_text(" M README.md\n")
        (result_dir / "changes.diff").write_text("+MVP_OK\n")
        (result_dir / "codex-events.jsonl").write_text('{"type":"turn.completed"}\n')
        (result_dir / "loaded-skills.json").write_text('["awesome-api-design", "not-installed"]')
        (result_dir / "clone-milliseconds.txt").write_text("120")
        (result_dir / "codex-milliseconds.txt").write_text("5432")
        response = self.client.get(f"/v1/tasks/{task_id}/result")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["final_message"], "Done")
        self.assertEqual(result["loaded_skills"], ["awesome-api-design"])
        self.assertEqual(result["diff"], "+MVP_OK\n")
        self.assertEqual(result["timings_ms"]["local_clone"], 120)
        self.assertEqual(result["timings_ms"]["codex"], 5432)
        self.assertEqual(len(result["artifacts"]), 2)
        self.assertEqual(
            self.client.get(f"/v1/tasks/{task_id}/artifacts/changes.diff").text,
            "+MVP_OK\n",
        )
        self.assertEqual(
            self.client.get(f"/v1/tasks/{task_id}/artifacts/secret").status_code,
            404,
        )

    def test_failed_task_without_outputs_has_readable_result(self):
        task_id = "f" * 32
        task_dir = self.root / "tasks" / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(
            json.dumps({"task_id": task_id, "status": "failed", "error": "DockerException"})
        )
        response = self.client.get(f"/v1/tasks/{task_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error"], "DockerException")
        self.assertIsNone(response.json()["exit_code"])
        self.assertEqual(response.json()["artifacts"], [])
        self.assertEqual(self.client.get("/v1/tasks/bad/result").status_code, 404)


if __name__ == "__main__":
    unittest.main()
