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


if __name__ == "__main__":
    unittest.main()
