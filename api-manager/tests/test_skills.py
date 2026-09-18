import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, skill_store


class SkillMarketplaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bundle = Path(__file__).resolve().parents[2] / "skill-catalog" / "public"
        patches = [
            patch.object(main, "DATA_DIR", self.root),
            patch.object(main, "HOST_DATA_DIR", self.root),
            patch.object(main, "TASK_DIR", self.root / "tasks"),
            patch.object(main, "RESULT_DIR", self.root / "results"),
            patch.object(main, "SKILL_BUNDLE_DIR", bundle),
            patch.object(main, "RUNNER_IMAGE", "runner:test"),
            patch.object(main.os, "chown"),
            patch.object(main, "_run_task", lambda _task_id: None),
            patch.dict(main.os.environ, {
                "MODEL_API_KEY": "test-model-key",
                "USER_API_KEYS_JSON": json.dumps({"alice": "alice-secret", "bob": "bob-secret"}),
            }),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.alice = {"Authorization": "Bearer alice-secret"}
        self.bob = {"Authorization": "Bearer bob-secret"}

    def test_public_catalog_install_isolation_and_uninstall(self):
        path = "/v1/skills/catalog"
        catalog = self.client.get(path, headers=self.alice)
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(len(catalog.json()["items"]), 5)
        self.assertFalse(any(item["installed"] for item in catalog.json()["items"]))
        public = self.root / "skills" / "public" / "awesome-api-design" / "SKILL.md"
        self.assertTrue(public.is_file())
        self.assertEqual(self.client.get(path).status_code, 401)

        install = self.client.post("/v1/skills/awesome-api-design/install", headers=self.alice)
        self.assertEqual(install.status_code, 200, install.text)
        self.assertEqual(self.client.post("/v1/skills/awesome-api-design/install", headers=self.alice).status_code, 200)
        alice_dir = skill_store.user_dir(self.root, "alice")
        bob_dir = skill_store.user_dir(self.root, "bob")
        self.assertTrue((alice_dir / "awesome-api-design" / "SKILL.md").is_file())
        self.assertTrue((alice_dir / "awesome-api-design" / "LICENSE").is_file())
        self.assertFalse(bob_dir.exists())
        self.assertEqual([item["id"] for item in self.client.get("/v1/skills", headers=self.alice).json()["items"]],
                         ["awesome-api-design"])
        self.assertEqual(self.client.get("/v1/skills", headers=self.bob).json()["items"], [])
        self.assertFalse(next(item for item in self.client.get(path, headers=self.bob).json()["items"]
                              if item["id"] == "awesome-api-design")["installed"])
        self.assertEqual(self.client.delete("/v1/skills/awesome-api-design", headers=self.bob).status_code, 404)
        self.assertEqual(self.client.delete("/v1/skills/awesome-api-design", headers=self.alice).status_code, 200)
        self.assertFalse((alice_dir / "awesome-api-design").exists())
        self.assertTrue(public.is_file())

    def test_task_snapshot_stays_after_uninstall_and_rejects_unknown_skill(self):
        self.assertEqual(self.client.post("/v1/skills/unknown/install", headers=self.alice).status_code, 404)
        self.assertEqual(self.client.post("/v1/skills/awesome-code-review/install", headers=self.alice).status_code, 200)
        task = self.client.post("/v1/tasks", headers=self.alice, json={
            "repository": {"url": "https://example.com/repo.git"}, "prompt": "$awesome-code-review review",
        })
        self.assertEqual(task.status_code, 202, task.text)
        task_id = task.json()["task_id"]
        snapshot = self.root / "tasks" / task_id / "skills" / "awesome-code-review"
        self.assertTrue((snapshot / "SKILL.md").is_file())
        self.assertTrue((snapshot / "references" / "smell-baseline.md").is_file())
        self.assertEqual(self.client.get(f"/v1/tasks/{task_id}", headers=self.alice).json()["skills"],
                         ["awesome-code-review"])
        self.client.delete("/v1/skills/awesome-code-review", headers=self.alice)
        self.assertTrue((snapshot / "SKILL.md").is_file())
        self.assertFalse((skill_store.user_dir(self.root, "alice") / "awesome-code-review").exists())
        with patch.object(main, "MAX_ACTIVE_TASKS", 2):
            bob_task = self.client.post("/v1/tasks", headers=self.bob, json={
                "repository": {"url": "https://example.com/repo.git"}, "prompt": "Review",
            })
        self.assertEqual(bob_task.status_code, 202)
        self.assertEqual(self.client.get(f"/v1/tasks/{bob_task.json()['task_id']}", headers=self.bob).json()["skills"], [])

    def test_administrator_removal_from_public_is_not_reseeded(self):
        self.client.get("/v1/skills/catalog", headers=self.alice)
        public = self.root / "skills" / "public" / "awesome-api-design"
        shutil.rmtree(public)
        catalog = self.client.get("/v1/skills/catalog", headers=self.alice).json()["items"]
        self.assertNotIn("awesome-api-design", {item["id"] for item in catalog})


if __name__ == "__main__":
    unittest.main()
