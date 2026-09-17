import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main


class FakeContainer:
    status = "exited"

    def __init__(self):
        self.removed = False

    def reload(self):
        pass

    def logs(self, **kwargs):
        return b"runner test log"

    def remove(self, force=False):
        self.removed = force


class FakeDocker:
    def __init__(self, container):
        self.container = container
        self.options = None
        self.containers = self

    def run(self, image, **options):
        self.options = options
        self.image = image
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
            patch.object(main, "RUNNER_IMAGE", "runner:test"),
            patch.object(main, "MAX_ACTIVE_TASKS", 1),
            patch.object(main.docker, "from_env", return_value=self.docker),
            patch.object(main.os, "chown"),
            patch.dict(main.os.environ, {"MODEL_API_KEY": "test-key"}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)

    def test_task_starts_runner_and_removes_it(self):
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
        self.assertTrue(self.container.removed)
        self.assertEqual(self.client.get(f"/v1/tasks/{task_id}").json()["status"], "failed")
        self.assertEqual(self.client.get("/v1/capacity").json()["running_tasks"], 0)

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

        task_file.write_text(json.dumps({"task_id": task_id, "status": "succeeded"}))
        (result_dir / "exit-code.txt").write_text("0")
        (result_dir / "final-message.md").write_text("Done")
        (result_dir / "git-status.txt").write_text(" M README.md\n")
        (result_dir / "changes.diff").write_text("+MVP_OK\n")
        (result_dir / "codex-events.jsonl").write_text('{"type":"turn.completed"}\n')
        response = self.client.get(f"/v1/tasks/{task_id}/result")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["final_message"], "Done")
        self.assertEqual(result["diff"], "+MVP_OK\n")
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
