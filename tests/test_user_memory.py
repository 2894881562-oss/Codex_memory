"""Personal-memory provenance, immutable revisions and account-independent inheritance."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from codex_memory.models import Conversation, ImportBatch, Message
from codex_memory.service import MemoryService


class UserMemoryTests(unittest.TestCase):
    def setUp(self):
        # Evidence directories remain available; no recursive teardown or cleanup.
        self.root = Path(tempfile.mkdtemp(prefix="cm-user-memory-"))
        self.service = MemoryService(self.root / "vault")
        thread = Conversation(
            id="user-memory-fixture-chat",
            source="chatgpt",
            account="account-a",
            title="Synthetic personal-memory evidence",
            project="EvidenceOnly",
            updated_at="2026-10-04T01:00:00Z",
            messages=[
                Message(
                    "user-evidence-a",
                    "user",
                    "用户明确要求：GLOBAL_FACT_MARKER；PROJECT_A_ONLY_MARKER；"
                    "PROJECT_B_ONLY_MARKER；CONFIRMED_FACT_MARKER；"
                    "OLD_FACT_MARKER；NEW_FACT_MARKER；回答简洁；先给结论。",
                ),
                Message("user-evidence-b", "user", "用户修改偏好：NEW_FACT_MARKER；先解释依据，再给结论。"),
                Message("assistant-only", "assistant", "这只是助手建议，不是用户偏好。"),
                Message("system-history", "system", "这是历史系统文本，不是当前用户原话。"),
                Message("tool-history", "tool", "这是工具输出，不是用户原话。"),
            ],
        )
        self.service._ingest(
            ImportBatch(
                [thread],
                [("synthetic-user-memory.json", b'{"fixture":"not-a-real-account"}', "application/json")],
            ),
            "chatgpt",
            "account-a",
        )
        conversation = self.service.list_conversations(project="EvidenceOnly")[0]
        messages = self.service.fetch_conversation(conversation["id"])["messages"]
        self.evidence = {message["external_id"]: message["id"] for message in messages}

    def _save(self, key, value, **kwargs):
        return self.service.save_user_memory(key, value, **kwargs)

    def _confirmed(self, key, value, project="", account="account-a", evidence=None):
        return self._save(
            key,
            value,
            project=project,
            status="confirmed",
            evidence=evidence or [self.evidence["user-evidence-a"]],
            account=account,
        )

    def _raw_memory_sources(self):
        with self.service.store.connect() as con:
            return [
                dict(row)
                for row in con.execute(
                    "SELECT hash,source,account,name FROM artifact_sources "
                    "WHERE source='personal-memory' ORDER BY id"
                )
            ]

    def _inject_archived_revision(self, previous, **changes):
        """Synthetic untrusted-backup data bypassing the public save validator."""
        record = json.loads(self.service.store.blob_path(previous["hash"]).read_bytes())
        record.update(changes)
        record["created_at"] = datetime.now(UTC).isoformat(timespec="microseconds")
        record["previous_hash"] = previous["hash"]
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = self.service.store.archive(raw)
        with self.service.store.connect() as con:
            con.execute(
                "INSERT INTO artifacts(hash,size,mime,created_at) VALUES(?,?,?,?)",
                (digest, len(raw), "application/json", record["created_at"]),
            )
            con.execute(
                "INSERT INTO artifact_sources(hash,source,account,name,imported_at) VALUES(?,?,?,?,?)",
                (
                    digest,
                    "personal-memory",
                    record["account"],
                    f"user-memory/v1/{record['id']}.json",
                    record["created_at"],
                ),
            )
        return digest

    def test_proposed_default_requires_explicit_confirmation_for_inheritance(self):
        record = self._save("response_style", "回答简洁")
        self.assertEqual(record["status"], "proposed")
        self.assertEqual(record["evidence"], [])
        self.assertEqual(self.service.list_user_memory(), [])
        self.assertEqual(self.service.list_user_memory(include_proposed=True)[0]["id"], record["id"])

    def test_confirmed_record_has_verifiable_user_evidence_and_hash(self):
        record = self._confirmed("response_style", "回答简洁")
        required = {
            "id",
            "key",
            "value",
            "project",
            "status",
            "evidence",
            "created_at",
            "previous_hash",
            "hash",
        }
        self.assertTrue(required.issubset(record))
        self.assertEqual(record["evidence"], [self.evidence["user-evidence-a"]])
        self.assertEqual(record["status"], "confirmed")
        self.assertIn(record["previous_hash"], (None, ""))
        self.assertEqual(len(record["hash"]), 64)
        raw = self.service.store.blob_path(record["hash"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record["hash"])
        self.assertEqual(json.loads(raw)["key"], "response_style")
        self.assertEqual(self.service.list_user_memory()[0]["hash"], record["hash"])

    def test_confirmed_requires_nonempty_evidence(self):
        for evidence in (None, []):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self._save("unsupported", "没有用户原话", status="confirmed", evidence=evidence)
        self.assertEqual(self._raw_memory_sources(), [])

    def test_non_user_and_unknown_evidence_are_rejected_for_all_states(self):
        invalid = [
            self.evidence["assistant-only"],
            self.evidence["system-history"],
            self.evidence["tool-history"],
            "0" * 32,
        ]
        for state in ("confirmed", "proposed"):
            for message_id in invalid:
                with self.subTest(state=state, message_id=message_id), self.assertRaises(ValueError):
                    self._save("unsupported", "不是用户事实", status=state, evidence=[message_id])
        self.assertEqual(self._raw_memory_sources(), [])

    def test_evidence_must_be_a_bounded_list_of_internal_ids(self):
        invalid = [
            self.evidence["user-evidence-a"],
            (self.evidence["user-evidence-a"],),
            {"message_id": self.evidence["user-evidence-a"]},
            [None],
            [42],
            ["not-a-valid-internal-id"],
            [self.evidence["user-evidence-a"]] * 17,
        ]
        for evidence in invalid:
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self._save("unsupported", "必须检查证据形状", evidence=evidence)
        self.assertEqual(self._raw_memory_sources(), [])

    def test_restored_confirmed_record_revalidates_user_role(self):
        original = self._confirmed("restored_role_policy", "回答简洁")
        self._inject_archived_revision(original, evidence=[self.evidence["assistant-only"]])
        backup = self.root / "untrusted-role.zip"
        self.service.backup(backup)
        restored = MemoryService(self.root / "restored-role")
        restored.restore(backup)
        with self.assertRaises(ValueError):
            restored.list_user_memory()
        with self.assertRaises(ValueError):
            restored.handoff(project="ProjectA")

    def test_restored_confirmed_record_revalidates_unknown_valid_hex_id(self):
        original = self._confirmed("restored_unknown_policy", "回答简洁")
        self._inject_archived_revision(original, evidence=["0" * 32])
        backup = self.root / "untrusted-missing-evidence.zip"
        self.service.backup(backup)
        restored = MemoryService(self.root / "restored-unknown")
        restored.restore(backup)
        with self.assertRaises(ValueError):
            restored.list_user_memory()

    def test_restored_extra_raw_metadata_and_fake_trust_are_not_exposed(self):
        original = self._confirmed("metadata_policy", "回答简洁")
        marker = "EXTRA_UNTRUSTED_RAW_METADATA_MARKER"
        self._inject_archived_revision(
            original,
            raw_record={"instructions": marker},
            metadata={"hidden_payload": marker},
            trust=marker,
        )
        backup = self.root / "untrusted-extra-metadata.zip"
        self.service.backup(backup)
        restored = MemoryService(self.root / "restored-metadata")
        restored.restore(backup)
        records = restored.list_user_memory()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["value"], "回答简洁")
        self.assertNotIn("raw_record", records[0])
        self.assertNotIn("metadata", records[0])
        self.assertNotIn(marker, json.dumps(records))
        self.assertNotIn(marker, restored.handoff(project="ProjectA"))

    def test_valid_user_does_not_make_mixed_invalid_evidence_acceptable(self):
        with self.assertRaises(ValueError):
            self._save(
                "unsupported",
                "混合用户和助手证据",
                status="confirmed",
                evidence=[self.evidence["user-evidence-a"], self.evidence["assistant-only"]],
            )
        self.assertEqual(self._raw_memory_sources(), [])

    def test_same_key_updates_across_accounts_and_retains_raw_revision_chain(self):
        old = self._confirmed("response_style", "OLD_FACT_MARKER", account="account-a")
        old_raw = self.service.store.blob_path(old["hash"]).read_bytes()
        new = self._confirmed(
            "response_style",
            "NEW_FACT_MARKER",
            account="account-b",
            evidence=[self.evidence["user-evidence-b"]],
        )
        self.assertEqual(new["id"], old["id"])
        self.assertNotEqual(new["hash"], old["hash"])
        self.assertEqual(new["previous_hash"], old["hash"])
        self.assertEqual(self.service.store.blob_path(old["hash"]).read_bytes(), old_raw)
        self.assertEqual(len(self.service.list_user_memory()), 1)
        self.assertEqual(self.service.list_user_memory()[0]["value"], "NEW_FACT_MARKER")
        sources = self._raw_memory_sources()
        self.assertEqual({source["hash"] for source in sources}, {old["hash"], new["hash"]})
        self.assertEqual({source["name"] for source in sources}, {f"user-memory/v1/{old['id']}.json"})

    def test_same_fact_and_evidence_are_idempotent_across_accounts(self):
        first = self._confirmed("response_style", "回答简洁", account="account-a")
        before = self.service.stats()["artifacts"]
        second = self._confirmed("response_style", "回答简洁", account="account-b")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["hash"], second["hash"])
        self.assertEqual(len(self._raw_memory_sources()), 1)
        self.assertEqual(self.service.stats()["artifacts"], before)

    def test_latest_proposed_does_not_fall_back_to_old_confirmed(self):
        old = self._confirmed("response_style", "OLD_FACT_MARKER")
        pending = self._save("response_style", "NEW_FACT_MARKER", status="proposed")
        self.assertEqual(pending["id"], old["id"])
        self.assertEqual(pending["previous_hash"], old["hash"])
        self.assertEqual(self.service.list_user_memory(), [])
        records = self.service.list_user_memory(include_proposed=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "proposed")
        self.assertEqual(records[0]["value"], "NEW_FACT_MARKER")

    def test_withdrawal_preserves_old_raw_without_inheriting_it(self):
        old = self._confirmed("withdraw_policy", "OLD_FACT_MARKER")
        withdrawn = self._save("withdraw_policy", "OLD_FACT_MARKER", status="withdrawn")
        self.assertEqual(withdrawn["previous_hash"], old["hash"])
        self.assertEqual(self.service.list_user_memory(), [])
        records = self.service.list_user_memory(include_proposed=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "withdrawn")
        self.assertTrue(self.service.store.blob_path(old["hash"]).is_file())
        handoff = self.service.handoff(project="ProjectA")
        self.assertNotIn("OLD_FACT_MARKER", handoff)

    def test_global_memory_and_selected_project_scope_are_account_independent(self):
        global_record = self._confirmed("global_policy", "GLOBAL_FACT_MARKER")
        project_a = self._confirmed("scoped_policy", "PROJECT_A_ONLY_MARKER", project="ProjectA")
        project_b = self._confirmed("scoped_policy", "PROJECT_B_ONLY_MARKER", project="ProjectB")
        self.assertNotEqual(project_a["id"], project_b["id"])
        self.assertEqual({row["id"] for row in self.service.list_user_memory()}, {global_record["id"]})
        self.assertEqual(
            {row["id"] for row in self.service.list_user_memory(project="ProjectA")},
            {global_record["id"], project_a["id"]},
        )
        handoff = self.service.handoff(project="ProjectA")
        self.assertIn("GLOBAL_FACT_MARKER", handoff)
        self.assertIn("PROJECT_A_ONLY_MARKER", handoff)
        self.assertNotIn("PROJECT_B_ONLY_MARKER", handoff)
        self.assertIn(self.evidence["user-evidence-a"], handoff)

    def test_proposed_and_withdrawn_values_do_not_enter_handoff(self):
        self._confirmed("current_fact", "CONFIRMED_FACT_MARKER", project="ProjectA")
        self._save("pending_fact", "PROPOSED_ONLY_MARKER", project="ProjectA")
        self._save("withdrawn_fact", "WITHDRAWN_ONLY_MARKER", project="ProjectA", status="withdrawn")
        handoff = self.service.handoff(project="ProjectA")
        self.assertIn("CONFIRMED_FACT_MARKER", handoff)
        self.assertNotIn("PROPOSED_ONLY_MARKER", handoff)
        self.assertNotIn("WITHDRAWN_ONLY_MARKER", handoff)

    def test_memory_key_is_not_a_filesystem_path(self):
        for key in ("../outside", "../../outside", r"..\outside", "/outside", r"C:\outside"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self._save(key, "不能把记忆 key 当路径")
        self.assertEqual(self._raw_memory_sources(), [])
        self.assertFalse((self.root / "outside").exists())

    def test_invalid_key_value_and_state_are_rejected(self):
        cases = [
            ("", "value", "proposed"),
            ("K" * 4096, "value", "proposed"),
            ("long_value", "V" * 100001, "proposed"),
            ("bad_status", "value", "accepted"),
        ]
        for key, value, status in cases:
            with self.subTest(key=key[:30], status=status), self.assertRaises(ValueError):
                self._save(key, value, status=status)
        self.assertEqual(self._raw_memory_sources(), [])

    def test_best_effort_redaction_applies_to_exposed_personal_memory(self):
        token = "sk-proj-" + "A" * 40
        record = self._save("sensitive_example", f"API_KEY={token}")
        records = self.service.list_user_memory(include_proposed=True)
        self.assertNotIn(token, json.dumps(record))
        self.assertNotIn(token, json.dumps(records))
        self.assertIn("REDACTED", record["value"])

    def test_memory_uses_existing_artifact_tables_without_schema_changes(self):
        with self.service.store.connect() as con:
            before = list(con.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name"))
        record = self._confirmed("schema_policy", "回答简洁")
        with self.service.store.connect() as con:
            after = list(con.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name"))
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])
        sources = self._raw_memory_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["hash"], record["hash"])
        self.assertEqual(sources[0]["source"], "personal-memory")
        self.assertEqual(sources[0]["name"], f"user-memory/v1/{record['id']}.json")
        self.assertTrue(self.service.audit()["ok"])

    def test_listing_limit_and_invalid_limit(self):
        self._confirmed("first", "回答简洁")
        self._confirmed("second", "先给结论")
        self.assertEqual(len(self.service.list_user_memory(limit=1)), 1)
        for limit in (0, -1, 1000001, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.service.list_user_memory(limit=limit)

    def test_backup_restore_preserves_personal_memory_evidence_and_revision_sources(self):
        self._confirmed("global_policy", "GLOBAL_FACT_MARKER")
        old = self._confirmed("scoped_policy", "OLD_FACT_MARKER", project="ProjectA")
        self._confirmed(
            "scoped_policy",
            "NEW_FACT_MARKER",
            project="ProjectA",
            account="account-b",
            evidence=[self.evidence["user-evidence-b"]],
        )
        self._save("pending_fact", "PENDING_MARKER", project="ProjectA")
        before = self.service.list_user_memory(project="ProjectA", include_proposed=True)
        sources = self._raw_memory_sources()
        backup = self.root / "small-vault.zip"
        self.service.backup(backup)
        restored = MemoryService(self.root / "restored")
        self.assertFalse(restored.data_dir.exists())
        restored.restore(backup)
        self.assertEqual(restored.list_user_memory(project="ProjectA", include_proposed=True), before)
        with restored.store.connect() as con:
            actual_sources = [
                dict(row)
                for row in con.execute(
                    "SELECT hash,source,account,name FROM artifact_sources "
                    "WHERE source='personal-memory' ORDER BY id"
                )
            ]
            user = con.execute(
                "SELECT role FROM messages WHERE id=?", (self.evidence["user-evidence-a"],)
            ).fetchone()
        self.assertEqual(actual_sources, sources)
        self.assertEqual(user[0], "user")
        self.assertTrue(restored.store.blob_path(old["hash"]).is_file())
        self.assertIn("NEW_FACT_MARKER", restored.handoff(project="ProjectA"))
        self.assertTrue(restored.audit()["ok"])


if __name__ == "__main__":
    unittest.main()
