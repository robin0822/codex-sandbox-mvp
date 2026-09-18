"""Local, same-origin server for the Codex conversation UI."""

import base64
import getpass
import json
import os
import re
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


WEB_DIR = Path(__file__).resolve().parent
UPSTREAM = os.environ.get("CODEX_API_BASE", "http://127.0.0.1:18080").rstrip("/")
SSH_HOST = os.environ.get("CODEX_SSH_HOST", "")
SSH_USER = os.environ.get("CODEX_SSH_USER", "root")
SSH_PASSWORD = os.environ.get("CODEX_SSH_PASSWORD", "")
HOST = "127.0.0.1"
PORT = int(os.environ.get("CODEX_WEB_PORT", "5173"))
REMOTE_API_PORT = int(os.environ.get("CODEX_REMOTE_API_PORT", "18080"))
SESSIONS = {}
SESSION_LOCK = threading.Lock()
SESSION_SECONDS = 12 * 60 * 60
SESSION_COOKIE = "codex_local_session"

REMOTE_PROXY = r"""
import base64
import json
import shutil
import sys
import urllib.error
import urllib.request

options = json.loads(base64.b64decode(sys.argv[2]))
body = sys.stdin.buffer.read(options["body_length"])
request = urllib.request.Request(
    "http://127.0.0.1:" + str(options["port"]) + options["path"],
    data=body if options["method"] == "POST" else None,
    headers=options["headers"],
    method=options["method"],
)
try:
    response = urllib.request.urlopen(request, timeout=240)
except urllib.error.HTTPError as error:
    response = error
except Exception as error:
    payload = json.dumps({"detail": str(error)}).encode()
    metadata = {"status": 502, "content_type": "application/json", "retry_after": None}
    header = json.dumps(metadata).encode()
    sys.stdout.buffer.write(len(header).to_bytes(4, "big") + header + payload)
    sys.stdout.buffer.flush()
    sys.exit(0)

with response:
    metadata = {
        "status": response.status,
        "content_type": response.headers.get("Content-Type", "application/json"),
        "retry_after": response.headers.get("Retry-After"),
    }
    header = json.dumps(metadata).encode()
    sys.stdout.buffer.write(len(header).to_bytes(4, "big") + header)
    sys.stdout.buffer.flush()
    if metadata["content_type"].startswith("text/event-stream"):
        for line in response:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
    else:
        shutil.copyfileobj(response, sys.stdout.buffer)
        sys.stdout.buffer.flush()
"""


def ssh_request(method, path, headers=None, body=b""):
    import paramiko

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    try:
        client.connect(
            SSH_HOST,
            username=SSH_USER,
            password=SSH_PASSWORD,
            look_for_keys=False,
            allow_agent=False,
            timeout=7,
            auth_timeout=10,
        )
        options = json.dumps({
            "method": method,
            "path": path,
            "headers": headers or {},
            "body_length": len(body),
            "port": REMOTE_API_PORT,
        })
        encoded_code = base64.b64encode(REMOTE_PROXY.encode()).decode()
        encoded_options = base64.b64encode(options.encode()).decode()
        command = (
            "python3 -u -c 'import base64,sys;"
            "exec(base64.b64decode(sys.argv[1]))' "
            f"{encoded_code} {encoded_options}"
        )
        stdin, stdout, stderr = client.exec_command(command, timeout=240)
        if body:
            stdin.write(body)
            stdin.flush()
        stdin.channel.shutdown_write()
        size = stdout.read(4)
        if len(size) != 4:
            raise RuntimeError("SSH API bridge returned no response: " + stderr.read(500).decode(errors="replace"))
        metadata = json.loads(stdout.read(int.from_bytes(size, "big")))
        return metadata, stdout, stderr, client
    except Exception:
        client.close()
        raise


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, _format, *_args):
        pass

    def end_headers(self):
        if urlsplit(self.path).path in {"/", "/index.html", "/app.js", "/styles.css"}:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/status":
            self._status()
        elif path == "/api/me":
            self._me()
        elif path.startswith("/v1/") or path.startswith("/health/"):
            self._proxy()
        else:
            super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/login":
            self._login()
        elif path == "/api/logout":
            self._logout()
        elif path in ("/v1/tasks", "/v1/conversations") or re.fullmatch(
            r"/v1/conversations/[0-9a-f]{32}/turns", path
        ) or re.fullmatch(r"/v1/skills/[a-z][a-z0-9-]{0,63}/install", path):
            self._proxy()
        else:
            self.send_error(404)

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if re.fullmatch(r"/v1/skills/[a-z][a-z0-9-]{0,63}", path):
            self._proxy()
        else:
            self.send_error(404)

    def _session_key(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            session_id = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None
        except Exception:
            return None
        if not session_id:
            return None
        with SESSION_LOCK:
            entry = SESSIONS.get(session_id)
            if not entry:
                return None
            if entry[1] < time.time():
                SESSIONS.pop(session_id, None)
                return None
            return entry[0]

    def _api_json(self, method, path, headers=None, body=None):
        if SSH_HOST:
            metadata, response, stderr, client = ssh_request(method, path, headers or {}, body or b"")
            try:
                payload = response.read()
            finally:
                client.close()
            return metadata["status"], payload
        request = Request(f"{UPSTREAM}{path}", data=body, headers=headers or {}, method=method)
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def _json_response(self, code, payload, cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def _me(self):
        headers = {}
        key = self._session_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            code, payload = self._api_json("GET", "/v1/me", headers)
        except Exception as error:
            self._json_response(502, json.dumps({"detail": str(error)}).encode())
            return
        self._json_response(code, payload)

    def _login(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 8192:
            self.send_error(413)
            return
        try:
            key = json.loads(self.rfile.read(length))["api_key"]
            if not isinstance(key, str) or not key or len(key) > 4096:
                raise ValueError("invalid API key")
            code, payload = self._api_json("GET", "/v1/me", {"Authorization": f"Bearer {key}"})
        except (ValueError, KeyError, TypeError):
            self._json_response(400, json.dumps({"detail": "请输入有效的 API Key"}).encode())
            return
        except Exception as error:
            self._json_response(502, json.dumps({"detail": str(error)}).encode())
            return
        if code != 200:
            self._json_response(code, payload)
            return
        session_id = secrets.token_urlsafe(32)
        with SESSION_LOCK:
            SESSIONS[session_id] = (key, time.time() + SESSION_SECONDS)
        self._json_response(200, payload,
                            f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}")

    def _logout(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        if SESSION_COOKIE in cookie:
            with SESSION_LOCK:
                SESSIONS.pop(cookie[SESSION_COOKIE].value, None)
        self._json_response(200, b'{"ok":true}',
                            f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")

    def _status(self):
        try:
            if SSH_HOST:
                metadata, response, stderr, client = ssh_request("GET", "/health/ready")
                try:
                    response.read()
                    ready = metadata["status"] == 200
                finally:
                    if stderr.channel.exit_status_ready():
                        error_text = stderr.read().decode(errors="replace").strip()
                        if error_text:
                            print("SSH API bridge:", error_text, file=sys.stderr, flush=True)
                    client.close()
            else:
                with urlopen(f"{UPSTREAM}/health/ready", timeout=2) as response:
                    ready = response.status == 200
        except Exception:
            ready = False
        body = json.dumps({"ready": ready}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        path = urlsplit(self.path)
        target = f"{UPSTREAM}{path.path}"
        if path.query:
            target += f"?{path.query}"
        body = None
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100_000:
                self.send_error(413)
                return
            body = self.rfile.read(length)
        headers = {}
        for name in ("Content-Type", "Accept", "Last-Event-ID"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        key = self._session_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if SSH_HOST:
            self._proxy_ssh(path.path + (f"?{path.query}" if path.query else ""), headers, body or b"")
            return
        request = Request(target, data=body, headers=headers, method=self.command)
        try:
            response = urlopen(request, timeout=240)
        except HTTPError as error:
            response = error
        except (URLError, TimeoutError) as error:
            payload = json.dumps({"detail": f"后端 API 不可用：{error.reason if isinstance(error, URLError) else error}"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        with response:
            content_type = response.headers.get("Content-Type", "application/json")
            is_stream = content_type.startswith("text/event-stream")
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            if response.headers.get("Retry-After"):
                self.send_header("Retry-After", response.headers["Retry-After"])
            if is_stream:
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    for line in response:
                        self.wfile.write(line)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                payload = response.read()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

    def _proxy_ssh(self, path, headers, body):
        try:
            metadata, response, stderr, client = ssh_request(self.command, path, headers, body)
        except Exception as error:
            payload = json.dumps({"detail": f"SSH API 连接失败：{error}"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        try:
            self.send_response(metadata["status"])
            content_type = metadata.get("content_type", "application/json")
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            if metadata.get("retry_after"):
                self.send_header("Retry-After", metadata["retry_after"])
            if content_type.startswith("text/event-stream"):
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                for line in response:
                    self.wfile.write(line.encode() if isinstance(line, str) else line)
                    self.wfile.flush()
            else:
                payload = response.read()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if stderr.channel.exit_status_ready():
                error_text = stderr.read().decode(errors="replace").strip()
                if error_text:
                    print("SSH API bridge:", error_text, file=sys.stderr, flush=True)
            client.close()


if __name__ == "__main__":
    if SSH_HOST and not SSH_PASSWORD:
        SSH_PASSWORD = getpass.getpass(f"SSH password for {SSH_USER}@{SSH_HOST}: ")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    target = f"SSH {SSH_USER}@{SSH_HOST} -> 127.0.0.1:{REMOTE_API_PORT}" if SSH_HOST else UPSTREAM
    print(f"Codex UI: http://{HOST}:{PORT}  |  API: {target}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
