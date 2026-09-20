import json
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.conversation_store import session_factory


class ConversationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prompts = []
        bundle = Path(__file__).resolve().parents[2] / "skill-catalog" / "public"
        patches = [
            patch.object(main, "DATA_DIR", self.root),
            patch.object(main, "HOST_DATA_DIR", self.root),
            patch.object(main, "TASK_DIR", self.root / "tasks"),
            patch.object(main, "RESULT_DIR", self.root / "results"),
            patch.object(main, "SKILL_BUNDLE_DIR", bundle),
            patch.object(main, "RUNNER_IMAGE", "runner:test"),
            patch.object(main.os, "chown"),
            patch.object(main, "_run_task", self.fake_run_task),
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
        self.repository = {"url": "https://example.com/repo.git", "ref": "main"}

    def fake_run_task(self, task_id):
        task = main._read_task(task_id)
        prompt = (self.root / "tasks" / task_id / "job" / "prompt.txt").read_text()
        self.prompts.append(prompt)
        result = self.root / "results" / task_id
        if "当前问题：\n失败" in prompt:
            task["status"] = "failed"
            task["error"] = "TestFailure"
        else:
            task["status"] = "succeeded"
            (result / "final-message.md").write_text(f"回答{len(self.prompts)}")
        task["finished_at"] = main._now()
        main._write_task(task)
        main._sync_turn_from_task(task)

    def create_conversation(self, headers=None):
        response = self.client.post(
            "/v1/conversations", json={}, headers=headers or self.alice
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_each_window_has_an_isolated_persistent_blank_workspace(self):
        first = self.create_conversation()
        second = self.create_conversation()
        first_path = main._workspace_path("alice", first)
        second_path = main._workspace_path("alice", second)
        self.assertTrue(first_path.is_dir())
        self.assertTrue(second_path.is_dir())
        self.assertNotEqual(first_path, second_path)
        (first_path / "hello.txt").write_text("第一轮文件", encoding="utf-8")

        turn = self.post_turn(first, "继续修改 hello.txt")
        self.assertEqual(turn.status_code, 202, turn.text)
        task = self.client.get(f"/v1/tasks/{turn.json()['task_id']}", headers=self.alice).json()
        self.assertEqual(task["execution_mode"], "conversation_workspace")
        self.assertTrue((first_path / "hello.txt").is_file())
        self.assertFalse((second_path / "hello.txt").exists())

        listing = self.client.get(f"/v1/conversations/{first}/files", headers=self.alice)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["items"][0]["path"], "hello.txt")
        content = self.client.get(
            f"/v1/conversations/{first}/files/content?path=hello.txt", headers=self.alice
        )
        self.assertEqual(content.content.decode(), "第一轮文件")
        self.assertEqual(
            self.client.get(f"/v1/conversations/{first}/files", headers=self.bob).status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/v1/conversations/{first}/files/content?path=../secret", headers=self.alice).status_code,
            400,
        )

        archive = self.client.get(f"/v1/conversations/{first}/workspace", headers=self.alice)
        self.assertEqual(archive.status_code, 200, archive.text)
        with tarfile.open(fileobj=io.BytesIO(archive.content), mode="r:gz") as bundle:
            self.assertIn("workspace/hello.txt", bundle.getnames())
            self.assertFalse(any(".git" in name.split("/") for name in bundle.getnames()))

    def test_delete_conversation_removes_workspace_and_history(self):
        conversation_id = self.create_conversation()
        workspace = main._workspace_path("alice", conversation_id)
        (workspace / "remove-me.txt").write_text("delete", encoding="utf-8")
        response = self.client.delete(f"/v1/conversations/{conversation_id}", headers=self.alice)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(workspace.exists())
        self.assertEqual(self.client.get(
            f"/v1/conversations/{conversation_id}", headers=self.alice
        ).status_code, 404)

    def post_turn(self, conversation_id, message, headers=None, request_id=None,
                  skill_ids=None, mcp_ids=None):
        body = {"message": message}
        if request_id:
            body["request_id"] = request_id
        if skill_ids is not None:
            body["skill_ids"] = skill_ids
        if mcp_ids is not None:
            body["mcp_ids"] = mcp_ids
        return self.client.post(
            f"/v1/conversations/{conversation_id}/turns", json=body, headers=headers or self.alice
        )

    def test_sixth_turn_uses_only_five_prior_rounds_and_keeps_all_history(self):
        conversation_id = self.create_conversation()
        for number in range(1, 8):
            response = self.post_turn(conversation_id, f"问题{number}")
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["context_rounds_used"], min(number - 1, 5))
        self.assertNotIn("问题1", self.prompts[6])
        self.assertIn("问题2", self.prompts[6])
        self.assertIn("问题6", self.prompts[6])
        self.assertIn("问题7", self.prompts[6])

        first_page = self.client.get(
            f"/v1/conversations/{conversation_id}/turns?limit=3", headers=self.alice
        ).json()
        self.assertEqual([turn["sequence"] for turn in first_page["items"]], [5, 6, 7])
        self.assertTrue(first_page["has_more"])
        next_page = self.client.get(
            f"/v1/conversations/{conversation_id}/turns?limit=3&before_seq={first_page['next_before_seq']}",
            headers=self.alice,
        ).json()
        self.assertEqual([turn["sequence"] for turn in next_page["items"]], [2, 3, 4])
        final_page = self.client.get(
            f"/v1/conversations/{conversation_id}/turns?before_seq={next_page['next_before_seq']}",
            headers=self.alice,
        ).json()
        self.assertEqual([turn["sequence"] for turn in final_page["items"]], [1])

    def test_window_and_user_isolation_and_task_ownership(self):
        alice_a = self.create_conversation()
        alice_b = self.create_conversation()
        bob_window = self.create_conversation(self.bob)
        first = self.post_turn(alice_a, "仅窗口A的事实")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.post_turn(alice_b, "窗口B问题").json()["context_rounds_used"], 0)
        self.assertEqual(self.post_turn(bob_window, "Bob问题", self.bob).json()["context_rounds_used"], 0)
        self.assertNotIn("仅窗口A的事实", self.prompts[-1])
        self.assertEqual(self.client.get("/v1/conversations", headers=self.bob).json()["items"][0]["id"], bob_window)
        for path in (
            f"/v1/conversations/{alice_a}",
            f"/v1/conversations/{alice_a}/turns",
            f"/v1/tasks/{first.json()['task_id']}",
            f"/v1/tasks/{first.json()['task_id']}/result",
            f"/v1/tasks/{first.json()['task_id']}/events",
        ):
            self.assertEqual(self.client.get(path, headers=self.bob).status_code, 404, path)
        self.assertEqual(self.client.get("/v1/conversations", headers={}).status_code, 401)

    def test_failed_turn_is_visible_but_not_in_context_and_retry_is_idempotent(self):
        conversation_id = self.create_conversation()
        failed = self.post_turn(conversation_id, "失败")
        self.assertEqual(failed.status_code, 202)
        self.assertEqual(self.post_turn(conversation_id, "恢复", request_id="request_0001").status_code, 202)
        replay = self.post_turn(conversation_id, "恢复", request_id="request_0001")
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["context_rounds_used"], 0)
        self.assertEqual(len(self.prompts), 2)
        history = self.client.get(f"/v1/conversations/{conversation_id}/turns", headers=self.alice).json()
        self.assertEqual([item["status"] for item in history["items"]], ["failed", "succeeded"])

    def test_memory_budget_discards_oldest_complete_round(self):
        conversation_id = self.create_conversation()
        self.assertEqual(self.post_turn(conversation_id, "第一轮").status_code, 202)
        self.assertEqual(self.post_turn(conversation_id, "第二轮").status_code, 202)
        with patch.object(main, "MAX_HISTORY_BYTES", 180):
            response = self.post_turn(conversation_id, "第三轮")
        self.assertEqual(response.status_code, 202)
        self.assertLess(response.json()["context_rounds_used"], 2)
        self.assertIn("第三轮", self.prompts[-1])

    def test_historical_skill_mentions_cannot_select_current_skill(self):
        conversation_id = self.create_conversation()
        self.assertEqual(self.post_turn(conversation_id, "请用 $awesome-api-design 分析").status_code, 202)
        self.assertIn("$awesome-api-design", self.prompts[-1])
        self.assertEqual(self.post_turn(conversation_id, "现在只总结上一轮").status_code, 202)
        self.assertIn("＄awesome-api-design", self.prompts[-1])
        self.assertNotIn("$awesome-api-design", self.prompts[-1])

    def test_same_window_rejects_overlapping_turns(self):
        conversation_id = self.create_conversation()
        with patch.object(main, "_run_task", lambda _task_id: None):
            first = self.post_turn(conversation_id, "仍在执行")
        self.assertEqual(first.status_code, 202)
        second = self.post_turn(conversation_id, "同时提交")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            self.client.get(f"/v1/conversations/{conversation_id}", headers=self.alice).json()["active_task_id"],
            first.json()["task_id"],
        )

    def test_turn_passes_selected_native_capabilities_only(self):
        self.client.post("/v1/skills/awesome-api-design/install", headers=self.alice)
        self.client.post("/v1/skills/awesome-code-review/install", headers=self.alice)
        self.client.post("/v1/mcp/context7/install", headers=self.alice)
        self.client.post("/v1/mcp/wikipedia/install", headers=self.alice)
        conversation_id = self.create_conversation()
        response = self.post_turn(conversation_id, "设计接口", skill_ids=["awesome-api-design"],
                                  mcp_ids=["context7"])
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        task = self.client.get(f"/v1/tasks/{task_id}", headers=self.alice).json()
        self.assertEqual(task["skills"], ["awesome-api-design"])
        self.assertEqual(task["mcps"], ["context7"])
        self.assertTrue((self.root / "tasks" / task_id / "skills" /
                         "awesome-api-design" / "SKILL.md").is_file())
        self.assertFalse((self.root / "tasks" / task_id / "skills" / "awesome-code-review").exists())
        config = (self.root / "tasks" / task_id / "codex-config.toml").read_text()
        self.assertIn('[mcp_servers."context7"]', config)
        self.assertNotIn('[mcp_servers."wikipedia"]', config)
        self.assertTrue(self.prompts[-1].startswith("$awesome-api-design\n\n"))

    def test_turn_automatically_routes_installed_search_mcp(self):
        self.client.post("/v1/mcp/exa-search/install", headers=self.alice)
        conversation_id = self.create_conversation()
        response = self.post_turn(conversation_id, "查询最近的 Agent 智能体相关新闻")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["mcps"], ["exa-search"])
        self.assertEqual(response.json()["mcp_selection"], "auto")
        task_id = response.json()["task_id"]
        config = (self.root / "tasks" / task_id / "codex-config.toml").read_text()
        self.assertIn('[mcp_servers."exa-search"]', config)


if __name__ == "__main__":
    unittest.main()
