"""HTTP security, CLI integration and optional SDK tools. Temporary archives are retained."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import importlib.util
import io
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_memory import __version__
from codex_memory.cli import build_parser, main, run
from codex_memory.mcp_server import LoopbackBearerGuard, _bounded_thread, create_mcp
from codex_memory.models import Conversation, ImportBatch, Message
from codex_memory.server import MAX_BODY, create_server
from codex_memory.service import MemoryService


class HTTPInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="codex-memory-http-"))
        cls.service = MemoryService(cls.directory / "vault")
        cls.server = create_server(cls.service, port=0)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.origin = f"http://127.0.0.1:{cls.server.port}"
        _, _, headers = cls.request("GET", "/")
        cls.cookie = headers["Set-Cookie"].split(";", 1)[0]
        _, session, _ = cls.request("GET", "/api/session", headers={"Cookie": cls.cookie})
        cls.csrf = session["csrf_token"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)
        # Deliberately retain temporary directories; no recursive cleanup.

    @classmethod
    def request(cls, method: str, path: str, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.server.port, timeout=5)
        supplied = dict(headers or {})
        if body is not None:
            body = json.dumps(body).encode() if not isinstance(body, bytes) else body
            supplied.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type", "")
        value = json.loads(raw) if "application/json" in content_type and raw else raw.decode("utf-8")
        result = response.status, value, dict(response.getheaders())
        connection.close()
        return result

    def dashboard_headers(self):
        return {"Cookie": self.cookie, "Origin": self.origin, "X-CSRF-Token": self.csrf}

    def collector_headers(self, *, extension: bool = False):
        headers = {"Authorization": "Bearer " + self.server.collector_token}
        if extension:
            headers["Origin"] = "chrome-extension://" + "a" * 32
        return headers

    def test_dashboard_and_cookie(self):
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("续忆", body)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_api_requires_dashboard_session(self):
        status, _, _ = self.request("GET", "/api/stats")
        self.assertEqual(status, 401)
        status, stats, _ = self.request("GET", "/api/stats", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn("conversations", stats)
        self.assertTrue(stats["runtime"]["online"])
        self.assertEqual(stats["runtime"]["version"], __version__)
        self.assertGreaterEqual(stats["runtime"]["checked_at"], stats["runtime"]["server_started_at"])

    def test_collector_status_is_current_authenticated_read_only_and_minimal(self):
        with (
            patch.object(self.service, "sync_sources", side_effect=AssertionError("must not sync")),
            patch.object(self.service, "ingest_live", side_effect=AssertionError("must not ingest")),
            patch.object(self.service, "handoff", side_effect=AssertionError("must not build a handoff")),
        ):
            status, body, headers = self.request(
                "POST", "/api/collector/status", {}, headers=self.collector_headers(extension=True)
            )
        self.assertEqual(status, 200)
        self.assertTrue(body["online"])
        self.assertEqual(body["version"], __version__)
        self.assertGreaterEqual(body["checked_at"], self.server.started_at)
        self.assertEqual(body["server_started_at"], self.server.started_at)
        self.assertEqual(body["coverage"], "opened_rendered_chat_only")
        self.assertEqual(
            set(body), {"online", "version", "checked_at", "server_started_at", "last_live", "coverage"}
        )
        self.assertEqual(set(body["last_live"]), {"at", "account", "conversation_id"})
        self.assertNotIn(self.server.collector_token, json.dumps(body))
        self.assertNotIn(str(self.service.data_dir), json.dumps(body))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "chrome-extension://" + "a" * 32)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_collector_status_security_and_preflight(self):
        for supplied, expected in (
            ({}, 401),
            ({"Authorization": "Bearer wrong"}, 401),
            ({"Cookie": self.cookie}, 401),
            ({**self.collector_headers(), "Origin": "https://chatgpt.com"}, 403),
            ({**self.collector_headers(), "Host": "attacker.example"}, 403),
        ):
            with self.subTest(supplied={key: key for key in supplied}):
                status, _, _ = self.request("POST", "/api/collector/status", {}, headers=supplied)
                self.assertEqual(status, expected)
        status, _, headers = self.request(
            "OPTIONS", "/api/collector/status", headers={"Origin": "chrome-extension://" + "a" * 32}
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Methods"], "POST, OPTIONS")
        self.assertEqual(headers["Access-Control-Allow-Private-Network"], "true")
        status, _, _ = self.request(
            "OPTIONS", "/api/collector/status", headers={"Origin": "https://attacker.example"}
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/collector/status", [], headers=self.collector_headers())
        self.assertEqual(status, 400)

    def test_host_rebinding_rejected(self):
        status, _, _ = self.request("GET", "/", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)

    def test_foreign_origin_and_site_rejected(self):
        status, _, _ = self.request(
            "GET", "/api/stats", headers={"Cookie": self.cookie, "Origin": "https://attacker.example"}
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_write_requires_origin_and_csrf(self):
        payload = {"project": "boundary", "goal": "check"}
        status, _, _ = self.request("POST", "/api/checkpoint", payload, headers={"Cookie": self.cookie})
        self.assertEqual(status, 403)
        headers = self.dashboard_headers()
        headers["X-CSRF-Token"] = "wrong"
        status, _, _ = self.request("POST", "/api/checkpoint", payload, headers=headers)
        self.assertEqual(status, 403)

    def test_checkpoint_project_handoff_flow(self):
        payload = {
            "project": "http-project",
            "goal": "继续另一账户",
            "next_steps": "继续工作",
            "account": "plus-A",
        }
        status, checkpoint, _ = self.request(
            "POST", "/api/checkpoint", payload, headers=self.dashboard_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(checkpoint["account"], "plus-A")
        status, project, _ = self.request(
            "GET", "/api/project?project=http-project", headers=self.dashboard_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(project["latest_checkpoint"]["goal"], payload["goal"])
        status, handoff, _ = self.request(
            "GET", "/api/handoff?project=http-project", headers=self.dashboard_headers()
        )
        self.assertEqual(status, 200)
        self.assertIn("继续另一账户", handoff["text"])

    def test_pairing_requires_same_origin_and_does_not_use_session_as_collector(self):
        status, _, _ = self.request("GET", "/api/pairing", headers={"Cookie": self.cookie})
        self.assertEqual(status, 403)
        status, paired, _ = self.request("GET", "/api/pairing", headers=self.dashboard_headers())
        self.assertEqual(status, 200)
        self.assertEqual(paired["collector_token"], self.server.collector_token)
        self.assertNotEqual(paired["collector_token"], self.server.session_token)
        status, _, _ = self.request(
            "POST", "/api/live", {}, headers={"Authorization": "Bearer " + self.server.session_token}
        )
        self.assertEqual(status, 401)

    def test_live_capture_and_token_boundary(self):
        payload = {
            "source": "chatgpt",
            "account": "plus-B",
            "conversation_id": "live-interface-test",
            "title": "跨账户记忆",
            "project": "live-project",
            "messages": [{"id": "turn-1", "role": "user", "text": "实时衔接工程消息"}],
        }
        status, report, headers = self.request(
            "POST", "/api/live", payload, headers=self.collector_headers(extension=True)
        )
        self.assertEqual(status, 200)
        self.assertEqual(report["messages"], 1)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "chrome-extension://" + "a" * 32)
        status, _, _ = self.request(
            "POST", "/api/checkpoint", {"project": "bad", "goal": "bad"}, headers=self.collector_headers()
        )
        self.assertEqual(status, 401)
        hits = self.service.search("实时衔接", account="plus-B")
        self.assertEqual(len(hits), 1)

    def test_collector_resume_cross_account_reference(self):
        self.service.create_checkpoint("resume-project", "本地读取进度", account="plus-one")
        status, data, _ = self.request(
            "POST",
            "/api/resume",
            {"project": "resume-project", "task": "review prior decisions"},
            headers=self.collector_headers(extension=True),
        )
        self.assertEqual(status, 200)
        self.assertIn("本地读取进度", data["handoff"])
        self.assertIn("review prior decisions", data["handoff"])
        self.assertIn("参考资料", data["handoff"])
        status, _, _ = self.request("POST", "/api/resume", {"project": "resume-project"}, headers={})
        self.assertEqual(status, 401)

    def test_task_is_forwarded_by_dashboard_collector_and_export(self):
        with patch.object(self.service, "handoff", return_value="task-scoped reference") as handoff:
            status, data, _ = self.request(
                "GET",
                "/api/handoff?project=task-demo&task=repair+offline+queue",
                headers=self.dashboard_headers(),
            )
            self.assertEqual(status, 200)
            self.assertEqual(data["text"], "task-scoped reference")
            self.assertEqual(handoff.call_args.kwargs["task"], "repair offline queue")
            status, _, _ = self.request(
                "POST",
                "/api/resume",
                {"project": "task-demo", "task": "continue queue retry"},
                headers=self.collector_headers(),
            )
            self.assertEqual(status, 200)
            self.assertEqual(handoff.call_args.kwargs["task"], "continue queue retry")
        with patch.object(self.service, "export_handoff", return_value={"path": "fixture.md"}) as export:
            status, _, _ = self.request(
                "POST",
                "/api/export",
                {
                    "project": "task-demo",
                    "path": str(self.directory / "fixture.md"),
                    "task": "review citations",
                },
                headers=self.dashboard_headers(),
            )
            self.assertEqual(status, 200)
            self.assertEqual(export.call_args.kwargs["task"], "review citations")

    def test_invalid_task_is_rejected_by_http_without_checkpoint_mutation(self):
        self.service.create_checkpoint("task-validation", "existing checkpoint")
        before = self.service.stats()["checkpoints"]
        for task in ("x" * 513, 42, False, ["repair"]):
            status, _, _ = self.request(
                "POST",
                "/api/resume",
                {"project": "task-validation", "task": task},
                headers=self.collector_headers(),
            )
            self.assertEqual(status, 400)
        status, _, _ = self.request(
            "GET", "/api/handoff?project=task-validation&task=" + "x" * 513, headers=self.dashboard_headers()
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.service.stats()["checkpoints"], before)

    def test_user_memory_http_proposed_confirmed_withdrawn_lifecycle(self):
        self.service.ingest_live(
            {
                "account": "profile-user",
                "conversation_id": "profile-evidence",
                "messages": [{"id": "preference", "role": "user", "text": "我希望先给结论，再展开细节。"}],
            }
        )
        conversation = self.service.list_conversations(account="profile-user")[0]
        message_id = self.service.fetch_conversation(conversation["id"])["messages"][0]["id"]
        payload = {"key": "profile.http", "value": "先给结论，再展开细节。", "account": "profile-user"}
        status, proposed, _ = self.request(
            "POST", "/api/user-memory", payload, headers=self.dashboard_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(proposed["status"], "proposed")
        status, confirmed_only, _ = self.request("GET", "/api/user-memory", headers=self.dashboard_headers())
        self.assertEqual(status, 200)
        self.assertFalse(any(item["key"] == payload["key"] for item in confirmed_only))
        status, all_states, _ = self.request(
            "GET", "/api/user-memory?include_proposed=1", headers=self.dashboard_headers()
        )
        self.assertEqual(status, 200)
        self.assertTrue(
            any(item["key"] == payload["key"] and item["status"] == "proposed" for item in all_states)
        )
        status, _, _ = self.request(
            "POST", "/api/user-memory", {**payload, "status": "confirmed"}, headers=self.dashboard_headers()
        )
        self.assertEqual(status, 400)
        status, confirmed, _ = self.request(
            "POST",
            "/api/user-memory",
            {**payload, "status": "confirmed", "evidence": [message_id]},
            headers=self.dashboard_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")
        status, withdrawn, _ = self.request(
            "POST", "/api/user-memory", {**payload, "status": "withdrawn"}, headers=self.dashboard_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(withdrawn["status"], "withdrawn")
        status, all_states, _ = self.request(
            "GET", "/api/user-memory?include_proposed=true", headers=self.dashboard_headers()
        )
        self.assertTrue(
            any(item["key"] == payload["key"] and item["status"] == "withdrawn" for item in all_states)
        )

    def test_user_memory_http_authentication_and_csrf(self):
        payload = {"key": "profile.boundary", "value": "a proposed preference"}
        status, _, _ = self.request("GET", "/api/user-memory")
        self.assertEqual(status, 401)
        status, _, _ = self.request("POST", "/api/user-memory", payload, headers=self.collector_headers())
        self.assertEqual(status, 401)
        headers = self.dashboard_headers()
        headers["X-CSRF-Token"] = "invalid"
        status, _, _ = self.request("POST", "/api/user-memory", payload, headers=headers)
        self.assertEqual(status, 403)

    def test_collector_foreign_and_invalid_origins_rejected(self):
        for origin in ("https://chatgpt.com", "chrome-extension://invalid", "null", self.origin):
            headers = self.collector_headers()
            headers["Origin"] = origin
            status, _, _ = self.request("POST", "/api/live", {}, headers=headers)
            self.assertEqual(status, 403)

    def test_extension_preflight(self):
        headers = {"Origin": "chrome-extension://" + "b" * 32}
        for path in ("/api/live", "/api/resume"):
            status, _, returned = self.request("OPTIONS", path, headers=headers)
            self.assertEqual(status, 204)
            self.assertEqual(returned["Access-Control-Allow-Private-Network"], "true")
        status, _, _ = self.request("OPTIONS", "/api/pairing", headers=headers)
        self.assertEqual(status, 403)

    def test_size_limit_and_malformed_json(self):
        headers = self.collector_headers()
        headers["Content-Length"] = str(MAX_BODY + 1)
        status, _, _ = self.request("POST", "/api/live", b"{}", headers=headers)
        self.assertEqual(status, 413)
        status, _, _ = self.request("POST", "/api/live", b"{", headers=self.collector_headers())
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "POST", "/api/live", {"messages": [{}] * 501}, headers=self.collector_headers()
        )
        self.assertEqual(status, 400)

    def test_raw_arbitrary_paths_not_exposed(self):
        for path in ("/api/raw?path=auth.json", "/../../auth.json", "/local-state/collector.token"):
            status, _, _ = self.request("GET", path, headers=self.dashboard_headers())
            self.assertEqual(status, 404)

    def test_public_bind_refused(self):
        with self.assertRaises(ValueError):
            create_server(self.service, host="0.0.0.0", port=0)


class CLIVersionTests(unittest.TestCase):
    def test_version_matches_package_without_opening_vault(self):
        with (
            patch("codex_memory.service.MemoryService") as service,
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaises(SystemExit) as exited,
        ):
            main(["--version"])
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"codex-memory {__version__}")
        service.assert_not_called()


class CLIInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="codex-memory-cli-"))
        self.service = MemoryService(self.directory / "vault")
        self.service.initialize()

    def test_cli_complete_handoff_flow(self):
        args = build_parser().parse_args(
            ["checkpoint", "cli-project", "--goal", "持续上下文", "--account", "plus-A"]
        )
        checkpoint = run(args, self.service)
        self.assertEqual(checkpoint["project"], "cli-project")
        args = build_parser().parse_args(["handoff", "--project", "cli-project"])
        self.assertIn("持续上下文", run(args, self.service))
        output = self.directory / "handoff.md"
        args = build_parser().parse_args(["handoff", "--project", "cli-project", "--output", str(output)])
        run(args, self.service)
        with self.assertRaises(FileExistsError):
            run(args, self.service)

    def test_cli_task_is_forwarded_for_preview_and_exclusive_export(self):
        args = build_parser().parse_args(
            ["handoff", "--project", "task-demo", "--task", "repair offline queue"]
        )
        with patch.object(self.service, "handoff", return_value="selected context") as handoff:
            self.assertEqual(run(args, self.service), "selected context")
            self.assertEqual(handoff.call_args.kwargs["task"], "repair offline queue")
        args = build_parser().parse_args(
            [
                "handoff",
                "--project",
                "task-demo",
                "--task",
                "repair offline queue",
                "--output",
                str(self.directory / "task.md"),
            ]
        )
        with patch.object(self.service, "export_handoff", return_value={"chars": 10}) as export:
            run(args, self.service)
            self.assertEqual(export.call_args.kwargs["task"], "repair offline queue")

    def test_cli_overlong_task_has_clean_error_exit(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            code = main(
                [
                    "--data-dir",
                    str(self.service.data_dir),
                    "handoff",
                    "--project",
                    "task-demo",
                    "--task",
                    "x" * 513,
                ]
            )
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", output.getvalue())

    def test_cli_memory_defaults_proposed_and_repeated_evidence(self):
        args = build_parser().parse_args(["memory", "save", "--key", "output.concise", "--value", "结论在前"])
        with patch.object(self.service, "save_user_memory", return_value={"status": "proposed"}) as save:
            run(args, self.service)
            self.assertEqual(save.call_args.kwargs["status"], "proposed")
            self.assertIsNone(save.call_args.kwargs["evidence"])
        args = build_parser().parse_args(
            [
                "memory",
                "save",
                "--key",
                "output.concise",
                "--value",
                "结论在前",
                "--status",
                "confirmed",
                "--evidence",
                "user-one",
                "--evidence",
                "user-two",
                "--project",
                "demo",
            ]
        )
        with patch.object(self.service, "save_user_memory", return_value={"status": "confirmed"}) as save:
            run(args, self.service)
            self.assertEqual(save.call_args.kwargs["evidence"], ["user-one", "user-two"])
            self.assertEqual(save.call_args.kwargs["project"], "demo")
        args = build_parser().parse_args(
            ["memory", "list", "--project", "demo", "--include-proposed", "--limit", "20"]
        )
        with patch.object(self.service, "list_user_memory", return_value=[]) as listing:
            run(args, self.service)
            listing.assert_called_once_with(project="demo", include_proposed=True, limit=20)

    def test_cli_memory_confirm_without_evidence_cleanly_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            code = main(
                [
                    "--data-dir",
                    str(self.service.data_dir),
                    "memory",
                    "save",
                    "--key",
                    "output.concise",
                    "--value",
                    "结论在前",
                    "--status",
                    "confirmed",
                ]
            )
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", output.getvalue())

    def test_project_add_registers_continuous_source(self):
        project_dir = self.directory / "project"
        project_dir.mkdir()
        (project_dir / "hello.txt").write_text("共享工程文件", encoding="utf-8")
        args = build_parser().parse_args(["project", "add", str(project_dir), "--project", "Shared"])
        result = run(args, self.service)
        self.assertEqual(result["source"]["kind"], "project")
        self.assertEqual(self.service.fetch_project_file("Shared", "hello.txt")["text"], "共享工程文件")

    def test_watch_once_and_interval_validation(self):
        args = build_parser().parse_args(["watch", "--once"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(run(args, self.service))
        self.assertIn("[]", output.getvalue())
        args = build_parser().parse_args(["watch", "--interval", "0.1", "--once"])
        with self.assertRaises(ValueError):
            run(args, self.service)

    def test_main_json_and_error_exit(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--data-dir", str(self.directory / "main"), "init"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "ready")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--data-dir", str(self.directory / "main"), "handoff"]), 1)

    def test_main_sqlite_failure_is_reported_without_traceback(self):
        with patch(
            "codex_memory.service.MemoryService.stats",
            side_effect=sqlite3.DatabaseError("database is corrupt"),
        ):
            with contextlib.redirect_stderr(io.StringIO()) as output:
                status = main(["--data-dir", str(self.directory / "corrupt"), "stats"])
        self.assertEqual(status, 1)
        self.assertIn("操作未完成", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


class MCPBoundaryTests(unittest.TestCase):
    def test_thread_character_budget_preserves_ids_and_filters_raw_metadata(self):
        data = {
            "conversation": {
                "id": "thread-id",
                "metadata": {"artifact": "fixture.jsonl", "raw_record": "H" * 30000},
            },
            "messages": [
                {"id": "message-one", "text": "A" * 8000, "metadata": {"line": 1, "raw_record": "R" * 30000}},
                {
                    "id": "message-two",
                    "text": "B" * 8000,
                    "metadata": {"call_id": "tool-one", "instructions": "I" * 30000},
                },
            ],
            "total": 2,
            "offset": 0,
            "has_more": False,
        }
        bounded = _bounded_thread(data, 1000)
        self.assertEqual(bounded["text_returned_chars"], 1000)
        self.assertEqual(bounded["text_total_chars"], 16000)
        self.assertTrue(bounded["text_truncated"])
        self.assertEqual([message["id"] for message in bounded["messages"]], ["message-one", "message-two"])
        self.assertEqual([len(message["text"]) for message in bounded["messages"]], [1000, 0])
        self.assertTrue(all(message["text_truncated"] for message in bounded["messages"]))
        self.assertEqual(bounded["messages"][0]["text_total_chars"], 8000)
        self.assertEqual(bounded["messages"][0]["metadata"], {"line": 1})
        self.assertNotIn("raw_record", json.dumps(bounded))
        self.assertNotIn("instructions", json.dumps(bounded))
        self.assertEqual(
            len(data["messages"][0]["text"]), 8000, "Retrieval budget must not mutate original records"
        )
        for invalid in (999, 100001, True):
            with self.assertRaises(ValueError):
                _bounded_thread(data, invalid)

    def test_http_guard_checks_host_origin_and_bearer(self):
        async def probe(headers):
            called = []
            messages = []

            async def app(scope, receive, send):
                called.append(True)

            async def send(message):
                messages.append(message)

            guard = LoopbackBearerGuard(app, "test-local-token", 8766)
            await guard({"type": "http", "headers": headers}, None, send)
            return called, messages

        authorized = [(b"host", b"127.0.0.1:8766"), (b"authorization", b"Bearer test-local-token")]
        called, _ = asyncio.run(probe(authorized))
        self.assertTrue(called)
        for headers, status in (
            (authorized[:1], 401),
            ([(b"host", b"evil.example"), *authorized[1:]], 403),
            ([*authorized, (b"origin", b"https://chatgpt.com")], 403),
        ):
            called, messages = asyncio.run(probe(headers))
            self.assertFalse(called)
            self.assertEqual(messages[0]["status"], status)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP SDK not installed")
    def test_official_sdk_tool_surface_and_write_opt_in(self):
        directory = Path(tempfile.mkdtemp(prefix="codex-memory-mcp-"))
        service = MemoryService(directory)
        read_server = create_mcp(service)
        tools = asyncio.run(read_server.list_tools())
        names = {tool.name for tool in tools}
        self.assertTrue(
            {"search_memory", "fetch_thread", "fetch_message", "resume_project", "fetch_project_file"}
            <= names
        )
        self.assertNotIn("save_checkpoint", names)
        self.assertIn("list_user_memory", names)
        self.assertNotIn("save_user_memory", names)
        self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools))
        write_server = create_mcp(service, allow_writes=True)
        write_tools = asyncio.run(write_server.list_tools())
        save = next(tool for tool in write_tools if tool.name == "save_checkpoint")
        self.assertFalse(save.annotations.readOnlyHint)
        self.assertFalse(save.annotations.destructiveHint)
        save_memory = next(tool for tool in write_tools if tool.name == "save_user_memory")
        self.assertFalse(save_memory.annotations.readOnlyHint)
        self.assertFalse(save_memory.annotations.destructiveHint)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP SDK not installed")
    def test_mcp_resume_handoff_and_prompt_forward_current_task(self):
        directory = Path(tempfile.mkdtemp(prefix="codex-memory-mcp-task-"))
        service = MemoryService(directory)
        mcp = create_mcp(service)
        with patch.object(service, "handoff", return_value="task-specific reference") as handoff:
            for tool in ("resume_project", "create_handoff"):
                asyncio.run(mcp.call_tool(tool, {"project": "task-demo", "task": "repair queue retry"}))
                self.assertEqual(handoff.call_args.kwargs["task"], "repair queue retry")
            asyncio.run(
                mcp.get_prompt(
                    "continue_project", {"project": "task-demo", "task": "review earlier decision"}
                )
            )
            self.assertEqual(handoff.call_args.kwargs["task"], "review earlier decision")

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP SDK not installed")
    def test_real_sdk_stdio_handshake_and_account_independent_read(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        directory = Path(tempfile.mkdtemp(prefix="codex-memory-stdio-"))
        service = MemoryService(directory)
        service.create_checkpoint("sdk-project", "SDK stdio 实际跨账户读取", account="plus-A")

        async def check():
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "codex_memory", "--data-dir", str(directory), "mcp"]
            )
            async with asyncio.timeout(20):
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.serverInfo.name, "Local Continuous Memory")
                        tools = await session.list_tools()
                        self.assertNotIn("save_checkpoint", {tool.name for tool in tools.tools})
                        result = await session.call_tool(
                            "resume_project", {"project": "sdk-project", "task": "MCP explicit current task"}
                        )
                        self.assertFalse(result.isError)
                        self.assertIn("实际跨账户读取", result.content[0].text)
                        self.assertIn("MCP explicit current task", result.content[0].text)
                        self.assertIn("参考资料", result.content[0].text)

        asyncio.run(check())

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP SDK not installed")
    def test_real_sdk_memory_write_requires_explicit_status_and_user_evidence(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        directory = Path(tempfile.mkdtemp(prefix="codex-memory-sdk-personal-"))
        service = MemoryService(directory)
        service.ingest_live(
            {
                "account": "plus-A",
                "conversation_id": "user-memory-evidence",
                "messages": [{"id": "preference", "role": "user", "text": "请把结论放在前面。"}],
            }
        )
        cid = service.list_conversations()[0]["id"]
        evidence_id = service.fetch_conversation(cid)["messages"][0]["id"]

        async def check():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "codex_memory", "--data-dir", str(directory), "mcp", "--allow-writes"],
            )
            async with asyncio.timeout(20):
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        proposed = await session.call_tool(
                            "save_user_memory",
                            {"key": "output.concise", "value": "请把结论放在前面。", "account": "plus-B"},
                        )
                        self.assertFalse(proposed.isError)
                        data = proposed.structuredContent or json.loads(proposed.content[0].text)
                        self.assertEqual(data["status"], "proposed")
                        listing = await session.call_tool("list_user_memory", {})
                        rows = (
                            listing.structuredContent["result"]
                            if listing.structuredContent
                            else json.loads(listing.content[0].text)
                        )
                        self.assertEqual(rows, [])
                        invalid = await session.call_tool(
                            "save_user_memory",
                            {"key": "output.concise", "value": "请把结论放在前面。", "status": "confirmed"},
                        )
                        self.assertTrue(invalid.isError)
                        confirmed = await session.call_tool(
                            "save_user_memory",
                            {
                                "key": "output.concise",
                                "value": "请把结论放在前面。",
                                "status": "confirmed",
                                "evidence": [evidence_id],
                                "account": "plus-B",
                            },
                        )
                        self.assertFalse(confirmed.isError)
                        listing = await session.call_tool("list_user_memory", {})
                        rows = (
                            listing.structuredContent["result"]
                            if listing.structuredContent
                            else json.loads(listing.content[0].text)
                        )
                        self.assertEqual(rows[0]["status"], "confirmed")
                        self.assertEqual(rows[0]["evidence"], [evidence_id])

        asyncio.run(check())

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP SDK not installed")
    def test_real_sdk_bounded_thread_and_chunked_long_message(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        directory = Path(tempfile.mkdtemp(prefix="codex-memory-sdk-budget-"))
        service = MemoryService(directory)
        long_text = "A" * 1500 + "chunk boundary continuation" + "B" * 10000
        conversation = Conversation(
            "sdk-long",
            "codex",
            "plus-A",
            "Long retrieval fixture",
            [
                Message(
                    "long-one",
                    "assistant",
                    long_text,
                    metadata={
                        "artifact": "session.jsonl",
                        "line": 3,
                        "raw_record": {"instructions": "raw imported history " * 5000},
                    },
                ),
                Message("long-two", "user", "Second message " * 1000),
            ],
            updated_at="2026-10-04T03:00:00Z",
            metadata={
                "capture": "fixture",
                "session_meta": {"instructions": "long historical payload " * 5000},
            },
        )
        service._ingest(ImportBatch([conversation]), "codex", "plus-A")

        async def check():
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "codex_memory", "--data-dir", str(directory), "mcp"]
            )
            async with asyncio.timeout(20):
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.call_tool("list_threads", {})
                        rows = (
                            listed.structuredContent["result"]
                            if listed.structuredContent
                            else json.loads(listed.content[0].text)
                        )
                        self.assertNotIn("session_meta", json.dumps(rows))
                        cid = rows[0]["id"]
                        fetched = await session.call_tool(
                            "fetch_thread", {"conversation_id": cid, "max_chars": 1000}
                        )
                        self.assertFalse(fetched.isError)
                        data = fetched.structuredContent or json.loads(fetched.content[0].text)
                        self.assertEqual(sum(len(message["text"]) for message in data["messages"]), 1000)
                        self.assertEqual(
                            data["messages"][0]["metadata"], {"artifact": "session.jsonl", "line": 3}
                        )
                        self.assertNotIn("raw_record", json.dumps(data))
                        self.assertTrue(data["messages"][0]["text_truncated"])
                        mid = data["messages"][0]["id"]
                        chunk = await session.call_tool(
                            "fetch_message", {"message_id": mid, "offset": 1500, "max_chars": 27}
                        )
                        self.assertFalse(chunk.isError)
                        part = chunk.structuredContent or json.loads(chunk.content[0].text)
                        self.assertEqual(part["text"], "chunk boundary continuation")
                        self.assertEqual(part["offset"], 1500)
                        self.assertTrue(part["has_more"])
                        self.assertEqual(part["total_chars"], len(long_text))

        asyncio.run(check())

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional official MCP SDK not installed")
    def test_real_sdk_http_bearer_handshake_and_checkpoint(self):
        import httpx
        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        directory = Path(tempfile.mkdtemp(prefix="codex-memory-mcp-http-"))
        service = MemoryService(directory)
        service.initialize()
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        mcp = create_mcp(service, allow_writes=True, port=port)
        app = LoopbackBearerGuard(mcp.streamable_http_app(), "integration-local-token", port)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="error")
        )
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(server.started)
            url = f"http://127.0.0.1:{port}/mcp"
            denied = httpx.post(url, json={}, timeout=3)
            self.assertEqual(denied.status_code, 401)

            async def check():
                async with asyncio.timeout(20):
                    async with httpx.AsyncClient(
                        headers={"Authorization": "Bearer integration-local-token"}, timeout=5
                    ) as client:
                        async with streamable_http_client(url, http_client=client) as (read, write, _):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                result = await session.call_tool(
                                    "save_checkpoint",
                                    {
                                        "project": "http-sdk",
                                        "goal": "真实 HTTP 工具写入",
                                        "account": "plus-B",
                                    },
                                )
                                self.assertFalse(result.isError)
                                handoff = await session.call_tool("resume_project", {"project": "http-sdk"})
                                self.assertIn("真实 HTTP 工具写入", handoff.content[0].text)

            asyncio.run(check())
            self.assertEqual(service.project_state("http-sdk")["latest_checkpoint"]["account"], "plus-B")
        finally:
            server.should_exit = True
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())


class ExtensionTaskInterfaceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js needed for extension worker contract")
    def test_extension_resume_task_is_transient_bounded_and_forwarded(self):
        worker = Path(__file__).resolve().parents[1] / "browser-extension" / "worker.js"
        script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const state = {settings: {token: 'local-test-token', port: 8765}, queue: []};
const requests = [];
let listener;
const context = vm.createContext({TextEncoder, AbortSignal, Date, Promise, Map,
  fetch: async (url, options) => {requests.push({url, options}); return {ok: true, json: async () => ({handoff:'context'})};},
  chrome: {
    storage: {local: {
      get: async keys => {const names=typeof keys==='string'?[keys]:keys; return JSON.parse(JSON.stringify(Object.fromEntries(names.map(key=>[key,state[key]]))));},
      set: async values => Object.assign(state, JSON.parse(JSON.stringify(values)))
    }, onChanged:{addListener(){}}},
    runtime:{onMessage:{addListener(fn){listener=fn;}},onStartup:{addListener(){}}},
    alarms:{create(){},onAlarm:{addListener(){}}}
  }
});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'), context);
async function send(message){return new Promise(resolve=>listener(message,{},resolve));}
(async()=>{
  assert.equal((await send({type:'resume',project:'demo',task:'  repair queue  '})).ok,true);
  assert.deepEqual(JSON.parse(requests[0].options.body),{project:'demo',task:'repair queue'});
  assert.equal(requests[0].url,'http://127.0.0.1:8765/api/resume');
  assert.equal(state.settings.task,undefined);
  assert.equal(state.queue.length,0);
  assert.equal((await send({type:'resume',project:'demo'})).ok,true);
  assert.equal(JSON.parse(requests[1].options.body).task,'');
  const before=requests.length;
  for(const task of ['x'.repeat(513),42,false,['repair']]) assert.equal((await send({type:'resume',project:'demo',task})).ok,false);
  assert.equal(requests.length,before);
  assert.equal((await send({type:'resume',project:'demo',task:'x'.repeat(512)})).ok,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(worker)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
