from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codex_memory.models import Conversation, ImportBatch, Message
from codex_memory.security import ensure_collector_token, redact, safe_relative
from codex_memory.service import MemoryService, import_report_metadata


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cm-core-test-"))
        self.service = MemoryService(self.root / "vault")

    def seed(self, account="plus-a", text="继续电路设计 Tao 项目", mid="m1"):
        thread = Conversation(
            "chat-1",
            "chatgpt",
            account,
            "电路设计",
            [Message(mid, "user", text)],
            updated_at="2026-10-04T01:00:00Z",
            project="Tao",
        )
        return self.service._ingest(
            ImportBatch(
                [thread], [("conversations.json", json.dumps({"text": text}).encode(), "application/json")]
            ),
            "chatgpt",
            account,
        )

    def test_lazy_initialization_and_idempotent_import(self):
        self.assertFalse(self.service.data_dir.exists())
        a = self.seed()
        b = self.seed()
        self.assertEqual(a["conversations"], 1)
        self.assertEqual(b["messages"], 0)
        self.assertEqual(b["unchanged"], 1)
        self.assertEqual(b["conversations_seen"], 1)
        self.assertEqual(b["messages_seen"], 1)
        self.assertEqual(self.service.stats()["messages"], 1)
        self.assertTrue(self.service.audit()["ok"])

    def test_import_diagnostics_are_bounded_redacted_and_count_omissions(self):
        secret = "sk-proj-" + "A" * 40
        batch = ImportBatch(
            warnings=[f"problem {i} {secret}" for i in range(500)],
            warnings_omitted=7,
            coverage={"status": "complete", "chat_files_found": 1, "chat_files_parsed": 1},
        )
        result = import_report_metadata(batch)
        self.assertEqual(len(result["warnings"]), 200)
        self.assertEqual(result["warnings_omitted"], 308)
        self.assertEqual(sum("Additional warnings omitted" in item for item in result["warnings"]), 1)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["coverage"]["scope"], "provided_export_only")
        self.assertLessEqual(
            len(import_report_metadata(ImportBatch(warnings=["a" * 5000]))["warnings"][0]), 1024
        )

    def test_invalid_diagnostics_fail_before_ingestion(self):
        for coverage in (
            {"status": "cloud_account_complete"},
            {"status": ["complete"]},
            {"status": "complete", "attachments_missing": True},
            {"status": "complete", "references_truncated": -1},
        ):
            with self.subTest(coverage=coverage), self.assertRaises(ValueError):
                self.service._ingest(ImportBatch(coverage=coverage), "chatgpt", "test")
        self.assertFalse(self.service.data_dir.exists())
        for omitted in (True, -1, "1"):
            with self.subTest(omitted=omitted), self.assertRaises(ValueError):
                import_report_metadata(ImportBatch(warnings_omitted=omitted))

    def test_coverage_cannot_claim_complete_with_known_gaps(self):
        for gap in (
            {"chat_files_found": 2, "chat_files_parsed": 1},
            {"attachments_external": 1},
            {"attachments_missing": 1},
            {"references_truncated": 1},
        ):
            with self.subTest(gap=gap):
                report = import_report_metadata(ImportBatch(coverage={"status": "complete", **gap}))
                self.assertEqual(report["coverage"]["status"], "partial")

    def test_stats_distinguish_latest_export_from_live_ingestion(self):
        empty = self.service.stats()["chatgpt_archive"]
        self.assertEqual(empty["conversations"], 0)
        self.assertIsNone(empty["last_export_at"])
        self.assertEqual(empty["coverage"], {})
        self.service._ingest(
            ImportBatch(coverage={"status": "partial", "chat_files_found": 2, "chat_files_parsed": 1}),
            "chatgpt",
            "plus-a",
        )
        first = self.service.stats()["chatgpt_archive"]
        self.service.ingest_live(
            {
                "account": "plus-a",
                "conversation_id": "live-stats",
                "messages": [{"id": "u1", "role": "user", "text": "续接"}],
            }
        )
        second = self.service.stats()["chatgpt_archive"]
        self.assertEqual(second["conversations"], 1)
        self.assertEqual(second["last_export_at"], first["last_export_at"])
        self.assertGreaterEqual(second["last_ingest_at"], second["last_export_at"])
        self.assertEqual(second["coverage"]["status"], "partial")
        self.assertEqual(second["coverage"]["chat_files_parsed"], 1)

    def test_ignored_zip_members_are_not_chat_history_gaps(self):
        source = self.root / "selected-export.zip"
        history = [
            {
                "id": "complete-chat",
                "mapping": {
                    "u1": {
                        "id": "u1",
                        "message": {
                            "id": "u1",
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["确认续接目标"]},
                        },
                    }
                },
            }
        ]
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("conversations.json", json.dumps(history))
            archive.writestr(".DS_Store", "ignored unrelated metadata")
        report = self.service.import_chatgpt(source, "plus-a")
        self.assertEqual(report["conversations_seen"], 1)
        self.assertEqual(report["messages_seen"], 1)
        self.assertGreater(report["skipped"], 0)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["coverage"]["status"], "complete")

    def test_restored_import_diagnostics_do_not_expose_arbitrary_metadata(self):
        self.service.initialize()
        with self.service.store.connect() as con:
            for report in (
                "invalid JSON",
                json.dumps({"coverage": {"status": "SECRET_STATUS", "scope": "provided_export_only"}}),
                json.dumps(
                    {
                        "coverage": {
                            "status": "complete",
                            "scope": "provided_export_only",
                            "chat_files_found": "SECRET_COUNT",
                        }
                    }
                ),
            ):
                con.execute(
                    "INSERT INTO import_runs(source,account,created_at,report) VALUES(?,?,?,?)",
                    ("chatgpt", "test", "2026-10-09", report),
                )
        stats = self.service.stats()
        self.assertEqual(stats["chatgpt_archive"]["coverage"], {"status": "unknown"})
        self.assertNotIn("SECRET", json.dumps(stats))

    def test_cross_account_archive_and_project_continuity(self):
        self.seed("plus-a", "账户 A 决定继续设计")
        self.seed("plus-b", "账户 B 接手继续测试")
        self.service.create_checkpoint(
            "Tao", "完成电路设计", decisions="用双层板", next_steps="测纹波", account="plus-a"
        )
        self.assertEqual(len(self.service.list_conversations()), 2)
        self.assertEqual(len(self.service.list_conversations(account="plus-b")), 1)
        state = self.service.project_state("Tao")
        self.assertEqual(state["latest_checkpoint"]["next_steps"], "测纹波")
        handoff = self.service.handoff(project="Tao")
        self.assertIn("双层板", handoff)
        self.assertIn("plus-b", handoff)

    def test_revisions_and_missing_snapshot_messages_are_retained(self):
        self.seed(text="第一版本")
        self.seed(text="第二版本")
        self.seed(text="另一消息", mid="m2")
        stats = self.service.stats()
        self.assertEqual(stats["messages"], 2)
        self.assertEqual(stats["message_revisions"], 3)
        cid = self.service.list_conversations()[0]["id"]
        data = self.service.fetch_conversation(cid, limit=1)
        self.assertTrue(data["has_more"])
        self.assertEqual(data["messages"][0]["text"], "第二版本")
        self.assertEqual(self.service.fetch_conversation(cid, offset=1)["messages"][0]["text"], "另一消息")

    def test_chinese_short_fulltext_and_sql_literal_queries(self):
        self.seed(text="电路设计 memory.sqlite 100% sample_value")
        for term in ["电路", "电路设计", "memory", "100%", "sample_value"]:
            self.assertEqual(len(self.service.search(term)), 1, term)
        self.assertEqual(self.service.search("' OR 1=1 --"), [])
        self.assertEqual(self.service.search("missing"), [])

    def test_secret_redaction_keeps_original_archive(self):
        token = "sk-proj-" + "A" * 40
        self.seed(text=f"API_KEY={token}")
        cid = self.service.list_conversations()[0]["id"]
        data = self.service.fetch_conversation(cid)
        self.assertNotIn(token, json.dumps(data))
        self.assertEqual(self.service.search(token), [])
        self.assertIn("REDACTED", data["messages"][0]["text"])
        with self.service.store.connect() as con:
            digest = con.execute("SELECT hash FROM artifacts").fetchone()[0]
        self.assertIn(token.encode(), self.service.store.blob_path(digest).read_bytes())

    def test_live_capture_updates_streaming_without_duplicate(self):
        payload = {
            "source": "chatgpt",
            "account": "plus-a",
            "conversation_id": "chat-live",
            "project": "study",
            "messages": [
                {"id": "u1", "role": "user", "text": "解释谐振"},
                {"id": "a1", "role": "assistant", "text": "谐"},
            ],
        }
        self.service.ingest_live(payload)
        payload["messages"][1]["text"] = "谐振是能量交换"
        self.service.ingest_live(payload)
        self.assertEqual(self.service.stats()["messages"], 2)
        self.assertEqual(self.service.stats()["message_revisions"], 3)
        cid = self.service.list_conversations()[0]["id"]
        self.assertEqual(self.service.fetch_conversation(cid)["messages"][-1]["text"], "谐振是能量交换")
        self.assertEqual(self.service.ingest_live(payload)["unchanged"], 2)

    def test_invalid_live_and_limits(self):
        for payload in ({}, {"conversation_id": "c", "messages": []}, {"source": "codex"}):
            with self.assertRaises(ValueError):
                self.service.ingest_live(payload)
        self.seed()
        for limit in (0, 101, True):
            with self.assertRaises(ValueError):
                self.service.search("Tao", limit=limit)
        with self.assertRaises(ValueError):
            self.service.handoff()
        with self.assertRaises(ValueError):
            self.service.fetch_conversation("missing")

    def test_older_export_enriches_live_without_rollback_and_restores_order(self):
        payload = {
            "account": "plus-a",
            "conversation_id": "chat-1",
            "title": "最新标题",
            "project": "Tao",
            "messages": [{"id": "m2", "role": "assistant", "text": "实时最新回答"}],
            "metadata": {"observed_at": "2026-10-04T03:00:00Z"},
        }
        self.service.ingest_live(payload)
        older = Conversation(
            "chat-1",
            "chatgpt",
            "plus-a",
            "旧标题",
            [
                Message("m1", "user", "历史问题", "2026-10-03T01:00:00Z"),
                Message(
                    "m2", "assistant", "旧回答", "2026-10-03T01:01:00Z", parent_id="m1", branch="branch-a"
                ),
            ],
            updated_at="2026-10-03T01:01:00Z",
            project="Tao",
        )
        result = self.service._ingest(ImportBatch([older]), "chatgpt", "plus-a")
        self.assertEqual(result["stale_revisions"], 1)
        cid = self.service.list_conversations()[0]["id"]
        data = self.service.fetch_conversation(cid)
        self.assertEqual(data["conversation"]["title"], "最新标题")
        self.assertEqual([m["external_id"] for m in data["messages"]], ["m1", "m2"])
        self.assertEqual(data["messages"][1]["text"], "实时最新回答")
        self.assertEqual(data["messages"][1]["parent_id"], "m1")
        payload["metadata"]["observed_at"] = "2026-10-04T04:00:00Z"
        self.service.ingest_live(payload)
        self.assertEqual(self.service.fetch_conversation(cid)["messages"][1]["branch"], "branch-a")
        self.assertTrue(self.service.audit()["ok"])

    def test_offline_out_of_order_live_does_not_rollback(self):
        payload = {
            "account": "plus-a",
            "conversation_id": "chat-live",
            "messages": [{"id": "a1", "role": "assistant", "text": "完整回答"}],
            "metadata": {"observed_at": "2026-10-04T05:00:00Z"},
        }
        self.service.ingest_live(payload)
        payload["messages"][0]["text"] = "完整"
        payload["metadata"]["observed_at"] = "2026-10-04T04:00:00Z"
        self.assertEqual(self.service.ingest_live(payload)["stale_revisions"], 1)
        cid = self.service.list_conversations()[0]["id"]
        self.assertEqual(self.service.fetch_conversation(cid)["messages"][0]["text"], "完整回答")

    def test_codex_partial_file_retries_without_rescanning_healthy_files(self):
        root = self.root / "codex" / "sessions"
        root.mkdir(parents=True)
        good = {
            "type": "session_meta",
            "payload": {"id": "good", "cwd": str(self.root), "timestamp": "2026-10-04T05:00:00Z"},
        }
        (root / "good.jsonl").write_text(json.dumps(good) + "\n", encoding="utf-8")
        (root / "partial.jsonl").write_text(
            json.dumps({**good, "payload": {**good["payload"], "id": "partial"}}) + '\n{"type":',
            encoding="utf-8",
        )
        self.service.configure_source("codex", root.parent, "plus-a")
        first = self.service.sync_sources()[0]
        self.assertEqual(first["files_scanned"], 2)
        self.assertEqual(first["status"], "partial")
        second = self.service.sync_sources()[0]
        self.assertEqual(second["files_scanned"], 1)
        with (root / "partial.jsonl").open("a", encoding="utf-8") as stream:
            stream.write('"unknown", "payload": {}}\n')
        self.assertEqual(self.service.sync_sources()[0]["status"], "ok")
        self.assertEqual(self.service.sync_sources()[0]["status"], "unchanged")

    def test_codex_append_saves_delta_and_keeps_pair_identity(self):
        root = self.root / "codex" / "sessions"
        root.mkdir(parents=True)
        meta = {"type": "session_meta", "payload": {"id": "append-session", "cwd": str(self.root)}}
        event = {
            "type": "event_msg",
            "timestamp": "2026-10-04T05:00:00Z",
            "payload": {"type": "user_message", "message": "继续工作"},
        }
        file = root / "append.jsonl"
        baseline = (json.dumps(meta) + "\n" + json.dumps(event) + "\n").encode()
        file.write_bytes(baseline)
        self.service.configure_source("codex", root.parent, "plus-a")
        self.service.sync_sources()
        response = {
            "type": "response_item",
            "timestamp": "2026-10-04T05:00:01Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "继续工作"}],
            },
        }
        extra = (json.dumps(response) + "\n").encode()
        with file.open("ab") as stream:
            stream.write(extra)
        report = self.service.sync_sources()[0]
        self.assertEqual(report["append_files"], 1)
        self.assertEqual(self.service.stats()["messages"], 1)
        with self.service.store.connect() as con:
            raw_sizes = [r[0] for r in con.execute("SELECT size FROM artifacts")]
        self.assertNotIn(len(baseline) + len(extra), raw_sizes)
        self.assertIn(len(extra), raw_sizes)
        self.assertTrue(self.service.audit()["ok"])
        self.assertEqual(self.service.sync_sources()[0]["status"], "unchanged")

    def test_handoff_budget_and_exclusive_export(self):
        self.seed(text="长消息" * 10000)
        text = self.service.handoff(project="Tao", max_chars=1000)
        self.assertLessEqual(len(text), 1000)
        target = self.root / "handoff.md"
        self.service.export_handoff(target, project="Tao")
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            self.service.export_handoff(target, project="Tao")
        self.assertEqual(target.read_bytes(), before)

    def test_message_slices_are_bounded_and_redacted(self):
        secret = "sk-proj-" + "A" * 40
        self.seed(text="长消息" * 10000 + secret)
        cid = self.service.list_conversations()[0]["id"]
        mid = self.service.fetch_conversation(cid, limit=1)["messages"][0]["id"]
        first = self.service.fetch_message(mid, max_chars=30)
        self.assertEqual(len(first["text"]), 30)
        self.assertTrue(first["has_more"])
        self.assertGreater(first["total_chars"], 30000)
        last = self.service.fetch_message(mid, offset=30000)
        self.assertNotIn(secret, last["text"])
        self.assertFalse(last["has_more"])
        with self.assertRaises(ValueError):
            self.service.fetch_message("missing")
        with self.assertRaises(ValueError):
            self.service.fetch_message(mid, max_chars=100001)

    def test_project_snapshots_versions_secrets_and_binary(self):
        project = self.root / "project"
        project.mkdir()
        (project / "main.py").write_text("print('第一版')", encoding="utf-8")
        (project / ".env").write_text("SECRET=do-not-archive", encoding="utf-8")
        (project / "auth.json").write_text('{"token":"hidden"}', encoding="utf-8")
        (project / "image.bin").write_bytes(b"\x00\xff")
        a = self.service.archive_project(project, project="code")
        self.assertEqual(a["files"], 2)
        self.assertEqual(a["skipped"], 2)
        self.assertIsNone(self.service.fetch_project_file("code", "image.bin")["text"])
        (project / "main.py").write_text("print('第二版')", encoding="utf-8")
        self.service.archive_project(project, project="code")
        self.assertIn("第二版", self.service.fetch_project_file("code", "main.py")["text"])
        self.assertEqual(self.service.stats()["project_file_revisions"], 3)
        self.assertTrue(self.service.audit()["ok"])
        with self.assertRaises(ValueError):
            self.service.fetch_project_file("code", "../auth.json")

    def test_source_sync_and_change_detection(self):
        project = self.root / "project"
        project.mkdir()
        file = project / "hello.txt"
        file.write_text("a", encoding="utf-8")
        self.service.configure_source("project", project, project="watch")
        self.assertEqual(self.service.sync_sources()[0]["status"], "ok")
        self.assertEqual(self.service.sync_sources()[0]["status"], "unchanged")
        file.write_text("changed", encoding="utf-8")
        self.assertEqual(self.service.sync_sources()[0]["status"], "ok")
        self.assertEqual(self.service.fetch_project_file("watch", "hello.txt")["text"], "changed")

    def test_portable_consistent_backup_restore_no_token(self):
        self.seed()
        self.service.create_checkpoint("Tao", "继续工作")
        ensure_collector_token(self.service.data_dir)
        path = self.root / "backup.zip"
        result = self.service.backup(path)
        self.assertFalse(result["encrypted"])
        with zipfile.ZipFile(path) as archive:
            self.assertNotIn("local-state/collector.token", archive.namelist())
        restored = MemoryService(self.root / "restored")
        restored.restore(path)
        self.assertEqual(restored.stats()["messages"], 1)
        self.assertTrue(restored.audit()["ok"])
        with self.assertRaises(ValueError):
            restored.restore(path)
        with self.assertRaises(FileExistsError):
            self.service.backup(path)

    def test_encrypted_backup_wrong_password_and_restore(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("optional cryptography is not installed")
        self.seed()
        path = self.root / "backup.cmbackup"
        self.service.backup(path, password="correct-test-password")
        target = MemoryService(self.root / "restored")
        with self.assertRaises(ValueError):
            target.restore(path, password="wrong-test-password")
        self.assertFalse(target.data_dir.exists())
        target.restore(path, password="correct-test-password")
        self.assertTrue(target.audit()["ok"])

    def test_backup_path_traversal_rejected_before_restore(self):
        path = self.root / "bad.zip"
        with zipfile.ZipFile(path, "x") as archive:
            archive.writestr("../escape", "bad")
            archive.writestr("manifest.json", "{}")
        target = MemoryService(self.root / "restored")
        with self.assertRaises(ValueError):
            target.restore(path)
        self.assertFalse(target.data_dir.exists())

    def test_blob_corruption_detected(self):
        self.seed()
        with self.service.store.connect() as con:
            digest = con.execute("SELECT hash FROM artifacts").fetchone()[0]
        self.service.store.blob_path(digest).write_bytes(b"changed")
        self.assertFalse(self.service.audit()["ok"])
        with self.assertRaises(ValueError):
            self.service.backup(self.root / "bad.zip")

    def test_concurrent_checkpoints(self):
        self.service.initialize()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.service.create_checkpoint("project", f"goal {i}"), range(12)))
        self.assertEqual(self.service.stats()["checkpoints"], 12)
        self.assertTrue(self.service.audit()["ok"])

    def test_unsupported_schema_is_not_modified(self):
        self.service.data_dir.mkdir()
        with sqlite3.connect(self.service.db_path) as con:
            con.execute("CREATE TABLE schema_info(version INTEGER)")
            con.execute("INSERT INTO schema_info VALUES(999)")
        with self.assertRaises(ValueError):
            self.service.initialize()

    def test_token_and_path_security(self):
        a = ensure_collector_token(self.root / "token-dir")
        self.assertEqual(a, ensure_collector_token(self.root / "token-dir"))
        self.assertGreater(len(a), 40)
        for path in (
            "../a",
            "C:/x",
            "/a",
            "a\\..\\b",
            "a//b",
            "a/NUL.txt",
            "a/trailing.",
            "a/stream:name",
            "a/*.txt",
        ):
            with self.assertRaises(ValueError):
                safe_relative(path)
        self.assertIn("REDACTED", redact("Authorization: Bearer secret-access-token"))


if __name__ == "__main__":
    unittest.main()
