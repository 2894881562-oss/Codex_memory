"""No plaintext staging; malicious backups are validated before final writes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from codex_memory import backup
from codex_memory.models import Conversation, ImportBatch, Message
from codex_memory.service import MemoryService


class BackupHardeningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="codex-memory-backup-test-"))
        self.service = MemoryService(self.root / "vault")
        self.service._ingest(
            ImportBatch(
                [Conversation("thread", "chatgpt", "plus-a", "记忆", [Message("turn", "user", "本地继续")])],
                [("fixture.json", b'{"text":"raw original"}', "application/json")],
            ),
            "chatgpt",
            "plus-a",
        )

    def backup_contents(self):
        path = self.root / "good.zip"
        if not path.exists():
            self.service.backup(path)
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def rewrite_archive(self, contents, name="changed.zip"):
        manifest = json.loads(contents["manifest.json"])
        manifest["sha256"] = {
            path: hashlib.sha256(raw).hexdigest() for path, raw in contents.items() if path != "manifest.json"
        }
        contents["manifest.json"] = json.dumps(manifest).encode()
        path = self.root / name
        with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as archive:
            for member, raw in contents.items():
                archive.writestr(member, raw)
        return path

    def mutate_database(self, sql):
        contents = self.backup_contents()
        con = sqlite3.connect(":memory:")
        try:
            con.deserialize(contents["memory.sqlite3"])
            con.executescript(sql)
            contents["memory.sqlite3"] = con.serialize()
        finally:
            con.close()
        return contents

    def test_normal_backup_and_restore_never_create_plaintext_staging(self):
        target = self.root / "backup.zip"
        destination = self.root / "restored"
        with (
            patch("tempfile.mkdtemp", side_effect=AssertionError("no temporary directories")),
            patch("tempfile.TemporaryDirectory", side_effect=AssertionError("no temporary directories")),
            patch("tempfile.NamedTemporaryFile", side_effect=AssertionError("no temporary files")),
        ):
            result = self.service.backup(target)
            restored = MemoryService(destination).restore(target)
        self.assertEqual(result["staging"], "memory")
        self.assertEqual(restored["staging"], "memory")
        self.assertNotIn("staging_path", result)
        self.assertFalse((self.service.data_dir / "backup-staging").exists())
        self.assertTrue(MemoryService(destination).audit()["ok"])
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read("memory.sqlite3")[18:20], b"\x01\x01")

    def test_encrypted_output_and_failed_restore_do_not_leave_plaintext(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("optional cryptography unavailable")
        path = self.root / "encrypted.cmbackup"
        destination = self.root / "restored"
        with patch("tempfile.mkdtemp", side_effect=AssertionError("no temporary directories")):
            self.service.backup(path, password="strong-test-password")
            with self.assertRaisesRegex(ValueError, "密码错误"):
                MemoryService(destination).restore(path, password="wrong-test-password")
            self.assertFalse(destination.exists())
            MemoryService(destination).restore(path, password="strong-test-password")
        self.assertTrue(path.read_bytes().startswith(backup.MAGIC))
        self.assertFalse((self.service.data_dir / "backup-staging").exists())
        self.assertFalse(any(file.suffix == ".zip" for file in self.root.iterdir() if file.is_file()))
        self.assertTrue(MemoryService(destination).audit()["ok"])

    def test_online_snapshot_contains_uncheckpointed_wal_transaction(self):
        con = sqlite3.connect(self.service.db_path)
        try:
            con.execute("PRAGMA wal_autocheckpoint=0")
            con.execute("UPDATE messages SET text='committed in WAL'")
            con.commit()
            self.assertTrue(Path(str(self.service.db_path) + "-wal").exists())
            target = self.root / "wal.zip"
            self.service.backup(target)
        finally:
            con.close()
        destination = MemoryService(self.root / "restored")
        destination.restore(target)
        cid = destination.list_conversations()[0]["id"]
        self.assertEqual(destination.fetch_conversation(cid)["messages"][0]["text"], "committed in WAL")

    def test_sqlite_snapshot_memory_cap_fails_before_target_creation(self):
        target = self.root / "too-large.zip"
        with patch.object(backup, "MAX_SQLITE_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "SQLite 快照"):
                self.service.backup(target)
        self.assertFalse(target.exists())
        self.assertFalse((self.service.data_dir / "backup-staging").exists())

    def test_encrypted_zip_memory_cap_fails_before_target_creation(self):
        target = self.root / "too-large.cmbackup"
        with patch.object(backup, "MAX_ENCRYPTED_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "加密 ZIP"):
                self.service.backup(target, password="strong-test-password")
        self.assertFalse(target.exists())

    def test_restore_sqlite_size_and_total_bounds_before_destination_creation(self):
        contents = self.backup_contents()
        path = self.root / "good.zip"
        destination = self.root / "restored"
        with patch.object(backup, "MAX_SQLITE_BYTES", len(contents["memory.sqlite3"]) - 1):
            with self.assertRaisesRegex(ValueError, "单文件过大"):
                MemoryService(destination).restore(path)
        with patch.object(backup, "MAX_RESTORE_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "大小或文件数"):
                MemoryService(destination).restore(path)
        self.assertFalse(destination.exists())

    def test_existing_backup_and_existing_vault_never_overwritten(self):
        target = self.root / "existing.zip"
        target.write_bytes(b"user bytes")
        with self.assertRaises(FileExistsError):
            self.service.backup(target)
        self.assertEqual(target.read_bytes(), b"user bytes")
        self.backup_contents()
        destination = self.root / "existing-vault"
        destination.mkdir()
        (destination / "user.txt").write_bytes(b"retained")
        with self.assertRaises(ValueError):
            MemoryService(destination).restore(self.root / "good.zip")
        self.assertEqual((destination / "user.txt").read_bytes(), b"retained")

    def test_hash_mismatch_rejected_before_any_final_plaintext_writes(self):
        contents = self.backup_contents()
        contents["memory.sqlite3"] = b"not the hashed original"
        path = self.root / "bad-hash.zip"
        with zipfile.ZipFile(path, "x") as archive:
            for name, raw in contents.items():
                archive.writestr(name, raw)
        destination = self.root / "restored"
        with self.assertRaisesRegex(ValueError, "校验失败"):
            MemoryService(destination).restore(path)
        self.assertFalse(destination.exists())

    def test_valid_manifest_with_corrupt_sqlite_is_rejected(self):
        contents = self.backup_contents()
        contents["memory.sqlite3"] = b"garbage data"
        path = self.rewrite_archive(contents)
        destination = self.root / "restored"
        with self.assertRaisesRegex(ValueError, "SQLite"):
            MemoryService(destination).restore(path)
        self.assertFalse(destination.exists())

    def test_malicious_view_and_trigger_are_rejected_without_execution(self):
        for sql, name in (
            (
                "DROP TABLE schema_info; CREATE VIEW schema_info AS SELECT load_extension('/never-load-me') AS version;",
                "view.zip",
            ),
            (
                "CREATE TRIGGER hostile AFTER INSERT ON messages BEGIN SELECT load_extension('/never-load-me'); END;",
                "trigger.zip",
            ),
        ):
            with self.subTest(name=name):
                path = self.rewrite_archive(self.mutate_database(sql), name)
                destination = self.root / name.replace(".zip", "-restored")
                with self.assertRaisesRegex(ValueError, "视图或触发器"):
                    MemoryService(destination).restore(path)
                self.assertFalse(destination.exists())

    def test_validation_sets_trusted_schema_off_and_query_only_before_deserialize(self):
        contents = self.backup_contents()
        calls = []
        real_connect = sqlite3.connect

        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                calls.append(sql)
                return super().execute(sql, *args, **kwargs)

            def deserialize(self, raw, **kwargs):
                calls.append("DESERIALIZE")
                return super().deserialize(raw, **kwargs)

        def connect(*args, **kwargs):
            kwargs["factory"] = ObservedConnection
            return real_connect(*args, **kwargs)

        with (
            zipfile.ZipFile(self.root / "good.zip") as archive,
            patch.object(backup.sqlite3, "connect", side_effect=connect),
        ):
            manifest = json.loads(contents["manifest.json"])
            backup._validate_sqlite(contents["memory.sqlite3"], manifest["sha256"], archive)
        index = calls.index("DESERIALIZE")
        self.assertIn("PRAGMA trusted_schema=OFF", calls[:index])
        self.assertIn("PRAGMA query_only=ON", calls[:index])

    def test_malformed_manifest_hash_types_rejected_as_valueerror(self):
        contents = self.backup_contents()
        manifest = json.loads(contents["manifest.json"])
        manifest["sha256"]["memory.sqlite3"] = 123
        contents["manifest.json"] = json.dumps(manifest).encode()
        path = self.root / "bad-manifest.zip"
        with zipfile.ZipFile(path, "x") as archive:
            for name, raw in contents.items():
                archive.writestr(name, raw)
        destination = self.root / "restored"
        with self.assertRaisesRegex(ValueError, "哈希无效"):
            MemoryService(destination).restore(path)
        self.assertFalse(destination.exists())

    def test_sqlite_missing_tables_and_manifest_blob_mismatch_rejected(self):
        contents = self.mutate_database("DROP TABLE sources;")
        path = self.rewrite_archive(contents, "missing-table.zip")
        with self.assertRaisesRegex(ValueError, "必需的数据表"):
            MemoryService(self.root / "restored").restore(path)
        contents = self.mutate_database("UPDATE artifacts SET size=size+1;")
        path = self.rewrite_archive(contents, "mismatch.zip")
        with self.assertRaisesRegex(ValueError, "清单不一致"):
            MemoryService(self.root / "other-restored").restore(path)

    def test_sqlite_untrusted_table_definition_is_rejected(self):
        contents = self.mutate_database("ALTER TABLE messages ADD COLUMN untrusted TEXT DEFAULT 'injected';")
        path = self.rewrite_archive(contents, "untrusted-schema.zip")
        destination = self.root / "restored"
        with self.assertRaisesRegex(ValueError, "表结构与可信版本不一致"):
            MemoryService(destination).restore(path)
        self.assertFalse(destination.exists())

    def test_blob_size_limit_is_separate_from_sqlite_limit(self):
        target = self.root / "too-large-blob.zip"
        with patch.object(backup, "MAX_BLOB_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "归档记录无效"):
                self.service.backup(target)
        self.assertFalse(target.exists())
        self.backup_contents()
        with patch.object(backup, "MAX_BLOB_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "单文件过大"):
                MemoryService(self.root / "restored").restore(self.root / "good.zip")

    def test_zip_symlink_duplicate_and_windows_backslash_rejected(self):
        contents = self.backup_contents()
        for unsafe, name in (
            ("symlink", "link.zip"),
            ("duplicate", "duplicate.zip"),
            ("backslash", "backslash.zip"),
        ):
            with self.subTest(unsafe=unsafe):
                path = self.root / name
                with zipfile.ZipFile(path, "x") as archive:
                    for member, raw in contents.items():
                        archive.writestr(member, raw)
                    entry = zipfile.ZipInfo("extra.txt")
                    if unsafe == "symlink":
                        entry.create_system = 3
                        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                    elif unsafe == "duplicate":
                        entry.filename = "MEMORY.SQLITE3"
                    else:
                        entry.filename = "folder\\extra.txt"
                    archive.writestr(entry, b"unsafe")
                destination = self.root / name.replace(".zip", "-restored")
                with self.assertRaises(ValueError):
                    MemoryService(destination).restore(path)
                self.assertFalse(destination.exists())

    def test_restored_sources_are_history_only_and_never_trigger_automatic_scans(self):
        project = self.root / "original-project"
        project.mkdir()
        (project / "work.txt").write_text("local project", encoding="utf-8")
        codex = self.root / "original-codex"
        (codex / "sessions").mkdir(parents=True)
        chatgpt = self.root / "conversations.json"
        chatgpt.write_text("[]", encoding="utf-8")
        for kind, path in (("project", project), ("codex", codex), ("chatgpt", chatgpt)):
            self.service.configure_source(kind, path, "plus-a", "retained-project-label")
        self.backup_contents()
        restored = MemoryService(self.root / "restored")
        result = restored.restore(self.root / "good.zip")
        self.assertEqual(result["sources_disabled"], 3)
        self.assertIn("重新添加", result["sources_note"])
        source_rows = restored.list_sources()
        self.assertEqual({row["path"] for row in source_rows}, {str(project), str(codex), str(chatgpt)})
        for row in source_rows:
            self.assertEqual(row["last_report"]["status"], "needs_confirmation")
            self.assertTrue(row["last_report"]["restored"])
            self.assertEqual(row["last_run"], "")
            self.assertEqual(row["account"], "plus-a")
            self.assertEqual(row["project"], "retained-project-label")
        with (
            patch.object(
                restored, "_source_signature", side_effect=AssertionError("must not inspect restored paths")
            ) as signature,
            patch.object(
                restored, "archive_project", side_effect=AssertionError("must not scan projects")
            ) as project_scan,
            patch(
                "codex_memory.importers.parse_codex", side_effect=AssertionError("must not import Codex")
            ) as codex_import,
            patch(
                "codex_memory.importers.parse_chatgpt", side_effect=AssertionError("must not import ChatGPT")
            ) as chatgpt_import,
        ):
            reports = restored.sync_sources()
        self.assertEqual([report["status"] for report in reports], ["needs_confirmation"] * 3)
        for method in (signature, project_scan, codex_import, chatgpt_import):
            method.assert_not_called()
        self.assertEqual(restored.stats()["messages"], 1)
        self.assertTrue(restored.audit()["ok"])

    def test_explicit_readding_restored_source_enables_operator_authorized_scan(self):
        project = self.root / "original-project"
        project.mkdir()
        (project / "work.txt").write_text("local project", encoding="utf-8")
        original = self.service.configure_source("project", project, "plus-a", "selected-project")
        self.backup_contents()
        restored = MemoryService(self.root / "restored")
        restored.restore(self.root / "good.zip")
        confirmed = restored.configure_source("project", project, "plus-a", "selected-project")
        self.assertEqual(confirmed["id"], original["id"])
        self.assertEqual(confirmed["last_report"], {})
        with patch.object(restored, "archive_project", wraps=restored.archive_project) as scan:
            reports = restored.sync_sources()
        scan.assert_called_once()
        self.assertEqual(reports[0]["status"], "ok")
        self.assertEqual(restored.fetch_project_file("selected-project", "work.txt")["text"], "local project")


if __name__ == "__main__":
    unittest.main()
