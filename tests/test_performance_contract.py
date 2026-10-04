"""Structural performance contracts, without machine-dependent timing assertions.

All fixture archives are retained. No user histories or global Codex state are read.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from codex_memory.models import Conversation, ImportBatch, Message
from codex_memory.service import MemoryService
from codex_memory.store import Store


class PerformanceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="codex-memory-performance-contract-"))
        self.service = MemoryService(self.directory / "vault")
        self.service.initialize()

    @staticmethod
    def batch(count: int = 100, *, revised: bool = False) -> ImportBatch:
        messages = [
            Message(f"message-{index}", "user", f"synthetic performance fixture number {index}")
            for index in range(count)
        ]
        if revised:
            messages[50].text = "revision replacement unique searchable content"
        conversation = Conversation(
            "performance-thread",
            "chatgpt",
            "plus-fixture",
            "Performance fixture",
            messages,
            updated_at="2026-10-04T02:00:00Z" if revised else "2026-10-04T01:00:00Z",
            project="performance-project",
        )
        return ImportBatch([conversation])

    @staticmethod
    def normalized(statement: str) -> str:
        return " ".join(statement.strip().upper().split())

    def test_new_message_batch_never_scans_fts_deletions_but_revisions_replace_index(self):
        with self.service.store.connect() as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='message_fts'").fetchone():
                self.skipTest("SQLite build has no optional FTS5 trigram support")
        statements: list[str] = []
        original_connect = self.service.store.connect

        @contextmanager
        def traced_connect():
            with original_connect() as connection:
                connection.set_trace_callback(statements.append)
                try:
                    yield connection
                finally:
                    connection.set_trace_callback(None)

        with patch.object(self.service.store, "connect", traced_connect):
            report = self.service._ingest(self.batch(), "chatgpt", "plus-fixture")
        self.assertEqual(report["messages"], 100)
        first_deletes = [
            sql for sql in statements if self.normalized(sql).startswith("DELETE FROM MESSAGE_FTS WHERE")
        ]
        self.assertEqual(
            first_deletes, [], "New IDs must not perform an UNINDEXED FTS deletion scan per insert"
        )
        self.assertEqual(self.service.audit()["search_index"], {"messages": 100, "indexed": 100})

        statements.clear()
        with patch.object(self.service.store, "connect", traced_connect):
            revised = self.service._ingest(self.batch(revised=True), "chatgpt", "plus-fixture")
        revision_deletes = [
            sql for sql in statements if self.normalized(sql).startswith("DELETE FROM MESSAGE_FTS WHERE")
        ]
        self.assertEqual(revised["messages"], 1)
        self.assertEqual(revised["unchanged"], 99)
        self.assertEqual(len(revision_deletes), 1, "Only the changed existing message needs FTS replacement")
        self.assertEqual(self.service.stats()["messages"], 100)
        self.assertEqual(self.service.stats()["message_revisions"], 101)
        self.assertEqual(len(self.service.search("revision replacement")), 1)
        self.assertEqual(self.service.search("fixture number 50"), [])
        self.assertTrue(self.service.audit()["ok"])

    def test_fresh_store_existing_wal_initialization_executes_only_read_queries(self):
        statements: list[str] = []
        original_connect = sqlite3.connect

        def traced_sqlite_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        reader = Store(self.service.data_dir)
        with patch("codex_memory.store.sqlite3.connect", traced_sqlite_connect):
            reader.initialize()
        self.assertTrue(reader._ready)
        queries = [self.normalized(sql) for sql in statements]
        self.assertTrue(queries)
        self.assertTrue(
            all(query.startswith("SELECT ") or query == "PRAGMA JOURNAL_MODE" for query in queries), queries
        )
        self.assertFalse(
            any(query.startswith("CREATE ") or query.startswith("PRAGMA JOURNAL_MODE=") for query in queries)
        )

    def test_independent_service_reads_committed_snapshot_during_actual_writer_transaction(self):
        self.service._ingest(self.batch(count=100), "chatgpt", "plus-fixture")
        writer = sqlite3.connect(self.service.db_path, timeout=0)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE conversations SET title='uncommitted writer title'")
        statements: list[str] = []
        original_connect = sqlite3.connect

        def traced_sqlite_connect(*args, **kwargs):
            # Fail a regression in initialization immediately, without a timing expectation.
            # Correct WAL readers never need the lock held by the writer.
            kwargs["timeout"] = 0
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        try:
            reader = MemoryService(self.service.data_dir)
            with patch("codex_memory.store.sqlite3.connect", traced_sqlite_connect):
                stats = reader.stats()
                conversations = reader.list_conversations()
            self.assertEqual(stats["messages"], 100)
            self.assertEqual(stats["conversations"], 1)
            self.assertEqual(conversations[0]["title"], "Performance fixture")
            self.assertFalse(
                any(
                    self.normalized(sql).startswith(
                        ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ")
                    )
                    for sql in statements
                )
            )
            self.assertTrue(writer.in_transaction)
        finally:
            writer.rollback()
            writer.close()


if __name__ == "__main__":
    unittest.main()
