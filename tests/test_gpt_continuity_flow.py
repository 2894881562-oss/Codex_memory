"""Synthetic GPT archive/writeback flow, not real-account acceptance.

All fixtures and restored vaults stay in retained temporary directories. This
uses local public operations only: no model, network, browser or live service.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from codex_memory.service import MemoryService


class GPTContinuityFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cm-gpt-continuity-flow-"))
        self.service = MemoryService(self.root / "vault")
        self.project = "SyntheticGPTContinuity"
        self.account_a = "ACCOUNT_A"
        self.account_b = "ACCOUNT_B"
        self.export_path = self.root / "synthetic-segmented-export.zip"
        self.decision = "备份续接必须先校验 SHA256，禁止覆盖原库。我希望回答先给结论。"
        self.old_answer = "A_OLD_ANSWER：先做备份来源核对。"
        self._write_export()

    @staticmethod
    def _conversation(external_id: str, messages: list[tuple[str, str, str]]) -> dict:
        mapping = {"root": {"parent": None, "message": None}}
        parent = "root"
        for ordinal, (message_id, role, text) in enumerate(messages):
            mapping[message_id] = {
                "parent": parent,
                "message": {
                    "id": message_id,
                    "author": {"role": role},
                    "create_time": 1767225600 + ordinal,
                    "content": {"content_type": "text", "parts": [text]},
                },
            }
            parent = message_id
        return {
            "id": external_id,
            "title": f"合成备份续接/{external_id}",
            "create_time": 1767225600,
            "update_time": 1767225700,
            "current_node": parent,
            "mapping": mapping,
        }

    def _write_export(self):
        primary = self._conversation(
            "chat-a", [("a-user", "user", self.decision), ("a-answer", "assistant", self.old_answer)]
        )
        auxiliary = self._conversation("chat-a-aux", [("aux-user", "user", "无关天气记录。")])
        with zipfile.ZipFile(self.export_path, "w") as archive:
            archive.writestr("export/conversations-1.json", json.dumps([primary], ensure_ascii=False))
            archive.writestr("export/conversations-2.json", json.dumps([auxiliary], ensure_ascii=False))
            archive.writestr("export/attachments/file-fixture.txt", "SYNTHETIC_ATTACHMENT_BYTES")

    def _import_a(self):
        return self.service.import_chatgpt(self.export_path, account=self.account_a, project=self.project)

    def _thread(self, account: str, external_id: str) -> dict:
        return next(
            row
            for row in self.service.list_conversations(
                source="chatgpt", account=account, project=self.project
            )
            if row["external_id"] == external_id
        )

    @staticmethod
    def _message(service: MemoryService, conversation_id: str, external_id: str) -> dict:
        return next(
            row
            for row in service.fetch_conversation(conversation_id)["messages"]
            if row["external_id"] == external_id
        )

    def test_segmented_zip_repeat_import_observed_counts_are_not_new_records(self):
        original = self.export_path.read_bytes()
        initial = self._import_a()
        self.assertEqual(initial["conversations"], 2)
        self.assertEqual(initial["messages"], 3)
        self.assertEqual(initial["conversations_seen"], 2)
        self.assertEqual(initial["messages_seen"], 3)
        before = self.service.stats()
        repeated = self._import_a()
        self.assertEqual(repeated["conversations_seen"], 2)
        self.assertEqual(repeated["messages_seen"], 3)
        self.assertEqual(repeated["conversations"], 0)
        self.assertEqual(repeated["messages"], 0)
        self.assertEqual(repeated["unchanged"], 3)
        self.assertEqual(repeated["artifacts"], 0)
        after = self.service.stats()
        for field in ("conversations", "messages", "message_revisions", "artifacts"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(self.service.list_user_memory(project=self.project), [])
        self.assertEqual(self.export_path.read_bytes(), original)
        self.assertTrue(self.service.audit()["ok"])

    def test_confirmed_handoff_b_writeback_old_export_and_backup_restore(self):
        self._import_a()
        thread_a = self._thread(self.account_a, "chat-a")
        source_a = self._message(self.service, thread_a["id"], "a-user")
        self.assertEqual(source_a["role"], "user")
        self.assertEqual(source_a["text"], self.decision)
        preference = self.service.save_user_memory(
            "response.concise_first",
            "回答先给结论。",
            status="confirmed",
            evidence=[source_a["id"]],
            account=self.account_a,
        )
        strategy_a = self.service.save_user_memory(
            "project.backup_strategy",
            "备份续接先校验 SHA256，禁止覆盖原库。",
            project=self.project,
            status="confirmed",
            evidence=[source_a["id"]],
            account=self.account_a,
        )
        checkpoint_a = self.service.create_checkpoint(
            self.project,
            "备份续接",
            completed="已导入合成分片",
            decisions="校验 SHA256，不覆盖",
            next_steps="核对原始来源",
            blockers="真实账户未验收",
            account=self.account_a,
        )
        first_handoff = self.service.handoff(project=self.project, task="备份 来源", max_chars=1500)
        for marker in (strategy_a["id"], source_a["id"], checkpoint_a["id"], "SHA256", "不覆盖"):
            self.assertIn(marker, first_handoff)
        self.assertLessEqual(len(first_handoff), 1500)

        # A same-message later observation makes stale-export precedence a
        # meaningful check, independently of A/B account-label separation.
        newest_a_answer = "A_LIVE_NEW：备份来源已核对，接下来检查离线恢复。"
        self.service.ingest_live(
            {
                "source": "chatgpt",
                "account": self.account_a,
                "project": self.project,
                "conversation_id": "chat-a",
                "messages": [{"id": "a-answer", "role": "assistant", "text": newest_a_answer}],
                "metadata": {"observed_at": "2026-10-09T01:00:00Z"},
            }
        )
        task_b = "备份续接 B_WRITEBACK：下一步改为离线队列恢复验证，禁止覆盖原库。"
        writeback = self.service.ingest_live(
            {
                "source": "chatgpt",
                "account": self.account_b,
                "project": self.project,
                "conversation_id": "chat-b",
                "messages": [{"id": "b-user", "role": "user", "text": task_b}],
                "metadata": {"observed_at": "2026-10-09T02:00:00Z"},
            }
        )
        self.assertEqual(writeback["messages"], 1)
        thread_b = self._thread(self.account_b, "chat-b")
        self.assertNotEqual(thread_b["id"], thread_a["id"])
        source_b = self._message(self.service, thread_b["id"], "b-user")
        self.assertEqual(source_b["text"], task_b)
        self.assertEqual(source_b["role"], "user")
        self.assertEqual(
            self.service.search("B_WRITEBACK", account=self.account_b)[0]["message_id"], source_b["id"]
        )
        self.assertEqual(self.service.search("B_WRITEBACK", account=self.account_a), [])
        strategy_b = self.service.save_user_memory(
            "project.backup_strategy",
            "备份续接下一步改为离线队列恢复验证，禁止覆盖原库。",
            project=self.project,
            status="confirmed",
            evidence=[source_b["id"]],
            account=self.account_b,
        )
        self.assertEqual(strategy_b["id"], strategy_a["id"])
        self.assertEqual(strategy_b["previous_hash"], strategy_a["hash"])
        checkpoint_b = self.service.create_checkpoint(
            self.project,
            "备份续接",
            completed="合成 B 新消息已入库",
            decisions="禁止覆盖原库",
            next_steps="B_WRITEBACK：离线队列恢复验证",
            blockers="真实账户未验收",
            account=self.account_b,
        )

        stale = self._import_a()
        self.assertEqual(stale["messages"], 0)
        # A previously archived old fingerprint can count as unchanged rather
        # than a newly rejected stale revision; neither may roll back the index.
        self.assertEqual(stale["unchanged"] + stale["stale_revisions"], stale["messages_seen"])
        self.assertEqual(self._message(self.service, thread_a["id"], "a-answer")["text"], newest_a_answer)
        self.assertEqual(self._message(self.service, thread_b["id"], "b-user")["text"], task_b)
        self.assertEqual(self.service.project_state(self.project)["latest_checkpoint"], checkpoint_b)
        memories = self.service.list_user_memory(project=self.project)
        self.assertEqual({row["id"] for row in memories}, {preference["id"], strategy_b["id"]})
        current_strategy = next(row for row in memories if row["id"] == strategy_b["id"])
        self.assertEqual(current_strategy["evidence"], [source_b["id"]])
        self.assertEqual(current_strategy["hash"], strategy_b["hash"])
        self.assertTrue(self.service.store.blob_path(strategy_a["hash"]).is_file())
        third_handoff = self.service.handoff(project=self.project, task="备份 离线 队列", max_chars=3500)
        for marker in (checkpoint_b["id"], source_b["id"], "B_WRITEBACK", "离线队列恢复验证", "回答先给结论"):
            self.assertIn(marker, third_handoff)
        self.assertLessEqual(len(third_handoff), 3500)
        self.assertTrue(self.service.audit()["ok"])

        backup_path = self.root / "synthetic-continuity-backup.zip"
        self.service.backup(backup_path)
        restored = MemoryService(self.root / "restored-vault")
        restored.restore(backup_path)
        self.assertEqual(restored.list_user_memory(project=self.project), memories)
        self.assertEqual(restored.project_state(self.project)["latest_checkpoint"], checkpoint_b)
        self.assertEqual(self._message(restored, thread_b["id"], "b-user")["text"], task_b)
        self.assertEqual(self._message(restored, thread_a["id"], "a-answer")["text"], newest_a_answer)
        restored_handoff = restored.handoff(project=self.project, task="备份 离线 队列", max_chars=3500)
        self.assertEqual(restored_handoff, third_handoff)
        self.assertTrue(restored.store.blob_path(strategy_a["hash"]).is_file())
        self.assertTrue(restored.audit()["ok"])


if __name__ == "__main__":
    unittest.main()
