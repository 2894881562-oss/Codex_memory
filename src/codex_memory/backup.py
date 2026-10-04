"""Consistent portable backups; optional Scrypt + AES-GCM encryption."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import time
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

from codex_memory.security import is_link, json_text, safe_relative
from codex_memory.store import SCHEMA, SCHEMA_VERSION, Store

MAGIC = b"CMEMB1\x00"
MAX_RESTORE_BYTES = 8 * 1024 * 1024 * 1024
MAX_ENCRYPTED_BYTES = 512 * 1024 * 1024
MAX_SQLITE_BYTES = 2 * 1024 * 1024 * 1024
MAX_BLOB_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def _encrypt(raw: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if not password or len(password) < 10:
        raise ValueError("加密备份密码至少 10 个字符")
    if len(raw) > MAX_ENCRYPTED_BYTES:
        raise ValueError("加密备份超过 512 MB 内存上限，请先按项目导出或使用加密磁盘保存普通备份")
    salt, nonce = os.urandom(16), os.urandom(12)
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))
    return MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, raw, MAGIC)


def _decrypt(raw: bytes, password: str | None) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if len(raw) > MAX_ENCRYPTED_BYTES + len(MAGIC) + 44:
        raise ValueError("加密备份超过读取上限")
    if not password or len(raw) < len(MAGIC) + 44:
        raise ValueError("需要正确的加密备份密码")
    salt = raw[len(MAGIC) : len(MAGIC) + 16]
    nonce = raw[len(MAGIC) + 16 : len(MAGIC) + 28]
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))
    try:
        return AESGCM(key).decrypt(nonce, raw[len(MAGIC) + 28 :], MAGIC)
    except InvalidTag as exc:
        raise ValueError("密码错误或备份内容被修改") from exc


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _standalone_sqlite(raw: bytes) -> bytes:
    if len(raw) > MAX_SQLITE_BYTES:
        raise ValueError("SQLite 快照超过 2 GB 内存上限")
    if len(raw) < 100 or not raw.startswith(b"SQLite format 3\x00"):
        raise ValueError("备份 SQLite 文件头无效")
    if raw[18:20] not in {b"\x01\x01", b"\x02\x02"}:
        raise ValueError("备份 SQLite 日志版本无效")
    # An online backup already copies every committed page. Memory deserialize
    # cannot open a separate WAL; use the standalone journal header instead.
    return raw if raw[18:20] == b"\x01\x01" else raw[:18] + b"\x01\x01" + raw[20:]


def _snapshot(store: Store) -> tuple[bytes, list[tuple[str, int]]]:
    memory = sqlite3.connect(":memory:")
    try:
        memory.execute("PRAGMA temp_store=MEMORY")
        with store.connect() as source:
            page_size = source.execute("PRAGMA page_size").fetchone()[0]

            def bounded_backup(status: int, remaining: int, total: int) -> None:
                if total * page_size > MAX_SQLITE_BYTES:
                    raise ValueError("SQLite 快照超过 2 GB 内存上限")

            source.backup(memory, pages=256, progress=bounded_backup)
        raw = _standalone_sqlite(memory.serialize())
        artifacts = list(memory.execute("SELECT hash,size FROM artifacts"))
        return raw, artifacts
    except AttributeError as exc:
        raise ValueError(
            "当前 Python SQLite 不支持内存 serialize/deserialize；请使用提供的 Python 3.11+ 环境"
        ) from exc
    finally:
        memory.close()


def _stream_digest(
    source: BinaryIO, target: BinaryIO | None = None, limit: int = MAX_BLOB_BYTES
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(CHUNK_BYTES):
        size += len(chunk)
        if size > limit:
            raise ValueError("备份单文件超出大小上限")
        digest.update(chunk)
        if target is not None:
            target.write(chunk)
    return digest.hexdigest(), size


class _BoundedMemory(io.BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_ENCRYPTED_BYTES:
            raise ValueError("加密 ZIP 超过 512 MB 内存上限")
        return super().write(data)


def _write_zip(stream: BinaryIO, raw: bytes, artifacts: list[tuple[str, int]], store: Store) -> None:
    hashes = {"memory.sqlite3": hashlib.sha256(raw).hexdigest()}
    total = len(raw)
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("memory.sqlite3", raw)
        for digest, expected_size in artifacts:
            total += expected_size
            if total > MAX_RESTORE_BYTES:
                raise ValueError("备份超过 8 GB 展开大小上限")
            name = f"archive/sha256/{digest[:2]}/{digest}"
            with (
                store.blob_path(digest).open("rb") as source,
                archive.open(name, "w", force_zip64=True) as dest,
            ):
                observed_digest, observed_size = _stream_digest(source, dest, MAX_BLOB_BYTES)
            if observed_digest != digest or observed_size != expected_size:
                raise ValueError(f"原始归档在备份期间改变: {digest}；已生成文件可能不完整")
            hashes[name] = digest
        manifest = json_text(
            {
                "format": "codex-memory-backup",
                "schema_version": SCHEMA_VERSION,
                "sha256": hashes,
                "collector_token_included": False,
                "accounts": "labels only; no authentication",
            }
        ).encode("utf-8")
        if len(manifest) > MAX_MANIFEST_BYTES or total + len(manifest) > MAX_RESTORE_BYTES:
            raise ValueError("备份清单或展开大小超过上限")
        archive.writestr("manifest.json", manifest)


def create_backup(store: Store, path: Path, password: str | None = None) -> dict:
    target = path.expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError("备份目标已存在；请使用新文件名")
    if password is not None and (not isinstance(password, str) or len(password) < 10):
        raise ValueError("加密备份密码至少 10 个字符")
    raw, artifacts = _snapshot(store)
    if len(artifacts) + 2 > 100000:
        raise ValueError("备份文件数超过恢复上限")
    if len(raw) + sum(size for _, size in artifacts) > MAX_RESTORE_BYTES:
        raise ValueError("备份超过 8 GB 展开大小上限")
    # Verify originals before creating the exclusive final output. All hashes
    # are checked again while streaming; no plaintext backup stage is created.
    for digest, expected_size in artifacts:
        if (
            not _digest(digest)
            or not isinstance(expected_size, int)
            or not 0 <= expected_size <= MAX_BLOB_BYTES
        ):
            raise ValueError("原始归档记录无效或超出大小上限")
        with store.blob_path(digest).open("rb") as source:
            observed_digest, observed_size = _stream_digest(source, limit=MAX_BLOB_BYTES)
        if observed_digest != digest or observed_size != expected_size:
            raise ValueError(f"原始归档损坏，不能备份: {digest}")
    if password is not None:
        with _BoundedMemory() as memory:
            _write_zip(memory, raw, artifacts, store)
            encrypted = _encrypt(memory.getvalue(), password)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(encrypted)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            _write_zip(stream, raw, artifacts, store)
    return {
        "path": str(target),
        "encrypted": password is not None,
        "artifacts": len(artifacts),
        "size": target.stat().st_size,
        "staging": "memory",
    }


@lru_cache(maxsize=1)
def _trusted_tables() -> dict[str, str]:
    """Derive allowed main-table definitions from the package-owned schema."""
    con = sqlite3.connect(":memory:")
    try:
        con.execute("PRAGMA temp_store=MEMORY")
        con.executescript(SCHEMA)
        return dict(con.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'"))
    finally:
        con.close()


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.split()).casefold()


def _validate_sqlite(raw: bytes, hashes: dict[str, str], archive: zipfile.ZipFile) -> None:
    con = sqlite3.connect(":memory:")
    deadline = time.monotonic() + 30
    try:
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("PRAGMA query_only=ON")
        con.deserialize(_standalone_sqlite(raw))
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("PRAGMA query_only=ON")
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        # Product databases contain neither triggers nor views. Rejecting them
        # prevents imported schema from becoming executable future instructions.
        schema = list(con.execute("SELECT name,type,sql FROM sqlite_schema"))
        if any(kind in {"view", "trigger"} for _, kind, _ in schema):
            raise ValueError("备份 SQLite 不允许视图或触发器")
        tables = {name: sql for name, kind, sql in schema if kind == "table"}
        trusted = _trusted_tables()
        if not set(trusted).issubset(tables):
            raise ValueError("备份 SQLite 缺少必需的数据表")
        if any(_normalized_sql(tables[name] or "") != _normalized_sql(sql) for name, sql in trusted.items()):
            raise ValueError("备份 SQLite 表结构与可信版本不一致")
        for name, sql in tables.items():
            if sql and re.search(r"\bCREATE\s+VIRTUAL\s+TABLE\b", sql, re.I):
                if name != "message_fts" or not re.search(r"\bUSING\s+fts5\s*\(", sql, re.I):
                    raise ValueError("备份 SQLite 含不允许的虚拟表")
        checks = con.execute("PRAGMA integrity_check").fetchall()
        if checks != [("ok",)]:
            raise ValueError("备份 SQLite 损坏")
        versions = con.execute("SELECT version FROM schema_info").fetchall()
        if versions != [(SCHEMA_VERSION,)]:
            raise ValueError("备份数据库版本不兼容")
        if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("备份 SQLite 外键不一致")
        artifact_names = {"memory.sqlite3"}
        for digest, size in con.execute("SELECT hash,size FROM artifacts"):
            if not _digest(digest) or not isinstance(size, int) or not 0 <= size <= MAX_BLOB_BYTES:
                raise ValueError("备份数据库归档记录无效")
            name = f"archive/sha256/{digest[:2]}/{digest}"
            if hashes.get(name) != digest or archive.getinfo(name).file_size != size:
                raise ValueError("备份数据库与归档清单不一致")
            artifact_names.add(name)
        if set(hashes) != artifact_names:
            raise ValueError("备份清单含数据库未引用的归档")
    except (sqlite3.Error, AttributeError, KeyError) as exc:
        raise ValueError(f"备份 SQLite 验证失败: {exc}") from exc
    finally:
        con.close()


def _pause_restored_sources(database: Path) -> int:
    """Retain historical source labels/paths without authorizing future reads."""
    con = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)
    try:
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA journal_mode=MEMORY")
        report = json_text(
            {
                "status": "needs_confirmation",
                "restored": True,
                "reason": "Restored source paths are untrusted history; explicitly re-add each source before scanning.",
            }
        )
        # Do not SELECT, stat, open, enumerate, or otherwise resolve source paths.
        changed = con.execute("UPDATE sources SET last_run='',last_report=?", (report,)).rowcount
        con.commit()
        return changed
    finally:
        con.close()


def restore_backup(data_dir: Path, path: Path, password: str | None = None) -> dict:
    destination = data_dir.expanduser().absolute()
    if any(os.path.lexists(parent) and is_link(parent) for parent in (destination, *destination.parents)):
        raise ValueError("恢复目标不允许符号链接、junction 或 reparse 路径")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("只能恢复到全新的空目录；既有记忆库不会被覆盖")
    source = path.expanduser().resolve()
    with source.open("rb") as source_stream:
        magic = source_stream.read(len(MAGIC))
        source_stream.seek(0)
        encrypted = magic == MAGIC
        if encrypted:
            ciphertext = source_stream.read(MAX_ENCRYPTED_BYTES + len(MAGIC) + 45)
            archive_source = io.BytesIO(_decrypt(ciphertext, password))
        else:
            archive_source = source_stream
        try:
            with zipfile.ZipFile(archive_source) as archive:
                infos = archive.infolist()
                if len(infos) > 100000 or sum(info.file_size for info in infos) > MAX_RESTORE_BYTES:
                    raise ValueError("备份超出恢复大小或文件数上限")
                names = [info.filename for info in infos]
                if len(names) != len(set(name.casefold() for name in names)):
                    raise ValueError("备份中存在重复路径")
                for info in infos:
                    safe_relative(info.filename)
                    mode = info.external_attr >> 16
                    if (
                        "\\" in info.orig_filename
                        or info.orig_filename != info.filename
                        or info.is_dir()
                        or stat.S_ISLNK(mode)
                        or stat.S_IFMT(mode)
                        and not stat.S_ISREG(mode)
                    ):
                        raise ValueError("备份中不允许符号链接、目录或不安全路径")
                    limit = MAX_SQLITE_BYTES if info.filename == "memory.sqlite3" else MAX_BLOB_BYTES
                    if info.flag_bits & 1 or info.file_size > limit:
                        raise ValueError("备份单文件过大或使用不支持的 ZIP 加密")
                if "manifest.json" not in names or "memory.sqlite3" not in names:
                    raise ValueError("备份清单不完整")
                if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                    raise ValueError("备份清单超过大小上限")
                manifest = json.loads(archive.read("manifest.json"))
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("format") != "codex-memory-backup"
                    or manifest.get("schema_version") != SCHEMA_VERSION
                ):
                    raise ValueError("备份格式或版本不受支持")
                hashes = manifest.get("sha256")
                if not isinstance(hashes, dict) or set(names) != set(hashes) | {"manifest.json"}:
                    raise ValueError("备份清单不完整")
                for name, digest in hashes.items():
                    if not _digest(digest):
                        raise ValueError("备份清单哈希无效")
                    if name != "memory.sqlite3" and name != f"archive/sha256/{digest[:2]}/{digest}":
                        raise ValueError("备份清单含非归档文件")
                    with archive.open(name) as member:
                        observed_digest, observed_size = _stream_digest(
                            member, limit=MAX_SQLITE_BYTES if name == "memory.sqlite3" else MAX_BLOB_BYTES
                        )
                    if observed_digest != digest or observed_size != archive.getinfo(name).file_size:
                        raise ValueError(f"备份校验失败：{name}")
                raw = archive.read("memory.sqlite3")
                _validate_sqlite(raw, hashes, archive)
                # Only the final requested destination receives plaintext files,
                # after all hashes and database structure pass validation.
                if destination.exists() and any(destination.iterdir()):
                    raise ValueError("恢复目标在验证期间改变；不会覆盖既有文件")
                destination.mkdir(parents=True, exist_ok=True)
                for name in hashes:
                    target = destination / safe_relative(name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as member, target.open("xb") as output:
                        observed_digest, observed_size = _stream_digest(
                            member, output, MAX_SQLITE_BYTES if name == "memory.sqlite3" else MAX_BLOB_BYTES
                        )
                    if observed_digest != hashes[name] or observed_size != archive.getinfo(name).file_size:
                        raise ValueError(f"备份在恢复期间改变: {name}；已生成文件可能不完整")
                sources_disabled = _pause_restored_sources(destination / "memory.sqlite3")
        except (
            zipfile.BadZipFile,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            RuntimeError,
        ) as exc:
            raise ValueError(f"备份格式或内容无效: {exc}") from exc
        finally:
            if encrypted:
                archive_source.close()
    return {
        "data_dir": str(destination),
        "restored": len(hashes),
        "encrypted": encrypted,
        "collector_token_restored": False,
        "staging": "memory",
        "sources_disabled": sources_disabled,
        "sources_note": "恢复的来源标签和路径仅作为历史数据保留，自动扫描已暂停；用户检查后必须按相同类型、路径、账户标签和项目明确重新添加来源才能启用。跨账户可使用同一 data-dir。",
    }
