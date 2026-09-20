import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, mcp_store
from app.conversation_store import session_factory


class McpMarketplaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patches = [
            patch.object(main, "DATA_DIR", self.root),
            patch.object(main, "HOST_DATA_DIR", self.root),
            patch.object(main, "TASK_DIR", self.root / "tasks"),
            patch.object(main, "RESULT_DIR", self.root / "results"),
            patch.object(main, "RUNNER_IMAGE", "runner:test"),
            patch.object(main.os, "chown"),
            patch.object(main, "_run_task", lambda _task_id: None),
            patch.dict(main.os.environ, {
                "MODEL_API_KEY": "test-model-key",
                "DATABASE_URL": f"sqlite:///{self.root / 'conversations.sqlite3'}",
                "USER_API_KEYS_JSON": json.dumps({"alice": "alice-secret", "bob": "bob-secret"}),
            }),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        session_factory.cache_clear()
        self.addCleanup(session_factory.cache_clear)
        self.client = TestClient(main.app)
        self.alice = {"Authorization": "Bearer alice-secret"}
        self.bob = {"Authorization": "Bearer bob-secret"}

    def test_catalog_contains_curated_remote_servers_and_installations_are_isolated(self):
        catalog = self.client.get("/v1/mcp/catalog", headers=self.alice)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        items = catalog.json()["items"]
        self.assertEqual(len(items), 7)
        self.assertTrue(all(item["transport"] == "streamable-http" for item in items))
        self.assertTrue(all(not item["installed"] for item in items))
        exa = next(item for item in items if item["id"] == "exa-search")
        self.assertEqual(exa["endpoint"], "https://mcp.exa.ai/mcp")
        self.assertEqual(exa["tools"], ["web_search_exa", "web_fetch_exa"])
        self.assertEqual(exa["category"], "联网搜索")
        self.assertEqual(self.client.post("/v1/mcp/context7/install", headers=self.alice).status_code, 200)
        self.assertEqual(self.client.post("/v1/mcp/context7/install", headers=self.alice).status_code, 200)
        self.assertEqual([item["id"] for item in self.client.get("/v1/mcp", headers=self.alice).json()["items"]],
                         ["context7"])
        self.assertEqual(self.client.get("/v1/mcp", headers=self.bob).json()["items"], [])
        self.assertFalse(next(item for item in self.client.get("/v1/mcp/catalog", headers=self.bob).json()["items"]
                              if item["id"] == "context7")["installed"])
        self.assertEqual(self.client.delete("/v1/mcp/context7", headers=self.bob).status_code, 404)
        self.assertEqual(self.client.delete("/v1/mcp/context7", headers=self.alice).status_code, 200)

    def test_task_uses_native_codex_config_without_modifying_prompt(self):
        self.client.post("/v1/mcp/context7/install", headers=self.alice)
        self.client.post("/v1/mcp/wikipedia/install", headers=self.alice)
        prompt = "查询 FastAPI 的最新依赖注入文档"
        response = self.client.post("/v1/tasks", headers=self.alice, json={
            "repository": {"url": "https://example.com/repo.git"}, "prompt": prompt,
            "mcp_ids": ["context7"],
        })
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        job = self.root / "tasks" / task_id / "job"
        self.assertEqual((job / "prompt.txt").read_text(), prompt)
        config = (self.root / "tasks" / task_id / "codex-config.toml").read_text()
        self.assertIn('[mcp_servers."context7"]', config)
        self.assertNotIn('[mcp_servers."wikipedia"]', config)
        self.assertIn('enabled_tools = ["resolve-library-id", "query-docs"]', config)
        self.assertNotIn("FastAPI", config)
        task = self.client.get(f"/v1/tasks/{task_id}", headers=self.alice).json()
        self.assertEqual(task["mcps"], ["context7"])
        with patch.object(main, "MAX_ACTIVE_TASKS", 2):
            rejected = self.client.post("/v1/tasks", headers=self.bob, json={
                "repository": {"url": "https://example.com/repo.git"}, "prompt": prompt,
                "mcp_ids": ["context7"],
            })
        self.assertEqual(rejected.status_code, 409)

    def test_unknown_mcp_and_config_renderer(self):
        self.assertEqual(self.client.post("/v1/mcp/unknown/install", headers=self.alice).status_code, 404)
        rendered = mcp_store.render_codex_config('model = "test"\n', [mcp_store.BY_ID["arxiv"]])
        self.assertIn('url = "https://arxiv.caseyjhand.com/mcp"', rendered)
        self.assertNotIn("MCP instructions", rendered)
        exa = mcp_store.render_codex_config('model = "test"\n', [mcp_store.BY_ID["exa-search"]])
        self.assertIn('[mcp_servers."exa-search"]', exa)
        self.assertIn('url = "https://mcp.exa.ai/mcp"', exa)
        self.assertIn('enabled_tools = ["web_search_exa", "web_fetch_exa"]', exa)


if __name__ == "__main__":
    unittest.main()
