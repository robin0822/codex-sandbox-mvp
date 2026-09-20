import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

import server


class FakeApi(BaseHTTPRequestHandler):
    received = None
    last_event_id = None
    authorization = None

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/health/ready":
            self._json(200, {"status": "ready"})
        elif self.path == "/v1/me":
            if self.headers.get("Authorization") == "Bearer alice-key":
                self._json(200, {"user_id": "alice"})
            else:
                self._json(401, {"detail": "API key required"})
        elif self.path.startswith("/v1/conversations"):
            self._json(200, {"items": [], "has_more": False})
        elif self.path == "/v1/skills/catalog":
            self._json(200, {"items": [{"id": "awesome-api-design", "installed": False}]})
        elif self.path == "/v1/mcp/catalog":
            self._json(200, {"items": [{"id": "context7", "installed": False}]})
        elif self.path == "/v1/tasks/abc/events":
            type(self).last_event_id = self.headers.get("Last-Event-ID")
            type(self).authorization = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'id: 2\nevent: codex.event\ndata: {"data":{"type":"turn.started"}}\n\n')
            self.wfile.flush()
            self.wfile.write(b'id: 3\nevent: task.completed\ndata: {"status":"succeeded"}\n\n')
            self.wfile.flush()
        else:
            self.send_error(404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).received = json.loads(body) if body else None
        type(self).authorization = self.headers.get("Authorization")
        self._json(201 if self.path == "/v1/conversations" else 202, {"task_id": "abc"})

    def do_DELETE(self):
        type(self).authorization = self.headers.get("Authorization")
        self._json(200, {"id": "awesome-api-design", "installed": False})

    def _json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class LocalPreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        cls.preview = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        server.UPSTREAM = f"http://127.0.0.1:{cls.api.server_port}"
        cls.threads = [
            threading.Thread(target=instance.serve_forever, daemon=True)
            for instance in (cls.api, cls.preview)
        ]
        for thread in cls.threads:
            thread.start()
        cls.base = f"http://127.0.0.1:{cls.preview.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.preview.shutdown()
        cls.api.shutdown()
        cls.preview.server_close()
        cls.api.server_close()

    def test_static_page_and_api_status(self):
        with urlopen(self.base + "/") as response:
            page = response.read().decode()
            self.assertIn("Codex 工作台", page)
            self.assertIn("/vendor/markdown-it/markdown-it.umd.min.js", page)
            self.assertIn("/vendor/dompurify/purify.min.js", page)
        with urlopen(self.base + "/vendor/markdown-it/markdown-it.umd.min.js") as response:
            self.assertEqual(response.status, 200)
            self.assertGreater(len(response.read()), 1000)
        with urlopen(self.base + "/vendor/dompurify/purify.min.js") as response:
            self.assertEqual(response.status, 200)
            self.assertGreater(len(response.read()), 1000)
        with urlopen(self.base + "/api/status") as response:
            self.assertTrue(json.load(response)["ready"])

    def test_submit_and_stream_events_with_resume_header(self):
        payload = {
            "repository": {"url": "https://github.com/octocat/Hello-World.git", "ref": "master"},
            "prompt": "检查 README",
        }
        request = Request(
            self.base + "/v1/tasks",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(json.load(response)["task_id"], "abc")
        self.assertEqual(FakeApi.received, payload)

        request = Request(self.base + "/v1/tasks/abc/events", headers={"Last-Event-ID": "1"})
        with urlopen(request) as response:
            self.assertEqual(response.headers["Content-Type"], "text/event-stream")
            events = response.read().decode()
        self.assertEqual(FakeApi.last_event_id, "1")
        self.assertIn("event: codex.event", events)
        self.assertIn("event: task.completed", events)

    def test_cancel_task_is_proxied_to_api_manager(self):
        task_id = "a" * 32
        request = Request(self.base + f"/v1/tasks/{task_id}/cancel", data=b"", method="POST")
        with urlopen(request) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(json.load(response)["task_id"], "abc")

    def test_ssh_bridge_streams_text_lines_as_sse_bytes(self):
        response = io.StringIO('event: codex.event\ndata: {"data":{"type":"turn.started"}}\n\n')
        stderr = Mock()
        stderr.channel.exit_status_ready.return_value = False
        client = Mock()
        with patch.object(server, "SSH_HOST", "example.invalid"), patch.object(
            server,
            "ssh_request",
            return_value=(
                {"status": 200, "content_type": "text/event-stream"},
                response,
                stderr,
                client,
            ),
        ):
            with urlopen(self.base + "/v1/tasks/abc/events") as streamed:
                self.assertEqual(streamed.headers["Content-Type"], "text/event-stream")
                self.assertIn("event: codex.event", streamed.read().decode())
        client.close.assert_called_once()

    def test_login_cookie_proxies_conversations_with_server_side_identity(self):
        login = Request(
            self.base + "/api/login",
            data=json.dumps({"api_key": "alice-key"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(login) as response:
            self.assertEqual(json.load(response)["user_id"], "alice")
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        conversation = Request(
            self.base + "/v1/conversations",
            data=json.dumps({"repository": {"url": "https://example.com/repo.git"}}).encode(),
            headers={"Content-Type": "application/json", "Cookie": cookie},
        )
        with urlopen(conversation) as response:
            self.assertEqual(response.status, 201)
        self.assertEqual(FakeApi.authorization, "Bearer alice-key")
        self.assertEqual(FakeApi.received["repository"]["url"], "https://example.com/repo.git")
        events = Request(self.base + "/v1/tasks/abc/events", headers={"Cookie": cookie})
        with urlopen(events) as streamed:
            self.assertIn("event: task.completed", streamed.read().decode())
        self.assertEqual(FakeApi.authorization, "Bearer alice-key")

    def test_skill_marketplace_routes_include_install_and_delete(self):
        with urlopen(self.base + "/v1/skills/catalog") as response:
            self.assertEqual(json.load(response)["items"][0]["id"], "awesome-api-design")
        with urlopen(Request(self.base + "/v1/skills/awesome-api-design/install", data=b"", method="POST")) as response:
            self.assertEqual(response.status, 202)
        with urlopen(Request(self.base + "/v1/skills/awesome-api-design", method="DELETE")) as response:
            self.assertFalse(json.load(response)["installed"])

    def test_mcp_marketplace_routes_include_install_and_delete(self):
        with urlopen(self.base + "/v1/mcp/catalog") as response:
            self.assertEqual(json.load(response)["items"][0]["id"], "context7")
        with urlopen(Request(self.base + "/v1/mcp/context7/install", data=b"", method="POST")) as response:
            self.assertEqual(response.status, 202)
        with urlopen(Request(self.base + "/v1/mcp/context7", method="DELETE")) as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
