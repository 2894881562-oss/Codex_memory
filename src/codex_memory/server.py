"""Local-only authenticated dashboard and narrowly scoped live collector."""

from __future__ import annotations

import hmac
import json
import mimetypes
import re
import secrets
import sys
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 5 * 1024 * 1024
ASSETS = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}\Z")


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


class MemoryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service: Any, host: str, port: int):
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("管理面板仅允许绑定本机 127.0.0.1 / localhost")
        from .security import ensure_collector_token

        self.service = service
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.collector_token = ensure_collector_token(service.data_dir)
        self.operation_lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.last_sync: dict[str, Any] = {"running": False, "at": None, "reports": []}
        self.last_live: dict[str, Any] = {"at": None, "account": "", "conversation_id": ""}
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def sync(self) -> list[dict[str, Any]]:
        with self.operation_lock:
            with self.status_lock:
                self.last_sync["running"] = True
            try:
                reports = self.service.sync_sources()
                with self.status_lock:
                    self.last_sync = {"running": False, "at": time.time(), "reports": reports}
                return reports
            except Exception:
                with self.status_lock:
                    self.last_sync = {
                        "running": False,
                        "at": time.time(),
                        "error": "来源同步失败；请检查路径与来源格式",
                    }
                raise

    def start_sync(self, interval: float) -> threading.Thread | None:
        if interval == 0:
            return None
        if interval < 0.5:
            raise ValueError("同步间隔至少为 0.5 秒，或设为 0 关闭")

        def watch() -> None:
            while not self.stop_event.is_set():
                try:
                    self.sync()
                except Exception:
                    pass  # Status is recorded; the next source update can recover.
                self.stop_event.wait(interval)

        worker = threading.Thread(target=watch, name="memory-source-sync", daemon=True)
        worker.start()
        return worker

    def server_close(self) -> None:
        self.stop_event.set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: MemoryHTTPServer
    server_version = "LocalMemory/0.1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # No chat contents, tokens, passwords or query text in request logs.

    def _validate_host(self) -> None:
        allowed = {f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}"}
        if self.headers.get("Host", "").lower() not in allowed:
            raise RequestError(403, "Host 不受信任")

    def _same_origin(self) -> bool:
        return self.headers.get("Origin", "") in {
            f"http://127.0.0.1:{self.server.port}",
            f"http://localhost:{self.server.port}",
        }

    def _extension_origin(self) -> str:
        origin = self.headers.get("Origin", "")
        return origin if EXTENSION_ORIGIN.fullmatch(origin) else ""

    def _session(self, *, write: bool = False) -> None:
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError(403, "仅接受本机面板请求")
        origin = self.headers.get("Origin")
        if origin is not None and not self._same_origin():
            raise RequestError(403, "Origin 不受信任")
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            supplied = cookie["memory_session"].value if "memory_session" in cookie else ""
        except Exception:
            supplied = ""
        if not hmac.compare_digest(supplied, self.server.session_token):
            raise RequestError(401, "请先打开本机管理面板")
        if write:
            if not self._same_origin():
                raise RequestError(403, "写入需要本机 Origin")
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf_token):
                raise RequestError(403, "CSRF 校验未通过，请刷新面板")

    def _collector(self) -> None:
        origin = self.headers.get("Origin")
        if origin is not None and not self._extension_origin():
            raise RequestError(403, "实时采集仅接受已配对浏览器扩展")
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.collector_token
        if not hmac.compare_digest(supplied, expected):
            raise RequestError(401, "采集配对令牌无效")

    def _headers(
        self,
        status: int,
        content_type: str,
        *,
        length: int = 0,
        cookie: bool = False,
        cors: bool = False,
        attachment: str = "",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'",
        )
        if cookie:
            self.send_header(
                "Set-Cookie", f"memory_session={self.server.session_token}; HttpOnly; SameSite=Strict; Path=/"
            )
        if cors and self._extension_origin():
            self.send_header("Access-Control-Allow-Origin", self._extension_origin())
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()

    def _json(self, result: Any, status: int = 200, *, cors: bool = False) -> None:
        body = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", length=len(body), cors=cors)
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "不支持流式请求体")
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "请求需为 application/json")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError as exc:
            raise RequestError(400, "请求长度无效") from exc
        if length < 0:
            raise RequestError(411, "需要 Content-Length")
        if length > MAX_BODY:
            raise RequestError(413, "请求体超过 5 MB")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(400, "JSON 内容无效") from exc
        if not isinstance(body, dict):
            raise RequestError(400, "请求需为 JSON 对象")
        return body

    def _error(self, exc: Exception, *, cors: bool = False) -> None:
        if isinstance(exc, RequestError):
            self._json({"error": exc.message}, exc.status, cors=cors)
        elif isinstance(exc, (ValueError, OSError)):
            # Local selected paths may be useful, but credentials supplied in fields are never echoed.
            self._json({"error": "参数、路径或档案无效；请检查输入与本地文件"}, 400, cors=cors)
        else:
            self._json({"error": "本地服务无法完成请求；原档案保留"}, 500, cors=cors)

    def do_OPTIONS(self) -> None:
        try:
            self._validate_host()
            if urlsplit(self.path).path not in {"/api/live", "/api/resume"} or not self._extension_origin():
                raise RequestError(403, "不接受该跨来源请求")
            self._headers(204, "application/json", cors=True)
        except Exception as exc:
            self._error(exc)

    def do_GET(self) -> None:
        try:
            self._validate_host()
            route = urlsplit(self.path)
            path = route.path
            query = {k: values[-1] for k, values in parse_qs(route.query).items()}
            if path in ASSETS:
                origin = self.headers.get("Origin")
                if (origin and not self._same_origin()) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                    raise RequestError(403, "仅接受本机面板请求")
                asset = Path(__file__).parent / "web" / ASSETS[path]
                body = asset.read_bytes()
                content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                self._headers(200, content_type + "; charset=utf-8", length=len(body), cookie=path == "/")
                self.wfile.write(body)
                return
            self._session()
            service = self.server.service
            if path == "/api/session":
                result = {
                    "csrf_token": self.server.csrf_token,
                    "data_dir": str(service.data_dir),
                    "local_only": True,
                    "version": "0.1.0",
                }
            elif path == "/api/stats":
                result = service.stats()
                with self.server.status_lock:
                    result = {
                        **result,
                        "sync": dict(self.server.last_sync),
                        "live": dict(self.server.last_live),
                    }
            elif path == "/api/conversations":
                result = service.list_conversations(
                    source=query.get("source", ""),
                    account=query.get("account", ""),
                    project=query.get("project", ""),
                    limit=int(query.get("limit", 50)),
                    offset=int(query.get("offset", 0)),
                )
            elif path == "/api/conversation":
                result = service.fetch_conversation(
                    query.get("id", ""),
                    limit=int(query.get("limit", 100)),
                    offset=int(query.get("offset", 0)),
                )
            elif path == "/api/search":
                result = service.search(
                    query.get("q", ""),
                    source=query.get("source", ""),
                    account=query.get("account", ""),
                    project=query.get("project", ""),
                    limit=int(query.get("limit", 30)),
                )
            elif path == "/api/projects":
                result = service.list_projects()
            elif path == "/api/user-memory":
                result = service.list_user_memory(
                    project=query.get("project", ""),
                    include_proposed=query.get("include_proposed", "").lower() in {"1", "true"},
                    limit=int(query.get("limit", 100)),
                )
            elif path == "/api/project":
                result = service.project_state(query.get("project", ""))
            elif path == "/api/files":
                result = service.list_project_files(
                    query.get("project", ""),
                    limit=int(query.get("limit", 100)),
                    offset=int(query.get("offset", 0)),
                )
            elif path == "/api/file":
                result = service.fetch_project_file(
                    query.get("project", ""),
                    query.get("path", ""),
                    max_chars=int(query.get("max_chars", 20000)),
                )
            elif path == "/api/sources":
                result = service.list_sources()
            elif path == "/api/pairing":
                if not self._same_origin() and self.headers.get("Sec-Fetch-Site") != "same-origin":
                    raise RequestError(403, "配对信息仅能从同来源面板读取")
                result = {
                    "collector_token": self.server.collector_token,
                    "endpoint": self.server.url + "/api/live",
                    "coverage": "仅采集扩展正在打开的可见 ChatGPT 会话；历史全量内容请导入官方导出。",
                }
            elif path == "/api/handoff":
                result = service.handoff(
                    project=query.get("project", ""),
                    conversation_id=query.get("id", ""),
                    max_chars=int(query.get("max_chars", 16000)),
                    task=query.get("task", ""),
                )
                if query.get("download") == "1":
                    body = result.encode("utf-8")
                    self._headers(
                        200, "text/markdown; charset=utf-8", length=len(body), attachment="memory-handoff.md"
                    )
                    self.wfile.write(body)
                    return
                result = {"text": result}
            else:
                raise RequestError(404, "接口不存在")
            self._json(result)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        cors = path in {"/api/live", "/api/resume"}
        try:
            self._validate_host()
            if path in {"/api/live", "/api/resume"}:
                self._collector()
                body = self._body()
                if path == "/api/resume":
                    result = {
                        "handoff": self.server.service.handoff(
                            project=body.get("project", ""),
                            conversation_id=body.get("conversation_id", ""),
                            max_chars=int(body.get("max_chars", 16000)),
                            task=body.get("task", ""),
                        )
                    }
                    self._json(result, cors=True)
                    return
                messages = body.get("messages")
                if not isinstance(messages, list) or len(messages) > 500:
                    raise RequestError(400, "实时请求最多 500 条消息")
                with self.server.operation_lock:
                    result = self.server.service.ingest_live(body)
                with self.server.status_lock:
                    self.server.last_live = {
                        "at": time.time(),
                        "account": str(body.get("account", "")),
                        "conversation_id": str(body.get("conversation_id", "")),
                    }
                self._json(result, cors=True)
                return
            self._session(write=True)
            body = self._body()
            service = self.server.service
            if path == "/api/sync":
                result = self.server.sync()
            else:
                with self.server.operation_lock:
                    if path == "/api/import":
                        if body.get("kind") not in {"chatgpt", "codex"}:
                            raise RequestError(400, "导入类型无效")
                        method = service.import_chatgpt if body["kind"] == "chatgpt" else service.import_codex
                        result = method(
                            Path(body["path"]),
                            account=body.get("account", "default"),
                            project=body.get("project", ""),
                        )
                    elif path == "/api/sources":
                        result = service.configure_source(
                            body["kind"],
                            Path(body["path"]),
                            account=body.get("account", "default"),
                            project=body.get("project", ""),
                        )
                    elif path == "/api/checkpoint":
                        result = service.create_checkpoint(
                            body["project"],
                            body["goal"],
                            completed=body.get("completed", ""),
                            decisions=body.get("decisions", ""),
                            next_steps=body.get("next_steps", ""),
                            blockers=body.get("blockers", ""),
                            account=body.get("account", "default"),
                        )
                    elif path == "/api/user-memory":
                        result = service.save_user_memory(
                            body["key"],
                            body["value"],
                            project=body.get("project", ""),
                            status=body.get("status", "proposed"),
                            evidence=body.get("evidence"),
                            account=body.get("account", "default"),
                        )
                    elif path == "/api/project":
                        result = service.archive_project(
                            Path(body["path"]),
                            project=body.get("project", ""),
                            account=body.get("account", "default"),
                        )
                    elif path == "/api/export":
                        result = service.export_handoff(
                            Path(body["path"]),
                            project=body.get("project", ""),
                            conversation_id=body.get("id", ""),
                            max_chars=int(body.get("max_chars", 16000)),
                            task=body.get("task", ""),
                        )
                    elif path == "/api/backup":
                        result = service.backup(Path(body["path"]), password=body.get("password") or None)
                    elif path == "/api/audit":
                        result = service.audit()
                    else:
                        raise RequestError(404, "接口不存在")
            self._json(result)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (KeyError, TypeError):
            self._error(RequestError(400, "缺少字段或字段类型无效"), cors=cors)
        except Exception as exc:
            self._error(exc, cors=cors)


def create_server(service: Any, *, host: str = "127.0.0.1", port: int = 8765) -> MemoryHTTPServer:
    service.initialize()
    return MemoryHTTPServer(service, host, port)


def serve(
    service: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    sync_interval: float = 3.0,
    open_browser: bool = False,
) -> None:
    if sync_interval < 0 or 0 < sync_interval < 0.5:
        raise ValueError("同步间隔至少为 0.5 秒，或设为 0 关闭")
    server = create_server(service, host=host, port=port)
    try:
        server.start_sync(sync_interval)
        print(f"本地记忆面板：{server.url}  （Ctrl+C 停止）", file=sys.stderr, flush=True)
        if open_browser:
            webbrowser.open(server.url)
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
