"""Importer integrity and coverage tests; temporary directories are retained."""

import json
import stat
import tempfile
import unittest
import zipfile
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from codex_memory import importers


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="codex-memory-import-test-"))

    def json_file(self, value, name="conversations.json"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        return path

    def session_file(self, records, name="rollout.jsonl", trailing=b""):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = (
            b"\n".join(json.dumps(record, ensure_ascii=False).encode("utf-8") for record in records)
            + b"\n"
            + trailing
        )
        path.write_bytes(blob)
        return path, blob

    @staticmethod
    def conversation():
        return {
            "id": "chat-id",
            "title": "项目记忆",
            "create_time": 1,
            "update_time": 5,
            "current_node": "branch-b",
            "mapping": {
                "root": {"parent": None, "children": ["user-node"], "message": None},
                "user-node": {
                    "parent": "root",
                    "children": ["branch-a", "branch-b"],
                    "message": {
                        "id": "user-id",
                        "author": {"role": "user"},
                        "create_time": 2,
                        "content": {"content_type": "text", "parts": ["继续项目"]},
                    },
                },
                "branch-b": {
                    "parent": "user-node",
                    "message": {
                        "id": "answer-b",
                        "author": {"role": "assistant"},
                        "create_time": 4,
                        "content": {"content_type": "text", "parts": ["方案 B"]},
                    },
                },
                "branch-a": {
                    "parent": "user-node",
                    "message": {
                        "id": "answer-a",
                        "author": {"role": "assistant"},
                        "create_time": 3,
                        "content": {"content_type": "text", "parts": ["方案 A"]},
                    },
                },
            },
        }

    def test_chatgpt_preserves_all_branches_and_byte_snapshot(self):
        path = self.json_file([self.conversation()])
        original = path.read_bytes()
        batch = importers.parse_chatgpt(path, "plus-b", "tao")
        conv = batch.conversations[0]
        self.assertEqual(conv.id, "chat-id")
        self.assertEqual((conv.account, conv.project, conv.title), ("plus-b", "tao", "项目记忆"))
        self.assertEqual([m.id for m in conv.messages], ["user-id", "answer-a", "answer-b"])
        self.assertEqual(conv.messages[1].parent_id, "user-id")
        self.assertEqual(conv.messages[1].branch, "branch-a")
        self.assertTrue(conv.messages[2].metadata["current_node"])
        self.assertEqual(batch.artifacts[0][1], original)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(batch.warnings, [])
        self.assertEqual(batch.warnings_omitted, 0)
        self.assertEqual(batch.coverage["status"], "complete")
        self.assertEqual(batch.coverage["scope"], "provided_export_only")

    def test_chatgpt_tool_and_multimodal_references_are_truthful(self):
        conv = self.conversation()
        conv["mapping"]["image"] = {
            "parent": "branch-b",
            "message": {
                "id": "image-id",
                "author": {"role": "user"},
                "create_time": 5,
                "content": {
                    "content_type": "multimodal_text",
                    "parts": [
                        "看这张图",
                        {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-img"},
                    ],
                },
            },
        }
        conv["mapping"]["tool"] = {
            "parent": "image",
            "message": {
                "id": "tool-id",
                "author": {"role": "tool", "name": "python"},
                "create_time": 6,
                "content": {"content_type": "text", "parts": ["{'rows': 7}"]},
            },
        }
        path = self.json_file([conv])
        batch = importers.parse_chatgpt(path, "a")
        image = batch.conversations[0].messages[-2]
        self.assertIn("看这张图", image.text)
        self.assertIn("image_asset_pointer reference", image.text)
        self.assertNotIn("described", image.text)
        self.assertEqual(batch.conversations[0].messages[-1].role, "tool")
        self.assertTrue(any("no matching local bytes" in warning for warning in batch.warnings))

    def test_zip_attachments_are_archived_and_sensitive_members_never_read(self):
        path = self.root / "export.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("export/conversations.json", json.dumps([self.conversation()]))
            archive.writestr("export/file-photo.png", b"PNG fixture")
            archive.writestr("export/attachments/file-code.py", b"print('data, not execution')")
            archive.writestr("export/auth.json", b"CREDENTIAL_DO_NOT_IMPORT")
            archive.writestr("export/secrets.txt", b"CREDENTIAL_DO_NOT_IMPORT")
            archive.writestr("export/browser/cookies.json", b"CREDENTIAL_DO_NOT_IMPORT")
            archive.writestr("export/user.json", b"PROFILE_NOT_ALLOWLISTED")
        batch = importers.parse_chatgpt(path, "a")
        self.assertEqual(len(batch.conversations), 1)
        self.assertEqual(len(batch.artifacts), 3)
        self.assertTrue(any(name.endswith("file-code.py") for name, _, _ in batch.artifacts))
        self.assertFalse(any(b"CREDENTIAL" in blob for _, blob, _ in batch.artifacts))
        self.assertFalse(any(name.endswith(".zip") for name, _, _ in batch.artifacts))

    def test_zip_traversal_absolute_symlink_and_duplicates_are_skipped(self):
        path = self.root / "unsafe.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("conversations.json", json.dumps([self.conversation()]))
            for name in ("../outside.txt", "/absolute.txt", "C:/outside.txt"):
                archive.writestr(name, "bad")
            backslash = zipfile.ZipInfo("placeholder.txt")
            backslash.filename = "folder\\outside.txt"
            archive.writestr(backslash, "bad")
            symlink = zipfile.ZipInfo("attachments/link.txt")
            symlink.create_system = 3
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(symlink, "../../secret")
            archive.writestr("Conversations.json", "[]")
        batch = importers.parse_chatgpt(path, "a")
        self.assertEqual(len(batch.artifacts), 1)
        self.assertEqual(len(batch.conversations), 1)
        self.assertGreaterEqual(batch.skipped, 6)
        self.assertFalse((self.root.parent / "outside.txt").exists())

    def test_zip_bomb_bounds_and_file_count_bounds(self):
        path = self.root / "large.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("conversations.json", json.dumps([self.conversation()]))
            archive.writestr("file-big.txt", "0" * 10000)
        with patch.object(importers, "MAX_TOTAL_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "uncompressed total"):
                importers.parse_chatgpt(path, "a")
        with patch.object(importers, "MAX_ARCHIVE_ENTRIES", 1):
            with self.assertRaisesRegex(ValueError, "entry count"):
                importers.parse_chatgpt(path, "a")
        with patch.object(importers, "MAX_COMPRESSION_RATIO", 100):
            batch = importers.parse_chatgpt(path, "a")
        self.assertEqual(len(batch.conversations), 1)
        self.assertFalse(any(name.endswith("file-big.txt") for name, _, _ in batch.artifacts))

    def test_directory_is_allowlisted(self):
        self.json_file([self.conversation()])
        self.json_file({"secret": "NEVER"}, "auth.json")
        self.json_file({"secret": "NEVER"}, "browser/cookies.json")
        (self.root / "file-photo.png").write_bytes(b"fixture")
        batch = importers.parse_chatgpt(self.root, "a")
        self.assertEqual(len(batch.artifacts), 2)
        self.assertEqual(len(batch.conversations), 1)
        self.assertFalse(any(b"NEVER" in blob for _, blob, _ in batch.artifacts))

    def test_chatgpt_shards_and_attachment_associations_in_zip_and_directory(self):
        records = {}
        for number, filename in (
            (10, "conversations_10.json"),
            (2, "conversations_2.json"),
            (1, "conversations-1.json"),
            (0, "conversations.json"),
        ):
            conv = self.conversation()
            conv["id"] = f"chat-{number}"
            message = conv["mapping"]["user-node"]["message"]
            if number == 0:
                message["content"]["parts"].append({"asset_pointer": "file-service://file-photo"})
                message["metadata"] = {
                    "attachments": [{"id": "upload-opaque", "name": "attachments/report.pdf"}]
                }
            elif number == 1:
                message["content"]["parts"].append({"file_url": "attachments/a/duplicate.pdf"})
            elif number == 2:
                message["content"]["parts"].append({"file_url": "duplicate.pdf"})
            else:
                message["content"]["parts"].extend(
                    [
                        {"image_url": "https://example.invalid/file-photo.png"},
                        {"asset_pointer": "file-service://file-missing"},
                    ]
                )
            records[f"bundle/{filename}"] = json.dumps(
                {"conversations": [conv]} if number == 2 else conv if number == 10 else [conv]
            ).encode()
        records.update(
            {
                "bundle/file-photo.png": b"PNG fixture",
                "bundle/attachments/report.pdf": b"PDF fixture",
                "bundle/attachments/a/duplicate.pdf": b"first duplicate",
                "bundle/attachments/b/duplicate.pdf": b"second duplicate",
                # Even attachment JSON with a chat-like shape is not a shard.
                "bundle/attachments/other.json": json.dumps([self.conversation()]).encode(),
                "bundle/conversations-final.json": json.dumps([self.conversation()]).encode(),
                "bundle/conversations-1-extra.json": json.dumps([self.conversation()]).encode(),
                "bundle/auth.json": b"SYNTHETIC_CREDENTIAL_NOT_READ",
            }
        )
        batches = []
        for kind in ("zip", "directory"):
            with self.subTest(kind=kind):
                if kind == "zip":
                    source = self.root / "sharded.zip"
                    with zipfile.ZipFile(source, "w") as archive:
                        for name, blob in records.items():
                            archive.writestr(name, blob)
                else:
                    source = self.root / "directory-export"
                    for name, blob in records.items():
                        destination = source / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(blob)
                batch = importers.parse_chatgpt(source, "plus-a", "project")
                batches.append(batch)
                self.assertEqual(
                    [conv.id for conv in batch.conversations], ["chat-0", "chat-1", "chat-2", "chat-10"]
                )
                self.assertEqual(batch.coverage["scope"], "provided_export_only")
                self.assertEqual(batch.coverage["status"], "partial")
                self.assertEqual(batch.coverage["chat_files_found"], 4)
                self.assertEqual(batch.coverage["chat_files_parsed"], 4)
                self.assertEqual(batch.coverage["chat_files_unsupported"], 0)
                self.assertEqual(batch.coverage["attachments_available"], 5)
                self.assertEqual(batch.coverage["attachments_matched"], 3)
                self.assertEqual(batch.coverage["attachments_missing"], 1)
                self.assertEqual(batch.coverage["attachments_ambiguous"], 1)
                self.assertEqual(batch.coverage["attachments_external"], 1)
                links = {
                    link["reference"]: link
                    for conv in batch.conversations
                    for message in conv.messages
                    for link in message.metadata["attachment_links"]
                }
                photo = links["file-service://file-photo"]
                self.assertEqual(photo["artifact"], "chatgpt/bundle/file-photo.png")
                self.assertEqual(photo["sha256"], sha256(b"PNG fixture").hexdigest())
                self.assertEqual(links["upload-opaque"]["artifact"], "chatgpt/bundle/attachments/report.pdf")
                qualified = links["attachments/a/duplicate.pdf"]
                self.assertEqual(qualified["artifact"], "chatgpt/bundle/attachments/a/duplicate.pdf")
                for reference, status in (
                    ("duplicate.pdf", "ambiguous"),
                    ("file-service://file-missing", "missing"),
                    ("https://example.invalid/file-photo.png", "external"),
                ):
                    self.assertEqual(links[reference]["status"], status)
                    self.assertNotIn("artifact", links[reference])
                    self.assertNotIn("sha256", links[reference])
                self.assertTrue(any("match multiple" in warning for warning in batch.warnings))
                self.assertFalse(any("conversations-final.json" in name for name, _, _ in batch.artifacts))
                for name, blob, _ in batch.artifacts:
                    self.assertEqual(blob, records[name.removeprefix("chatgpt/")])
        self.assertEqual(batches[0].coverage, batches[1].coverage)

    def test_shard_order_is_natural_and_independent_of_zip_member_order(self):
        members = []
        for filename in (
            "conversations_10.json",
            "conversations-2.json",
            "conversations_01.json",
            "conversations-1.json",
            "conversations.json",
        ):
            conv = self.conversation()
            conv["id"] = filename
            members.append((filename, json.dumps([conv])))
        expected = [
            "conversations.json",
            "conversations-1.json",
            "conversations_01.json",
            "conversations-2.json",
            "conversations_10.json",
        ]
        for index, entries in enumerate((members, list(reversed(members)))):
            source = self.root / f"order-{index}.zip"
            with zipfile.ZipFile(source, "w") as archive:
                for name, body in entries:
                    archive.writestr(name, body)
            batch = importers.parse_chatgpt(source, "a")
            self.assertEqual([conv.id for conv in batch.conversations], expected)
            self.assertEqual(batch.coverage["status"], "complete")

    def test_empty_and_unrecognized_chat_bodies_are_not_silent_success(self):
        for index, (value, parsed) in enumerate(
            (
                ([], 1),
                ({"conversations": []}, 1),
                ([{"title": "not a chat", "content": "unrelated data"}], 0),
                ({"mapping": "not an object"}, 0),
                ({"messages": "not a list"}, 0),
            )
        ):
            with self.subTest(value=value):
                source = self.json_file(value, f"empty-{index}.json")
                original = source.read_bytes()
                batch = importers.parse_chatgpt(source, "a")
                self.assertEqual(batch.conversations, [])
                self.assertEqual(batch.artifacts[0][1], original)
                self.assertEqual(batch.coverage["status"], "no_chat_history")
                self.assertEqual(batch.coverage["chat_files_found"], 1)
                self.assertEqual(batch.coverage["chat_files_parsed"], parsed)
                self.assertEqual(batch.coverage["chat_files_unsupported"], 1 - parsed)
                self.assertTrue(
                    any("No recognizable ChatGPT message history" in warning for warning in batch.warnings)
                )

    def test_directory_with_only_attachments_reports_no_history(self):
        self.json_file([self.conversation()], "attachments/not-history.json")
        (self.root / "photo.png").write_bytes(b"synthetic attachment")
        batch = importers.parse_chatgpt(self.root, "a")
        self.assertEqual(len(batch.artifacts), 2)
        self.assertEqual(batch.conversations, [])
        self.assertEqual(batch.coverage["chat_files_found"], 0)
        self.assertEqual(batch.coverage["attachments_available"], 2)
        self.assertEqual(batch.coverage["status"], "no_chat_history")

    def test_only_explicit_ascii_numeric_history_names_are_selected(self):
        for index, filename in enumerate(
            (
                "conversations-final.json",
                "conversations-1-extra.json",
                "conversations--1.json",
                "conversations-١.json",
                "converſations-1.json",
                "attachments/arbitrary.json",
            )
        ):
            conv = self.conversation()
            conv["id"] = f"unselected-{index}"
            self.json_file([conv], filename)
        selected = self.conversation()
        selected["id"] = "selected"
        self.json_file([selected], "CONVERSATIONS_001.JSON")
        batch = importers.parse_chatgpt(self.root, "a")
        self.assertEqual([conv.id for conv in batch.conversations], ["selected"])
        self.assertEqual(batch.coverage["chat_files_found"], 1)
        self.assertEqual(batch.coverage["status"], "complete")

    def test_archive_root_and_conversation_relative_paths_do_not_hide_ambiguity(self):
        conv = self.conversation()
        conv["mapping"]["user-node"]["message"]["content"]["parts"].append(
            {"file_url": "attachments/report.pdf"}
        )
        self.json_file([conv], "bundle/conversations.json")
        for name in ("attachments/report.pdf", "bundle/attachments/report.pdf"):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(name.encode())
        batch = importers.parse_chatgpt(self.root, "a")
        link = batch.conversations[0].messages[0].metadata["attachment_links"][0]
        self.assertEqual(link["status"], "ambiguous")
        self.assertEqual(link["candidate_count"], 2)
        self.assertNotIn("artifact", link)
        self.assertEqual(batch.coverage["attachments_ambiguous"], 1)
        self.assertEqual(batch.coverage["status"], "partial")

    def test_mixed_supported_and_unknown_records_warn_without_guessing(self):
        source = self.json_file([{"id": "unrelated", "title": "data"}, self.conversation(), None])
        batch = importers.parse_chatgpt(source, "a")
        self.assertEqual([conv.id for conv in batch.conversations], ["chat-id"])
        self.assertEqual(batch.skipped, 2)
        self.assertEqual(batch.coverage["chat_files_parsed"], 1)
        self.assertEqual(batch.coverage["status"], "partial")

    def test_attachment_filename_alias_collision_and_conflicting_name_are_not_guessed(self):
        conv = self.conversation()
        message = conv["mapping"]["user-node"]["message"]
        message["content"]["parts"].extend(
            [
                {"asset_pointer": "file-service://file-photo"},
                {"file_url": "file-photo.png"},
                {"file_url": "wrong-folder/file-photo.png"},
            ]
        )
        message["metadata"] = {
            "attachments": [
                {"id": "file-other", "name": "different.pdf"},
                {"id": ["malformed untrusted ID"], "name": "not used.pdf"},
            ]
        }
        self.json_file([conv])
        for name in ("file-photo.png", "file-photo-duplicate.jpg", "file-other.png", "different.pdf"):
            (self.root / name).write_bytes(name.encode())
        batch = importers.parse_chatgpt(self.root, "a")
        links = {
            link["reference"]: link
            for link in batch.conversations[0].messages[0].metadata["attachment_links"]
        }
        self.assertEqual(links["file-service://file-photo"]["status"], "ambiguous")
        self.assertEqual(links["file-other"]["status"], "ambiguous")
        self.assertEqual(links["file-photo.png"]["status"], "matched")
        self.assertEqual(links["wrong-folder/file-photo.png"]["status"], "missing")
        self.assertEqual(batch.coverage["status"], "partial")

    def test_attachment_links_survive_bounded_message_metadata(self):
        conv = self.conversation()
        message = conv["mapping"]["user-node"]["message"]
        message["metadata"] = {"huge": "汉" * 100000}
        message["content"]["parts"].append({"asset_pointer": "file-service://file-photo"})
        self.json_file([conv])
        (self.root / "file-photo.png").write_bytes(b"exact PNG bytes")
        batch = importers.parse_chatgpt(self.root, "a")
        metadata = batch.conversations[0].messages[0].metadata
        self.assertTrue(metadata["raw_truncated"])
        self.assertEqual(metadata["attachment_links"][0]["artifact"], "chatgpt/file-photo.png")
        self.assertEqual(metadata["attachment_links"][0]["sha256"], sha256(b"exact PNG bytes").hexdigest())
        self.assertLessEqual(len(json.dumps(metadata).encode()), importers.MAX_METADATA_BYTES)
        self.assertEqual(batch.coverage["attachments_matched"], 1)

    def test_warning_limit_counts_omissions_and_keeps_coverage_outside_warnings(self):
        source = self.json_file([{"not-chat": index} for index in range(8)])
        with patch.object(importers, "MAX_WARNINGS", 3):
            batch = importers.parse_chatgpt(source, "a")
        self.assertEqual(len(batch.warnings), 3)
        # Eight record warnings, unsupported file, and no history warning.
        self.assertEqual(batch.warnings_omitted, 7)
        self.assertEqual(batch.coverage["status"], "no_chat_history")
        self.assertEqual(batch.coverage["chat_files_unsupported"], 1)
        long_warning = importers.ImportBatch()
        importers._warn(long_warning, "x" * 5000)
        self.assertEqual(len(long_warning.warnings[0]), importers.MAX_WARNING_CHARS)
        self.assertTrue(long_warning.warnings[0].endswith("[warning truncated]"))
        self.assertEqual(long_warning.warnings_omitted, 0)

    def test_reference_inspection_limit_marks_partial_without_losing_raw(self):
        conv = self.conversation()
        message = conv["mapping"]["user-node"]["message"]
        message["content"]["parts"].extend({"file_id": f"file-{i}"} for i in range(5))
        source = self.json_file([conv])
        original = source.read_bytes()
        with patch.object(importers, "MAX_REFERENCES", 2):
            batch = importers.parse_chatgpt(source, "a")
        self.assertEqual(batch.coverage["references_truncated"], 1)
        self.assertEqual(batch.coverage["attachments_missing"], 2)
        self.assertEqual(batch.coverage["status"], "partial")
        self.assertEqual(batch.artifacts[0][1], original)
        self.assertTrue(any("per-message limit" in warning for warning in batch.warnings))

    def test_attachment_hint_entry_budget_bounds_actual_inspection(self):
        reads = {"id": 0, "name": 0}

        class CountedAttachment(dict):
            def get(self, key, default=None):
                if key in reads:
                    reads[key] += 1
                return super().get(key, default)

        attachments = [CountedAttachment(id="file-repeat", name="repeat.pdf") for _ in range(30)]
        message = {"metadata": {"attachments": attachments}}
        with patch.object(importers, "MAX_REFERENCE_NODES", 5):
            hints, truncated = importers._attachment_hints(message)
        self.assertEqual(hints, {"file-repeat": ("repeat.pdf",)})
        self.assertTrue(truncated)
        # Repeated IDs still consume a budget, not 30 reads or 30*ref-count reads.
        self.assertEqual(reads, {"id": 5, "name": 5})
        with patch.object(importers, "MAX_REFERENCES", 2):
            hints, truncated = importers._attachment_hints(
                {"attachments": [{"id": f"file-{i}", "name": f"name-{i}.pdf"} for i in range(4)]}
            )
        self.assertTrue(truncated)
        self.assertEqual(len(hints), 2)
        with patch.object(importers, "MAX_REFERENCES", 2):
            hints, truncated = importers._attachment_hints(
                {"attachments": [{"id": "same-id", "name": f"name-{i}.pdf"} for i in range(4)]}
            )
        self.assertTrue(truncated)
        self.assertEqual(len(hints["same-id"]), 2)

    def test_hint_index_is_built_once_per_message_and_double_limits_count_once(self):
        conv = self.conversation()
        conv["mapping"]["user-node"]["message"]["metadata"] = {
            "attachments": [{"id": "file-repeat", "name": "repeat.pdf"} for _ in range(60)]
        }
        source = self.json_file([conv])
        with (
            patch.object(importers, "MAX_REFERENCE_NODES", 40),
            patch.object(importers, "_attachment_hints", wraps=importers._attachment_hints) as builder,
        ):
            batch = importers.parse_chatgpt(source, "a")
        self.assertEqual(builder.call_count, 3)
        self.assertEqual(batch.coverage["references_truncated"], 1)
        self.assertEqual(batch.coverage["status"], "partial")
        self.assertEqual(batch.artifacts[0][1], source.read_bytes())
        self.assertEqual(sum("inspection reached" in warning for warning in batch.warnings), 1)

    def test_sensitive_explicit_inputs_rejected(self):
        auth = self.json_file([], "auth.json")
        with self.assertRaises(ValueError):
            importers.parse_chatgpt(auth, "a")
        root = self.root / "browser"
        root.mkdir()
        self.json_file([self.conversation()], "browser/conversations.json")
        with self.assertRaises(ValueError):
            importers.parse_chatgpt(root, "a")
        auth_rollout, _ = self.session_file([], "credentials.jsonl")
        with self.assertRaises(ValueError):
            importers.parse_codex(auth_rollout, "a")

    def test_bom_json_unsupported_and_malformed_preserve_raw(self):
        path = self.json_file([self.conversation()])
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        self.assertEqual(len(importers.parse_chatgpt(path, "a").conversations), 1)
        unsupported = self.json_file({"future-schema": 1}, "future.json")
        batch = importers.parse_chatgpt(unsupported, "a")
        self.assertEqual(len(batch.artifacts), 1)
        self.assertTrue(any("unsupported export schema" in warning for warning in batch.warnings))
        path.write_bytes(b"[{broken")
        batch = importers.parse_chatgpt(path, "a")
        self.assertEqual(batch.artifacts[0][1], b"[{broken")
        self.assertEqual(batch.skipped, 1)

    def test_metadata_is_bounded_but_full_raw_bytes_survive(self):
        conv = self.conversation()
        conv["mapping"]["user-node"]["message"]["metadata"] = {"huge": "x" * 100000}
        path = self.json_file([conv])
        batch = importers.parse_chatgpt(path, "a")
        metadata = batch.conversations[0].messages[0].metadata
        self.assertTrue(metadata["raw_truncated"])
        self.assertEqual(metadata["artifact"], "chatgpt/conversations.json")
        self.assertTrue(metadata["untrusted"])
        self.assertLess(len(json.dumps(metadata).encode()), importers.MAX_METADATA_BYTES)
        self.assertIn(b"x" * 100000, batch.artifacts[0][1])

    def test_missing_parents_and_malformed_nodes_warn(self):
        conv = self.conversation()
        conv["mapping"]["user-node"]["parent"] = "nonexistent"
        conv["mapping"]["malformed"] = {"message": "not an object"}
        batch = importers.parse_chatgpt(self.json_file([conv]), "a")
        self.assertTrue(any("missing parent" in warning for warning in batch.warnings))
        self.assertTrue(any("malformed message" in warning for warning in batch.warnings))
        self.assertEqual(len(batch.conversations[0].messages), 3)

    @staticmethod
    def codex_records():
        return [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "thread-one", "cwd": "D:/project", "timestamp": "2026-01-01T00:00:00Z"},
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Continue"},
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Continue"}],
                },
            },
            {
                "timestamp": "2026-01-01T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec",
                    "call_id": "call-1",
                    "arguments": '{"cmd":"git status"}',
                },
            },
            {
                "timestamp": "2026-01-01T00:00:03Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "call-1", "output": "clean"},
            },
            {
                "timestamp": "2026-01-01T00:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done"}],
                },
            },
            {
                "timestamp": "2026-01-01T00:00:04Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "Done"},
            },
        ]

    def test_codex_tools_and_event_duplicates_preserved_correctly(self):
        path, blob = self.session_file(self.codex_records())
        batch = importers.parse_codex(path, "account-b", "tao")
        conv = batch.conversations[0]
        self.assertEqual(conv.id, "thread-one")
        self.assertEqual(conv.cwd, "D:/project")
        self.assertEqual([msg.role for msg in conv.messages], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(conv.messages[1].kind, "function_call")
        self.assertEqual(conv.messages[2].metadata["call_id"], "call-1")
        self.assertIn("git status", conv.messages[1].text)
        self.assertEqual(batch.artifacts[0][1], blob)
        self.assertEqual(path.read_bytes(), blob)

    def test_codex_unknown_records_and_event_only_fallback(self):
        records = self.codex_records()[:1] + [
            {
                "timestamp": "2026-01-01T00:00:02Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "Only event"},
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "type": "future_record",
                "payload": {"unknown": "future info"},
            },
        ]
        path, _ = self.session_file(records)
        conv = importers.parse_codex(path, "a").conversations[0]
        self.assertEqual(conv.messages[0].kind, "record")
        self.assertEqual(conv.messages[0].metadata["raw_record"]["payload"]["unknown"], "future info")
        self.assertEqual(conv.messages[1].kind, "event_fallback")
        self.assertEqual(conv.messages[1].text, "Only event")

    def test_codex_infers_portable_cwd_project_and_preserves_explicit_override(self):
        for cwd, expected in (
            (r"D:\Work\Tao", "Tao"),
            ("D:/Work/Tao/", "Tao"),
            (r"\\server\share\Accordance", "Accordance"),
            ("/home/user/project", "project"),
            ("/", ""),
            ("D:/", ""),
            ("", ""),
        ):
            with self.subTest(cwd=cwd):
                records = self.codex_records()
                records[0]["payload"]["cwd"] = cwd
                path, _ = self.session_file(records)
                batch = importers.parse_codex(path, "a")
                self.assertEqual(batch.conversations[0].project, expected)
                self.assertEqual(batch.conversations[0].cwd, cwd)
                if expected:
                    self.assertTrue(any("same basename" in warning for warning in batch.warnings))
                overridden = importers.parse_codex(path, "a", "explicit-project")
                self.assertEqual(overridden.conversations[0].project, "explicit-project")
                self.assertFalse(any("same basename" in warning for warning in overridden.warnings))

    def test_codex_repeated_event_and_response_are_not_all_collapsed(self):
        records = self.codex_records() + [
            {
                "timestamp": "2026-01-01T00:00:05Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Continue"},
            },
        ]
        path, _ = self.session_file(records)
        conv = importers.parse_codex(path, "a").conversations[0]
        self.assertEqual(sum(message.role == "user" for message in conv.messages), 2)

    def test_codex_event_fallback_id_survives_later_response_append(self):
        records = self.codex_records()[:2]
        path, _ = self.session_file(records)
        before = importers.parse_codex(path, "a").conversations[0].messages
        records.append(self.codex_records()[2])
        self.session_file(records)
        after = importers.parse_codex(path, "a").conversations[0].messages
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 1)
        self.assertEqual(before[0].id, after[0].id)
        self.assertEqual(before[0].kind, "event_fallback")
        self.assertEqual(after[0].kind, "message")

    def test_codex_media_pointer_does_not_duplicate_user_event(self):
        records = self.codex_records()[:3]
        records[-1]["payload"]["content"].append(
            {"type": "input_image", "image_url": "file:///user-image.png"}
        )
        path, _ = self.session_file(records)
        conv = importers.parse_codex(path, "a").conversations[0]
        self.assertEqual(len(conv.messages), 1)
        self.assertIn("input_image reference", conv.messages[0].text)

    def test_invalid_unicode_indexes_safely_and_original_escape_survives(self):
        conv = self.conversation()
        conv["title"] = "title\ud800"
        conv["mapping"]["user-node"]["message"]["content"]["parts"] = ["text\ud800"]
        path = self.root / "conversations.json"
        path.write_bytes(json.dumps([conv], ensure_ascii=True).encode())
        batch = importers.parse_chatgpt(path, "a")
        batch.conversations[0].title.encode("utf-8")
        batch.conversations[0].messages[0].text.encode("utf-8")
        json.dumps(batch.conversations[0].messages[0].metadata, ensure_ascii=False).encode("utf-8")
        self.assertIn(b"\\ud800", batch.artifacts[0][1])

    def test_codex_scans_only_sessions_and_archived_sessions(self):
        self.session_file(self.codex_records(), "sessions/2026/10/rollout.jsonl")
        archived = self.codex_records()
        archived[0]["payload"]["id"] = "archived-thread"
        self.session_file(archived, "archived_sessions/old.jsonl")
        self.session_file(self.codex_records(), "root-untrusted.jsonl")
        self.session_file(self.codex_records(), "browser/other.jsonl")
        self.json_file({"key": "NEVER"}, "auth.json")
        batch = importers.parse_codex(self.root, "a")
        self.assertEqual({conv.id for conv in batch.conversations}, {"thread-one", "archived-thread"})
        self.assertEqual(len(batch.artifacts), 2)
        self.assertFalse(any(b"NEVER" in blob for _, blob, _ in batch.artifacts))

    def test_codex_incremental_paths_keep_original_relative_artifact_names(self):
        selected, _ = self.session_file(self.codex_records(), "sessions/2026/10/selected.jsonl")
        self.session_file(self.codex_records(), "sessions/2026/10/untouched.jsonl")
        self.session_file(self.codex_records(), "browser/forbidden.jsonl")
        self.assertEqual(len(importers.codex_files(self.root)), 2)
        batch = importers.parse_codex(self.root, "a", only_paths={selected})
        self.assertEqual(len(batch.artifacts), 1)
        self.assertEqual(batch.artifacts[0][0], "codex/sessions/2026/10/selected.jsonl")
        self.assertEqual(len(batch.conversations), 1)
        self.assertFalse(importers.parse_codex(self.root, "a", only_paths=set()).artifacts)

    def test_codex_truncated_final_record_and_malformed_middle_survive(self):
        path, blob = self.session_file(self.codex_records(), trailing=b'{"type":"response_item"')
        path.write_bytes(blob.replace(b'"type": "function_call_output"', b'"type": broken'))
        batch = importers.parse_codex(path, "a")
        self.assertTrue(any("truncated final record" in warning for warning in batch.warnings))
        self.assertTrue(any("malformed JSONL" in warning for warning in batch.warnings))
        self.assertEqual(batch.artifacts[0][1], path.read_bytes())
        self.assertEqual(batch.skipped, 2)
        self.assertGreater(len(batch.conversations[0].messages), 0)

    def test_codex_complete_final_record_without_newline_is_not_truncation(self):
        path, blob = self.session_file(self.codex_records())
        path.write_bytes(blob.rstrip(b"\n"))
        batch = importers.parse_codex(path, "a")
        self.assertFalse(any("truncated" in warning for warning in batch.warnings))

    def test_codex_title_skips_runtime_envelopes_without_losing_messages(self):
        texts = [
            "  # AGENTS.md instructions\n<INSTRUCTIONS>runtime</INSTRUCTIONS>",
            "<environment_context>cwd</environment_context>",
            "<INSTRUCTIONS>settings</INSTRUCTIONS>",
            "<turn_aborted>interrupted</turn_aborted>",
            "<skills_instructions>skills</skills_instructions>",
            "<external_codex_apps_open_page>{}</external_codex_apps_open_page>",
            "帮我实现\n 本地记忆跨账户衔接",
        ]
        records = self.codex_records()[:1]
        for index, text in enumerate(texts):
            records.append(
                {
                    "timestamp": f"2026-01-01T00:00:{index + 1:02d}Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
        path, _ = self.session_file(records)
        conv = importers.parse_codex(path, "a").conversations[0]
        self.assertEqual(conv.title, "帮我实现 本地记忆跨账户衔接")
        self.assertEqual(len(conv.messages), len(texts))
        self.assertEqual(conv.messages[0].text, texts[0])
        self.assertEqual(importers.suggest_codex_title(texts[:-1], "fallback"), "fallback")
        self.assertEqual(importers.suggest_codex_title(texts[:-1]), "")
        records[0]["payload"]["title"] = path.stem
        self.session_file(records)
        self.assertEqual(importers.parse_codex(path, "a").conversations[0].title, path.stem)

    def test_codex_title_falls_back_when_no_actual_user_request_exists(self):
        records = self.codex_records()[:1]
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "<environment_context>runtime only</environment_context>",
                },
            }
        )
        path, _ = self.session_file(records)
        self.assertEqual(importers.parse_codex(path, "a").conversations[0].title, path.stem)

    def test_importer_parses_the_one_snapshot_it_archives(self):
        path = self.json_file([self.conversation()])
        before = path.read_bytes()
        real_snapshot = importers._snapshot

        def changing_snapshot(candidate, batch):
            blob = real_snapshot(candidate, batch)
            candidate.write_bytes(b"[]")
            return blob

        with patch.object(importers, "_snapshot", side_effect=changing_snapshot):
            batch = importers.parse_chatgpt(path, "a")
        self.assertEqual(batch.artifacts[0][1], before)
        self.assertEqual(len(batch.conversations), 1)
        self.assertEqual(path.read_bytes(), b"[]")

    def test_symlink_sources_not_followed(self):
        path = self.json_file([self.conversation()])
        link = self.root / "linked.json"
        try:
            link.symlink_to(path)
        except OSError:
            self.skipTest("Creating symlinks requires Windows privilege or Developer Mode.")
        with self.assertRaises(ValueError):
            importers.parse_chatgpt(link, "a")


if __name__ == "__main__":
    unittest.main()
