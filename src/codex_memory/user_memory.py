"""Explicit user memories as immutable, source-addressed archive revisions.

Uses existing artifact tables, so old vaults and backup formats need no schema
migration. No automatic biography mining, model calls or credential access.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from codex_memory.security import bounded_int, json_text, label, redact
from codex_memory.store import Store, stable_id

SOURCE = "personal-memory"
PREFIX = "user-memory/v1/"
MAX_KEYS = 512
MAX_RECORD_BYTES = 32768
TRUST = "historical user-sourced reference; current instructions take precedence; identity and semantic accuracy are not automatically verified"


def _read(store: Store, digest: str) -> dict:
    path = store.blob_path(digest)
    if path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError("个人记忆记录超限")
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("个人记忆原件哈希不匹配，请先审计档案")
    try:
        record = json.loads(raw)
        if (
            record.get("format") != "user-memory-v1"
            or record.get("status") not in {"proposed", "confirmed", "withdrawn"}
            or not isinstance(record.get("project"), str)
            or len(record["project"]) > 256
            or not isinstance(record.get("account"), str)
            or len(record["account"]) > 256
            or not isinstance(record.get("created_at"), str)
            or len(record["created_at"]) > 64
            or not isinstance(record.get("previous_hash"), str)
            or (record["previous_hash"] and not re.fullmatch(r"[0-9a-f]{64}", record["previous_hash"]))
            or not isinstance(record.get("value"), str)
            or len(record["value"]) > 4000
            or not isinstance(record.get("evidence"), list)
            or len(record["evidence"]) > 16
            or any(
                not isinstance(mid, str) or not re.fullmatch(r"[0-9a-f]{32}", mid)
                for mid in record["evidence"]
            )
            or (record["status"] == "confirmed" and not record["evidence"])
            or not isinstance(record.get("key"), str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,63}", record["key"])
            or record.get("id") != stable_id("user-memory-v1", record.get("project", ""), record["key"])
        ):
            raise ValueError("个人记忆记录格式无效")
        record["value"] = redact(record["value"])
        record["account"] = redact(record["account"])
        # A restored JSON source cannot smuggle extra metadata into model tools.
        return {
            **{
                field: record[field]
                for field in (
                    "format",
                    "id",
                    "key",
                    "value",
                    "project",
                    "status",
                    "evidence",
                    "account",
                    "created_at",
                    "previous_hash",
                )
            },
            "hash": digest,
            "trust": TRUST,
        }
    except (TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("个人记忆记录格式无效") from exc


def list_memory(
    store: Store, project: str = "", include_proposed: bool = False, limit: int = 100
) -> list[dict]:
    project = label(project, "project", True)
    bounded_int(limit, 1, 100, "limit")
    if not isinstance(include_proposed, bool):
        raise ValueError("include_proposed 必须是布尔值")
    with store.connect() as con:
        # One current revision per stable memory ID, shared by every account
        # label. A withdrawn latest revision must hide an older confirmed one.
        rows = con.execute(
            "SELECT hash,name FROM (SELECT hash,name,row_number() OVER "
            "(PARTITION BY name ORDER BY imported_at DESC,id DESC) AS position FROM artifact_sources WHERE source=?) "
            "WHERE position=1 ORDER BY name LIMIT ?",
            (SOURCE, MAX_KEYS + 1),
        ).fetchall()
    if len(rows) > MAX_KEYS:
        raise ValueError("个人记忆键超过 512 条，请检查档案")
    records = []
    for row in rows:
        if not row["name"].startswith(PREFIX):
            raise ValueError("个人记忆来源格式无效")
        record = _read(store, row["hash"])
        if row["name"] != PREFIX + record["id"] + ".json":
            raise ValueError("个人记忆身份与来源不匹配")
        if record["project"] not in {"", project}:
            continue
        if record["status"] != "confirmed" and not include_proposed:
            continue
        if record["evidence"]:
            with store.connect() as con:
                for mid in record["evidence"]:
                    source = con.execute("SELECT role FROM messages WHERE id=?", (mid,)).fetchone()
                    if not source or source["role"] != "user":
                        raise ValueError("个人记忆原话来源失效；请检查档案后重新确认")
        records.append(record)
    return sorted(records, key=lambda record: (bool(record["project"]), record["key"]))[:limit]


def save_memory(
    store: Store,
    key: str,
    value: str,
    project: str = "",
    status: str = "proposed",
    evidence: list[str] | None = None,
    account: str = "default",
) -> dict:
    if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,63}", key):
        raise ValueError(
            "记忆 key 必须是 2..64 字符的 ASCII 字母、数字、点、横线或下划线，且以字母或数字开头"
        )
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError("记忆 value 必须是 1..4000 字的非空文本")
    if not isinstance(status, str) or status not in {"proposed", "confirmed", "withdrawn"}:
        raise ValueError("记忆 status 必须是 proposed/confirmed/withdrawn")
    project, account = (
        label(project, "project", True),
        label(redact(account) if isinstance(account, str) else account, "account"),
    )
    evidence = [] if evidence is None else evidence
    if (
        not isinstance(evidence, list)
        or len(evidence) > 16
        or any(not isinstance(mid, str) or not re.fullmatch(r"[0-9a-f]{32}", mid) for mid in evidence)
    ):
        raise ValueError("evidence 必须是最多 16 个内部用户消息 ID 的列表")
    evidence = list(dict.fromkeys(evidence))
    if status == "confirmed" and not evidence:
        raise ValueError("confirmed 记忆至少需要一条用户原话消息 ID；不能把助手推测直接确认")
    mid = stable_id("user-memory-v1", project, key)
    name = PREFIX + mid + ".json"
    value = redact(value.strip())
    with store.connect() as con:
        # Serialize revision chains with other memory writers, but not readers.
        con.execute("BEGIN IMMEDIATE")
        for source_id in evidence:
            source = con.execute("SELECT role FROM messages WHERE id=?", (source_id,)).fetchone()
            if not source or source["role"] != "user":
                raise ValueError("个人记忆来源必须是已归档的用户消息，不能引用助手、工具或未知消息")
        current = con.execute(
            "SELECT hash FROM artifact_sources WHERE source=? AND name=? ORDER BY imported_at DESC,id DESC LIMIT 1",
            (SOURCE, name),
        ).fetchone()
        old = _read(store, current["hash"]) if current else None
        if old and all(
            old[field] == expected
            for field, expected in (("value", value), ("status", status), ("evidence", evidence))
        ):
            return old
        if not old:
            count = con.execute(
                "SELECT count(DISTINCT name) FROM artifact_sources WHERE source=?", (SOURCE,)
            ).fetchone()[0]
            if count >= MAX_KEYS:
                raise ValueError("个人记忆最多支持 512 个稳定键")
        created_at = datetime.now(UTC).isoformat(timespec="microseconds")
        record = {
            "format": "user-memory-v1",
            "id": mid,
            "key": key,
            "value": value,
            "project": project,
            "status": status,
            "evidence": evidence,
            "account": account,
            "created_at": created_at,
            "previous_hash": old["hash"] if old else "",
            "trust": TRUST,
        }
        raw = json_text(record).encode("utf-8")
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("个人记忆记录超限")
        digest = store.archive(raw)
        con.execute(
            "INSERT OR IGNORE INTO artifacts(hash,size,mime,created_at) VALUES(?,?,?,?)",
            (digest, len(raw), "application/json", created_at),
        )
        con.execute(
            "INSERT INTO artifact_sources(hash,source,account,name,imported_at) VALUES(?,?,?,?,?)",
            (digest, SOURCE, account, name, created_at),
        )
        return {**record, "hash": digest}
