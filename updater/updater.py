from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.getenv("UPDATE_AGENT_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("UPDATE_AGENT_PORT", "9011"))
TOKEN_FILE = Path(os.getenv("UPDATE_AGENT_TOKEN_FILE", "/run/voltcore-updater/token"))
WORKSPACE = Path(os.getenv("UPDATE_AGENT_WORKSPACE", "/workspace"))
COMPOSE_FILE = Path(os.getenv("UPDATE_AGENT_COMPOSE_FILE", str(WORKSPACE / "docker-compose.yml")))
COMPOSE_PROJECT = os.getenv("UPDATE_AGENT_COMPOSE_PROJECT", "voltcore-community").strip() or "voltcore-community"
COMPOSE_SERVICE = os.getenv("UPDATE_AGENT_COMPOSE_SERVICE", "voltcore-community").strip() or "voltcore-community"
IMAGE_REPOSITORY = os.getenv("UPDATE_AGENT_IMAGE_REPOSITORY", "ghcr.io/hotteftw1981/voltcore-community").strip()
LATEST_ALIAS = os.getenv("UPDATE_AGENT_LATEST_ALIAS", f"{IMAGE_REPOSITORY}:latest").strip()
VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,5}$")
_update_lock = threading.Lock()
_update_active = False


def _ensure_token() -> str:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(48)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(token, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(TOKEN_FILE)
    return token


TOKEN = _ensure_token()


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    merged = os.environ.copy()
    if env:
        merged.update(env)
    subprocess.run(cmd, cwd=str(WORKSPACE), env=merged, check=True)


def _perform_update(target_version: str) -> None:
    global _update_active
    try:
        time.sleep(2)
        exact_image = f"{IMAGE_REPOSITORY}:v{target_version}"
        _run(["docker", "pull", exact_image])
        _run(["docker", "tag", exact_image, LATEST_ALIAS])
        env = {"VOLTCORE_COMMUNITY_IMAGE": exact_image}
        _run([
            "docker", "compose",
            "-p", COMPOSE_PROJECT,
            "-f", str(COMPOSE_FILE),
            "up", "-d", "--no-deps", "--force-recreate", COMPOSE_SERVICE,
        ], env=env)
        print(f"VoltCore Community update requested successfully: {exact_image}", flush=True)
    except Exception as exc:
        print(f"VoltCore Community update failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        with _update_lock:
            _update_active = False


class Handler(BaseHTTPRequestHandler):
    server_version = "VoltCoreCommunityUpdater/1.0"

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "busy": _update_active})
            return
        self._send(404, {"detail": "Not found"})

    def do_POST(self) -> None:
        global _update_active
        if self.path != "/update":
            self._send(404, {"detail": "Not found"})
            return
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        if not hmac.compare_digest(auth, expected):
            self._send(401, {"detail": "Unauthorized"})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 65536)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, {"detail": "Invalid JSON"})
            return
        target = str(body.get("target_version") or "").strip().removeprefix("v")
        if not VERSION_RE.fullmatch(target):
            self._send(400, {"detail": "Invalid target version"})
            return
        if not COMPOSE_FILE.is_file():
            self._send(503, {"detail": "Compose file is unavailable"})
            return
        with _update_lock:
            if _update_active:
                self._send(409, {"detail": "Update already running"})
                return
            _update_active = True
        threading.Thread(target=_perform_update, args=(target,), daemon=True).start()
        self._send(202, {"ok": True, "target_version": target, "provider": "docker-compose"})


if __name__ == "__main__":
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
