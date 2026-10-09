"""Read-only import of user-selected exports and local Codex rollout files.

The raw byte snapshots in ``ImportBatch.artifacts`` are exactly those parsed.
Imported strings (including system prompts and tool records) are untrusted data.
No archive is extracted to disk and no authentication or browser files are read.
"""

from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import stat
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

from .models import Conversation, ImportBatch, Message

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10000
MAX_COMPRESSION_RATIO = 2000
MAX_METADATA_BYTES = 16384
MAX_WARNINGS = 200
MAX_WARNING_CHARS = 1000
MAX_REFERENCES = 200
MAX_REFERENCE_NODES = 20000
MAX_JSONL_RECORD_BYTES = 8 * 1024 * 1024

_SENSITIVE = frozenset(
    {
        "auth.json",
        "auth",
        "credentials",
        "credentials.json",
        "credential",
        "secrets",
        "secrets.json",
        ".sandbox-secrets",
        "sandbox-secrets",
        "cookies",
        "cookies.json",
        "cookies.sqlite",
        "cookie",
        "login data",
        "browser",
        "browsers",
        "browser_profiles",
        "browser-profiles",
        "profiles",
        "profile",
        "default",
        "user data",
        "config.toml",
        ".env",
        ".git",
        "local state",
        "web data",
        "history",
        "session storage",
        "local storage",
    }
)
_DOCUMENT_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".bmp",
        ".tiff",
        ".pdf",
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        ".odt",
        ".ods",
        ".rtf",
        ".mp3",
        ".mp4",
        ".wav",
        ".ogg",
        ".m4a",
        ".flac",
        ".webm",
        ".mov",
        ".aac",
    }
)
_ATTACHMENT_CODE_SUFFIXES = frozenset(
    {
        ".json",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".css",
        ".ipynb",
        ".cpp",
        ".c",
        ".h",
        ".rs",
        ".go",
        ".java",
        ".sql",
        ".sh",
        ".ps1",
        ".zip",
    }
)
_ANCILLARY_EXPORTS = frozenset(
    {
        "chat.html",
        "message_feedback.json",
        "model_comparisons.json",
        "shared_conversations.json",
        "group_chats.json",
    }
)
_CHAT_HISTORY_NAME = re.compile(r"conversations(?:[-_]([0-9]+))?\.json", re.IGNORECASE | re.ASCII)


def _warn(batch: ImportBatch, message: str) -> None:
    # A corrupt million-line file must not produce a million-line report.
    if len(batch.warnings) < MAX_WARNINGS:
        message = _clean_text(message)
        if len(message) > MAX_WARNING_CHARS:
            suffix = " [warning truncated]"
            message = message[: max(0, MAX_WARNING_CHARS - len(suffix))] + suffix
        batch.warnings.append(message)
    else:
        batch.warnings_omitted += 1


def _sensitive(parts: tuple[str, ...] | list[str]) -> bool:
    for part in parts:
        folded = part.casefold()
        stem = PurePosixPath(folded).stem
        if (
            folded in _SENSITIVE
            or folded.startswith(".env.")
            or folded.startswith("auth.")
            or "sandbox-secret" in folded
            or stem
            in {
                "auth",
                "credentials",
                "secrets",
                "secret",
                "cookies",
                "tokens",
                "access_token",
                "refresh_token",
            }
            or folded.endswith((".key", ".pem", ".p12", ".pfx"))
        ):
            return True
    return False


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
        # Windows junctions and other reparse points also redirect reads.
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 1024)
    except OSError:
        return False


def _has_symlink(path: Path) -> bool:
    # Also reject a link in an ancestor, not just the final filename.
    return any(_is_link(part) for part in (path, *path.parents))


def _snapshot(path: Path, batch: ImportBatch) -> bytes | None:
    try:
        if _has_symlink(path):
            raise ValueError("symlink paths are not imported")
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not a regular file")
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError(f"exceeds {MAX_FILE_BYTES} byte file limit")
        # Read once: subsequent parsing must never reopen a changing source.
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
                raise ValueError("file identity changed while opening")
            blob = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(blob) > MAX_FILE_BYTES:
            raise ValueError("file grew past the byte limit")
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            _warn(batch, f"{path.name}: source changed during snapshot; retry sync to capture later data.")
        return blob
    except (OSError, ValueError) as exc:
        batch.skipped += 1
        _warn(batch, f"Skipped {path.name}: {exc}.")
        return None


def _metadata(value: Any) -> dict[str, Any]:
    """Bound serialized metadata without pretending the raw record was lost."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return {"raw_unindexed": True, "reason": "non-serializable metadata; see raw artifact"}
    # JSON can carry lone UTF-16 surrogates. Keep those bytes in the original
    # artifact, but replace invalid Unicode in indexed metadata for SQLite.
    blob = encoded.encode("utf-8", errors="replace")
    if len(blob) <= MAX_METADATA_BYTES:
        sanitized = json.loads(blob)
        return sanitized if isinstance(sanitized, dict) else {"raw": sanitized}
    bounded = {
        "raw_preview": blob[: MAX_METADATA_BYTES // 4].decode("utf-8", errors="replace"),
        "raw_sha256": sha256(blob).hexdigest(),
        "raw_bytes": len(blob),
        "raw_truncated": True,
        "raw_preserved_in_artifact": True,
    }
    if isinstance(value, dict):
        for key in ("artifact", "line", "node_id", "call_id", "current_node", "untrusted"):
            entry = value.get(key)
            if isinstance(entry, str):
                bounded[key] = _clean_text(entry)[:512]
            elif isinstance(entry, (bool, int)) or entry is None:
                bounded[key] = entry
        if isinstance(value.get("references"), list):
            bounded["references"] = [_clean_text(ref)[:256] for ref in value["references"][:10]]
            while len(json.dumps(bounded["references"], ensure_ascii=True)) > MAX_METADATA_BYTES // 4:
                bounded["references"].pop()
            bounded["references_truncated"] = len(bounded["references"]) < len(value["references"])
        if isinstance(value.get("attachment_links"), list):
            links: list[dict[str, Any]] = []
            for link in value["attachment_links"][:10]:
                if not isinstance(link, dict):
                    continue
                entry = {
                    key: _clean_text(link[key])[: (512 if key == "artifact" else 256)]
                    for key in ("reference", "status", "artifact", "sha256", "match_type")
                    if isinstance(link.get(key), str)
                }
                if len(json.dumps([*links, entry], ensure_ascii=True)) > MAX_METADATA_BYTES // 4:
                    break
                links.append(entry)
            bounded["attachment_links"] = links
            bounded["attachment_links_truncated"] = len(links) < len(value["attachment_links"])
    while (
        len(json.dumps(bounded, ensure_ascii=True).encode("utf-8")) > MAX_METADATA_BYTES
        and bounded["raw_preview"]
    ):
        bounded["raw_preview"] = bounded["raw_preview"][: len(bounded["raw_preview"]) // 2]
    return bounded


def _clean_text(value: Any) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _timestamp(value: Any) -> str:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return ""
        try:
            return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return ""
    return ""


def _sort_time(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _identifier(value: Any, fallback: str) -> str:
    return _clean_text(value) if isinstance(value, (str, int)) and str(value) else fallback


def _json(blob: bytes, name: str, batch: ImportBatch) -> Any:
    try:
        return json.loads(blob.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        batch.skipped += 1
        _warn(batch, f"{name}: invalid JSON ({exc}); original bytes preserved.")
        return None


def _text(value: Any, depth: int = 0) -> str:
    """Extract actual text and explicit media references, never fabricate media content."""
    if depth > 30:
        return "[Nested content retained in raw artifact]"
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _text(item, depth + 1)))
    if not isinstance(value, dict):
        return "" if value is None else str(value)
    parts: list[str] = []
    kind = value.get("content_type", value.get("type", ""))
    for key in ("text", "transcript", "caption"):
        if isinstance(value.get(key), (str, list, dict)):
            extracted = _text(value[key], depth + 1)
            if extracted:
                parts.append(extracted)
    for key in ("parts", "content", "summary", "output", "result"):
        if key in value:
            extracted = _text(value[key], depth + 1)
            if extracted and extracted not in parts:
                parts.append(extracted)
    for key in ("asset_pointer", "image_url", "audio_url", "file_id", "file_url"):
        pointer = value.get(key)
        if isinstance(pointer, dict):
            pointer = pointer.get("url") or pointer.get("asset_pointer")
        if isinstance(pointer, str):
            parts.append(f"[{kind or 'attachment'} reference: {pointer}]")
    for key in ("audio", "image", "video", "frames"):
        if isinstance(value.get(key), (dict, list)):
            extracted = _text(value[key], depth + 1)
            if extracted:
                parts.append(extracted)
    if "arguments" in value:
        arguments = value["arguments"]
        parts.append(arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False))
    if not parts and value:
        parts.append(f"[Unindexed {kind or 'structured'} content; preserved in raw artifact]")
    return "\n".join(parts)


def _references(value: Any, batch: ImportBatch | None = None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    # Iterator frames do not enqueue a whole million-item list. Each inspected
    # field/list item consumes the same bounded budget, including duplicates.
    pending = [iter((("", value, False),))]
    visited = 0
    truncated = False

    def list_rows(items: list[Any], attachments: bool):
        return (("", item, attachments) for item in items)

    def remember(ref: Any) -> None:
        nonlocal truncated
        if isinstance(ref, str) and ref not in seen:
            if len(found) >= MAX_REFERENCES:
                truncated = True
                return
            seen.add(ref)
            found.append(ref)

    while pending and not truncated:
        try:
            key, item, attachment_entry = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if visited >= MAX_REFERENCE_NODES:
            truncated = True
            break
        visited += 1
        if key in {"asset_pointer", "file_id", "file_url", "image_url", "audio_url"}:
            remember(item.get("url") if isinstance(item, dict) else item)
        if attachment_entry and isinstance(item, dict):
            remember(item.get("id") or item.get("name"))
        if isinstance(item, dict):
            pending.append(iter((name, child, False) for name, child in item.items()))
        elif isinstance(item, list):
            pending.append(list_rows(item, key == "attachments"))
    if truncated:
        if batch is not None:
            batch.coverage["references_truncated"] += 1
            _warn(batch, "Attachment reference inspection reached a per-message limit; raw content retained.")
    return found


def _history_name(name: str) -> bool:
    return _CHAT_HISTORY_NAME.fullmatch(PurePosixPath(name).name) is not None


def _export_order(name: str) -> tuple[Any, ...]:
    """Canonical history first, then natural numeric shards, without huge int conversion."""
    match = _CHAT_HISTORY_NAME.fullmatch(PurePosixPath(name).name)
    digits = ((match.group(1) or "").lstrip("0") or "0") if match else ""
    return (
        0 if match else 1,
        0 if match and match.group(1) is None else 1,
        len(digits),
        digits,
        name.casefold(),
        name,
    )


def _export_member(name: str) -> bool:
    path = PurePosixPath(name)
    if _sensitive(list(path.parts)):
        return False
    base = path.name.casefold()
    attachment = base.startswith("file-") or any(
        part.casefold() in {"attachments", "files", "assets"} for part in path.parts[:-1]
    )
    return (
        _history_name(name)
        or base in _ANCILLARY_EXPORTS
        or path.suffix.casefold() in _DOCUMENT_SUFFIXES
        or attachment
        and path.suffix.casefold() in _ATTACHMENT_CODE_SUFFIXES
    )


def _mime(name: str) -> str:
    if name.casefold().endswith(".jsonl"):
        return "application/x-ndjson"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _safe_zip_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or ":" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in {".", ".."} for part in path.parts)


def _chatgpt_inputs(path: Path, batch: ImportBatch) -> list[tuple[str, bytes]]:
    inputs: list[tuple[str, bytes]] = []
    total = 0

    def accept(name: str, blob: bytes) -> None:
        nonlocal total
        if total + len(blob) > MAX_TOTAL_BYTES:
            batch.skipped += 1
            _warn(batch, f"Skipped {name}: total archive byte limit exceeded.")
            return
        total += len(blob)
        batch.artifacts.append((f"chatgpt/{name}", blob, _mime(name)))
        if _history_name(name) or path.is_file() and path.suffix.casefold() == ".json":
            inputs.append((name, blob))

    if _has_symlink(path) or _sensitive(list(path.parts)):
        raise ValueError("Symlink or credential/browser import paths are not allowed.")
    if path.is_dir():
        # Only user-selected exports; credentials and browser dirs are pruned.
        for folder, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not _sensitive([d]) and not _is_link(Path(folder) / d))
            for name in sorted(files, key=_export_order):
                candidate = Path(folder) / name
                relative = candidate.relative_to(path).as_posix()
                if not _export_member(relative):
                    continue
                if _history_name(relative):
                    batch.coverage["chat_files_found"] += 1
                if len(batch.artifacts) >= MAX_ARCHIVE_ENTRIES:
                    _warn(batch, "Export entry limit reached; remaining files not imported.")
                    return sorted(inputs, key=lambda entry: _export_order(entry[0]))
                blob = _snapshot(candidate, batch)
                if blob is not None:
                    accept(relative, blob)
    elif path.is_file() and path.suffix.casefold() == ".zip":
        blob = _snapshot(path, batch)
        if blob is None:
            return inputs
        try:
            with zipfile.ZipFile(BytesIO(blob)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_ARCHIVE_ENTRIES:
                    raise ValueError("ZIP exceeds entry count limit")
                declared = sum(info.file_size for info in entries)
                if declared > MAX_TOTAL_BYTES:
                    raise ValueError("ZIP exceeds uncompressed total byte limit")
                names: set[str] = set()
                # Preserve the first entry among exact duplicate names. Other
                # member ordering no longer changes shard parsing order.
                for info in sorted(entries, key=lambda entry: _export_order(entry.filename)[:-1]):
                    if info.is_dir():
                        continue
                    name = info.filename
                    mode = info.external_attr >> 16
                    # zipfile normalizes backslashes on Windows; inspect the
                    # original central-directory name before that normalization.
                    if (
                        not _safe_zip_name(info.orig_filename)
                        or not _safe_zip_name(name)
                        or stat.S_ISLNK(mode)
                        or (stat.S_IFMT(mode) and not stat.S_ISREG(mode))
                    ):
                        batch.skipped += 1
                        _warn(batch, f"Skipped unsafe ZIP member: {name[:200]}.")
                        continue
                    if name.casefold() in names:
                        batch.skipped += 1
                        _warn(batch, f"Skipped duplicate ZIP member: {name[:200]}.")
                        continue
                    names.add(name.casefold())
                    if not _export_member(name):
                        batch.skipped += 1
                        continue
                    if _history_name(name):
                        batch.coverage["chat_files_found"] += 1
                    if (
                        info.file_size > MAX_FILE_BYTES
                        or info.flag_bits & 1
                        or info.file_size / max(1, info.compress_size) > MAX_COMPRESSION_RATIO
                    ):
                        batch.skipped += 1
                        _warn(batch, f"Skipped ZIP member exceeding limits or encrypted: {name[:200]}.")
                        continue
                    try:
                        with archive.open(info) as member:
                            content = member.read(MAX_FILE_BYTES + 1)
                        if len(content) > MAX_FILE_BYTES:
                            raise ValueError("expanded member exceeds byte limit")
                        accept(name, content)
                    except (
                        OSError,
                        RuntimeError,
                        ValueError,
                        zipfile.BadZipFile,
                        NotImplementedError,
                    ) as exc:
                        batch.skipped += 1
                        _warn(batch, f"{name[:200]}: cannot read ZIP member ({exc}).")
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise ValueError(f"Invalid or unsafe export ZIP: {exc}") from exc
    elif path.is_file() and path.suffix.casefold() == ".json" and not _sensitive([path.name]):
        batch.coverage["chat_files_found"] += 1
        blob = _snapshot(path, batch)
        if blob is not None:
            accept(path.name, blob)
    else:
        raise ValueError("Select an official ChatGPT ZIP, conversations JSON, or export directory.")
    return sorted(inputs, key=lambda entry: _export_order(entry[0]))


class _AttachmentIndex:
    """Associate pointers with archived bytes only; never open or download a pointer."""

    def __init__(self, batch: ImportBatch, history_inputs: list[tuple[str, bytes]]) -> None:
        self.batch = batch
        histories = {f"chatgpt/{name}" for name, _ in history_inputs}
        self.artifacts: dict[str, bytes] = {}
        self.paths: dict[str, set[str]] = {}
        self.names: dict[str, set[str]] = {}
        self.file_ids: dict[str, set[str]] = {}
        self.results: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
        self.digests: dict[str, str] = {}
        for name, blob, _ in batch.artifacts:
            path = PurePosixPath(name)
            if name in histories or path.name.casefold() in _ANCILLARY_EXPORTS:
                continue
            self.artifacts[name] = blob
            relative = name.removeprefix("chatgpt/")
            for alias in (name, relative):
                self.paths.setdefault(alias.casefold(), set()).add(name)
            self.names.setdefault(path.name.casefold(), set()).add(name)
            stem = path.stem.casefold()
            if stem.startswith("file-"):
                self.file_ids.setdefault(stem, set()).add(name)
                # Explicit export convention: file-<alphanumeric id>-<filename>.
                # A unique alias is required, and prefix collisions are never chosen.
                match = re.match(r"(file-[a-z0-9]+)-", stem)
                if match:
                    self.file_ids.setdefault(match.group(1), set()).add(name)
        batch.coverage["attachments_available"] = len(self.artifacts)

    @staticmethod
    def _local_key(reference: str) -> tuple[str, bool]:
        try:
            parsed = urlsplit(reference)
        except ValueError:
            return "", False
        if parsed.scheme:
            if parsed.scheme.casefold() != "file-service":
                return "", True
            key = unquote(parsed.netloc + parsed.path).lstrip("/")
        else:
            key = unquote(reference)
        return (key, False) if _safe_zip_name(key) else ("", False)

    def _lookup(self, key: str, origin: str, *, file_id: bool = True) -> tuple[set[str], str]:
        if not key:
            return set(), ""
        if "/" in key:
            # Qualified paths can disambiguate repeated basenames; an incorrect
            # qualified path must not fall back to a guessed basename.
            direct = self.paths.get(key.casefold(), set())
            joined = str(PurePosixPath(origin) / key)
            relative = self.paths.get(joined.casefold(), set())
            # If archive-root and export-relative interpretations disagree,
            # preserve the ambiguity instead of selecting by lookup priority.
            return set(direct) | set(relative), "path" if direct else "relative_path"
        candidates = self.names.get(key.casefold(), set())
        if candidates:
            return set(candidates), "filename"
        return (
            set(self.file_ids.get(key.casefold(), set())) if file_id else set(),
            "file_id_filename" if file_id else "attachment_name",
        )

    def resolve(self, reference: str, artifact: str, hints: tuple[str, ...] = ()) -> dict[str, Any]:
        origin = str(PurePosixPath(artifact.removeprefix("chatgpt/")).parent)
        cache_key = (reference, origin, hints)
        if cache_key in self.results:
            return self.results[cache_key]
        key, external = self._local_key(reference)
        result: dict[str, Any] = {"reference": reference}
        if external:
            status = "external"
        else:
            candidates, match_type = self._lookup(key, origin)
            had_primary = bool(candidates)
            for hint in hints:
                hint_key, hint_external = self._local_key(hint)
                if hint_external:
                    continue
                hinted, _ = self._lookup(hint_key, origin, file_id=False)
                if hinted:
                    candidates.update(hinted)
                    if not had_primary:
                        match_type = "attachment_name"
            if len(candidates) == 1:
                status = "matched"
                name = next(iter(candidates))
                if name not in self.digests:
                    self.digests[name] = sha256(self.artifacts[name]).hexdigest()
                result.update(artifact=name, sha256=self.digests[name], match_type=match_type)
            elif candidates:
                status = "ambiguous"
                result["candidate_count"] = len(candidates)
            else:
                status = "missing"
        result["status"] = status
        self.results[cache_key] = result
        self.batch.coverage[f"attachments_{status}"] += 1
        return result


def _attachment_hints(message: dict[str, Any]) -> tuple[dict[str, tuple[str, ...]], bool]:
    """Build ID/name evidence once, with entry, ID and per-ID name budgets."""
    containers = [message]
    if isinstance(message.get("metadata"), dict):
        containers.append(message["metadata"])
    hints: dict[str, set[str]] = {}
    scanned = 0
    truncated = False
    for container in containers:
        attachments = container.get("attachments")
        for attachment in attachments if isinstance(attachments, list) else []:
            if scanned >= MAX_REFERENCE_NODES:
                truncated = True
                break
            scanned += 1
            if not isinstance(attachment, dict):
                continue
            attachment_id = attachment.get("id")
            name = attachment.get("name")
            if not isinstance(attachment_id, str) or not isinstance(name, str):
                continue
            if attachment_id not in hints and len(hints) >= MAX_REFERENCES:
                truncated = True
                break
            names = hints.setdefault(attachment_id, set())
            if name not in names and len(names) >= MAX_REFERENCES:
                truncated = True
                break
            names.add(name)
        if truncated:
            break
    return {key: tuple(sorted(names)) for key, names in hints.items()}, truncated


def _chatgpt_conversation(
    raw: dict[str, Any],
    index: int,
    artifact: str,
    account: str,
    project: str,
    batch: ImportBatch,
    attachments_index: _AttachmentIndex,
) -> Conversation:
    conv_id = _identifier(
        raw.get("id") or raw.get("conversation_id"),
        "export-" + sha256(json.dumps(raw, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:24],
    )
    conv = Conversation(
        id=conv_id,
        source="chatgpt",
        account=account,
        title=_clean_text(raw.get("title") or "Untitled ChatGPT conversation"),
        created_at=_timestamp(raw.get("create_time")),
        updated_at=_timestamp(raw.get("update_time")),
        project=project,
    )
    mapping = raw.get("mapping")
    node_rows: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    if isinstance(mapping, dict):
        for node_id, node in mapping.items():
            if not isinstance(node, dict):
                batch.skipped += 1
                _warn(batch, f"{conv_id}: malformed mapping node {node_id}.")
                continue
            msg = node.get("message")
            if msg is None:
                continue
            if not isinstance(msg, dict):
                batch.skipped += 1
                _warn(batch, f"{conv_id}: malformed message in node {node_id}.")
                continue
            node_rows.append((str(node_id), node, msg))
    elif isinstance(raw.get("messages"), list):
        _warn(batch, f"{conv_id}: flat messages format; branch coverage cannot be verified.")
        for i, msg in enumerate(raw["messages"]):
            if isinstance(msg, dict):
                node_rows.append((_identifier(msg.get("id"), f"message-{i}"), {}, msg))
            else:
                batch.skipped += 1
    else:
        _warn(batch, f"{conv_id}: no supported message mapping; original conversation retained.")
    node_messages = {node_id: _identifier(msg.get("id"), node_id) for node_id, _, msg in node_rows}
    refs: list[str] = []
    seen_ids: set[str] = set()
    for node_id, node, msg in node_rows:
        msg_id = node_messages[node_id]
        if msg_id in seen_ids:
            _warn(batch, f"{conv_id}: duplicate message id {msg_id}; disambiguated by branch node.")
            msg_id = f"{msg_id}:node:{node_id}"
        seen_ids.add(msg_id)
        author = msg.get("author") if isinstance(msg.get("author"), dict) else {}
        role = str(author.get("role") or msg.get("role") or "unknown")
        parent = node.get("parent", msg.get("parent_id"))
        if parent is not None:
            parent = str(parent)
            if isinstance(mapping, dict) and parent not in mapping:
                _warn(batch, f"{conv_id}: missing parent node {parent}; branch graph incomplete.")
            parent = node_messages.get(parent, parent)
        content = msg.get("content", msg.get("text", ""))
        extracted = _text(content)
        attachments = (
            (msg.get("metadata") or {}).get("attachments", [])
            if isinstance(msg.get("metadata"), dict)
            else []
        )
        for attachment in attachments if isinstance(attachments, list) else []:
            if isinstance(attachment, dict):
                extracted += f"\n[Attachment: {attachment.get('name') or attachment.get('id') or 'unnamed'}; bytes only if included in export]"
        previously_truncated = batch.coverage["references_truncated"]
        msg_refs = _references(msg, batch)
        hints, hints_truncated = _attachment_hints(msg)
        if hints_truncated and batch.coverage["references_truncated"] == previously_truncated:
            batch.coverage["references_truncated"] += 1
            _warn(
                batch,
                "Attachment name evidence inspection reached a per-message limit; raw content retained.",
            )
        attachment_links = []
        for ref in msg_refs:
            key, _ = _AttachmentIndex._local_key(ref)
            explicit_names = tuple(sorted(set(hints.get(ref, ())) | set(hints.get(key, ()))))
            attachment_links.append(attachments_index.resolve(ref, artifact, explicit_names))
        refs.extend(ref for ref in msg_refs if ref not in refs)
        content_type = content.get("content_type", "message") if isinstance(content, dict) else "message"
        kind = "tool" if role == "tool" else str(content_type or "message")
        metadata = _metadata(
            {
                "raw_message": msg,
                "node_id": node_id,
                "children": node.get("children", []),
                "artifact": artifact,
                "references": msg_refs,
                "attachment_links": attachment_links,
                "current_node": node_id == str(raw.get("current_node", "")),
                "untrusted": True,
            }
        )
        conv.messages.append(
            Message(
                id=msg_id,
                role=_clean_text(role),
                text=_clean_text(extracted),
                timestamp=_timestamp(msg.get("create_time", msg.get("timestamp"))),
                parent_id=_clean_text(parent) if parent is not None else None,
                branch=_clean_text(node_id),
                kind=_clean_text(kind),
                metadata=metadata,
            )
        )
    conv.messages.sort(key=lambda message: _sort_time(message.timestamp))
    if not conv.created_at and conv.messages:
        conv.created_at = conv.messages[0].timestamp
    if not conv.updated_at and conv.messages:
        conv.updated_at = conv.messages[-1].timestamp
    conv.metadata = _metadata(
        {
            "artifact": artifact,
            "current_node": raw.get("current_node"),
            "all_exported_branches": isinstance(mapping, dict),
            "references": refs,
            "untrusted": True,
            "export_metadata": {
                key: value for key, value in raw.items() if key not in {"mapping", "messages"}
            },
        }
    )
    return conv


def parse_chatgpt(path: Path, account: str, project: str = "") -> ImportBatch:
    """Import an official export without extracting files or calling cloud services."""
    path = Path(path).absolute()
    batch = ImportBatch()
    batch.coverage = {
        "scope": "provided_export_only",
        "status": "no_chat_history",
        "chat_files_found": 0,
        "chat_files_parsed": 0,
        "chat_files_unsupported": 0,
        "attachments_available": 0,
        "attachments_matched": 0,
        "attachments_missing": 0,
        "attachments_ambiguous": 0,
        "attachments_external": 0,
        # Number of message records whose reference inspection hit a limit;
        # the unknown number of uninspected references is never invented.
        "references_truncated": 0,
    }
    inputs = _chatgpt_inputs(path, batch)
    attachments_index = _AttachmentIndex(batch, inputs)
    incomplete = bool(batch.warnings or batch.warnings_omitted)
    for name, blob in inputs:
        raw = _json(blob, name, batch)
        if raw is None:
            batch.coverage["chat_files_unsupported"] += 1
            continue
        if isinstance(raw, list):
            records = raw
        elif isinstance(raw, dict) and isinstance(raw.get("conversations"), list):
            records = raw["conversations"]
        elif isinstance(raw, dict) and (
            isinstance(raw.get("mapping"), dict) or isinstance(raw.get("messages"), list)
        ):
            records = [raw]
        else:
            batch.coverage["chat_files_unsupported"] += 1
            _warn(batch, f"{name}: unsupported export schema; raw JSON retained.")
            continue
        recognized = 0
        for index, record in enumerate(records):
            if not isinstance(record, dict) or not (
                isinstance(record.get("mapping"), dict) or isinstance(record.get("messages"), list)
            ):
                batch.skipped += 1
                incomplete = True
                _warn(
                    batch,
                    f"{name}: conversation {index} has no supported message mapping; raw JSON retained.",
                )
                continue
            recognized += 1
            before = len(batch.warnings) + batch.warnings_omitted
            batch.conversations.append(
                _chatgpt_conversation(
                    record, index, f"chatgpt/{name}", account, project, batch, attachments_index
                )
            )
            if len(batch.warnings) + batch.warnings_omitted > before:
                incomplete = True
        if recognized or not records:
            batch.coverage["chat_files_parsed"] += 1
        else:
            batch.coverage["chat_files_unsupported"] += 1
            _warn(batch, f"{name}: unsupported export schema; no recognizable conversations in JSON.")
    missing = batch.coverage["attachments_missing"]
    ambiguous = batch.coverage["attachments_ambiguous"]
    external = batch.coverage["attachments_external"]
    if missing:
        _warn(
            batch,
            f"{missing} attachment/media references have no matching local bytes; text references preserved.",
        )
    if ambiguous:
        _warn(
            batch,
            f"{ambiguous} attachment references match multiple local artifacts; no ambiguous link was selected.",
        )
    if external:
        _warn(
            batch, f"{external} external media references retained as pointers; no remote bytes downloaded."
        )
    if any(conv.messages for conv in batch.conversations):
        batch.coverage["status"] = (
            "partial"
            if incomplete
            or batch.coverage["chat_files_parsed"] != batch.coverage["chat_files_found"]
            or missing
            or ambiguous
            or external
            or batch.coverage["references_truncated"]
            else "complete"
        )
    else:
        _warn(
            batch,
            "No recognizable ChatGPT message history was imported; select conversations.json or numeric conversations-1.json / conversations_1.json shards with supported content.",
        )
    return batch


def _codex_paths(path: Path, batch: ImportBatch) -> list[tuple[Path, str]]:
    if _has_symlink(path) or _sensitive(list(path.parts)):
        raise ValueError("Symlink or credential/browser import paths are not allowed.")
    if path.is_file():
        if path.suffix.casefold() != ".jsonl" or _sensitive(list(path.parts)):
            raise ValueError("Codex imports accept a rollout JSONL or a Codex sessions root only.")
        return [(path, path.name)]
    if not path.is_dir():
        raise ValueError("Codex source does not exist.")
    roots: list[Path] = []
    if path.name.casefold() in {"sessions", "archived_sessions"}:
        roots.append(path)
    else:
        roots.extend(
            candidate
            for name in ("sessions", "archived_sessions")
            if (candidate := path / name).is_dir() and not _is_link(candidate)
        )
    if not roots:
        _warn(
            batch,
            "No sessions or archived_sessions directory found; no other Codex directories were scanned.",
        )
        return []
    paths: list[tuple[Path, str]] = []
    for root in roots:
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not _sensitive([d]) and not _is_link(Path(folder) / d))
            for name in sorted(files):
                candidate = Path(folder) / name
                if candidate.suffix.casefold() != ".jsonl" or _sensitive([name]):
                    continue
                if _is_link(candidate):
                    batch.skipped += 1
                    _warn(batch, f"Skipped symlink session {name}.")
                    continue
                relative = (
                    candidate.relative_to(path).as_posix()
                    if path != root
                    else f"{root.name}/{candidate.relative_to(root).as_posix()}"
                )
                paths.append((candidate, relative))
    return paths


def _codex_record_text(payload: dict[str, Any]) -> tuple[str, str, str]:
    kind = str(payload.get("type") or "record")
    if kind == "message":
        return (
            str(payload.get("role") or "unknown"),
            _text(payload.get("content", payload.get("text", ""))),
            "message",
        )
    if kind in {"function_call", "custom_tool_call", "local_shell_call", "computer_call", "web_search_call"}:
        value = payload.get("arguments", payload.get("input", payload.get("action", "")))
        return (
            "assistant",
            _text(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            kind,
        )
    if kind in {"function_call_output", "custom_tool_call_output", "tool_result"}:
        output = payload.get("output", payload.get("result", ""))
        if isinstance(output, (dict, list)):
            # JSON objects are genuine tool output, not invented prose.
            return "tool", json.dumps(output, ensure_ascii=False), kind
        return "tool", _text(output), kind
    if kind == "reasoning":
        return "assistant", _text(payload.get("summary", payload.get("content", ""))), kind
    return str(payload.get("role") or "system"), _text(payload), kind


def _event_message(payload: dict[str, Any]) -> tuple[str, str] | None:
    kind = payload.get("type")
    if kind == "user_message":
        return "user", _text(payload.get("message", payload.get("text", "")))
    if kind in {"agent_message", "assistant_message"}:
        return "assistant", _text(payload.get("message", payload.get("text", "")))
    return None


def _dedup_key(role: str, text: str) -> tuple[str, str]:
    return role, " ".join(text.split())


def _plain_text(value: Any, depth: int = 0) -> str:
    """Text-only comparison ignores generated media pointer annotations."""
    if depth > 30:
        return ""
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return "\n".join(_plain_text(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return "\n".join(
            _plain_text(value[key], depth + 1)
            for key in ("text", "transcript", "caption", "parts", "content")
            if key in value
        )
    return ""


def _cwd_project(cwd: str) -> str:
    """Infer a portable display label; the source path is never opened."""
    if not cwd:
        return ""
    windows = PureWindowsPath(cwd)
    path = windows if windows.drive or "\\" in cwd else PurePosixPath(cwd)
    # A root/drive has no basename. Labels cannot contain control characters.
    return "".join(character for character in path.name if ord(character) >= 32).strip()[:256]


def suggest_codex_title(texts: list[str], fallback: str = "") -> str:
    """Choose the first actual user request, ignoring clear runtime envelopes."""
    envelopes = (
        "# agents.md",
        "<environment_context>",
        "<instructions>",
        "<turn_aborted>",
        "<user_instructions>",
        "<permissions instructions>",
        "<collaboration_mode>",
        "<skills_instructions>",
        "<plugins_instructions>",
        "<app-context>",
        "<codex_apps_client_time_context>",
        "<external_codex_apps_open_page>",
    )
    for text in texts:
        if not isinstance(text, str):
            continue
        text = _clean_text(text).strip()
        if text and not text.casefold().startswith(envelopes):
            return " ".join(text.split())[:120]
    return fallback


def _parse_codex_snapshot(
    blob: bytes, name: str, account: str, project: str, batch: ImportBatch
) -> Conversation:
    records: list[tuple[int, dict[str, Any]]] = []
    lines = blob.splitlines(keepends=True)
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if len(line) > MAX_JSONL_RECORD_BYTES:
            batch.skipped += 1
            _warn(batch, f"{name}:{number}: record exceeds byte limit; raw bytes retained.")
            continue
        try:
            raw = json.loads(line.decode("utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("record is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            batch.skipped += 1
            partial = number == len(lines) and not line.endswith((b"\n", b"\r"))
            _warn(
                batch,
                f"{name}:{number}: {'truncated final record, retry on next sync' if partial else 'malformed JSONL record'} ({exc}); raw bytes retained.",
            )
            continue
        records.append((number, raw))
    meta = next(
        (
            record.get("payload")
            for _, record in records
            if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict)
        ),
        {},
    )
    fallback = Path(name).stem
    cwd = _clean_text(meta.get("cwd") or "")
    inferred_project = _cwd_project(cwd) if not project else ""
    conv = Conversation(
        id=_identifier(meta.get("id") or meta.get("session_id"), fallback),
        source="codex",
        account=account,
        title=_clean_text(meta.get("title") or fallback),
        created_at=_timestamp(meta.get("timestamp")),
        project=project or inferred_project,
        cwd=cwd,
    )
    # Pair event/response occurrences one-for-one. Use the earlier record's id
    # so an event-only fallback keeps its identity when a later sync sees the
    # response_item. The append-only store must not accumulate duplicate turns.
    response_entries: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    event_entries: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for number, record in records:
        payload = record.get("payload")
        if (
            record.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "message"
        ):
            role = str(payload.get("role") or "unknown")
            key = _dedup_key(role, _plain_text(payload.get("content", payload.get("text", ""))))
            response_entries.setdefault(key, []).append((number, record))
        elif (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and (event := _event_message(payload)) is not None
        ):
            role, _ = event
            key = _dedup_key(role, _plain_text(payload.get("message", payload.get("text", ""))))
            event_entries.setdefault(key, []).append((number, record))
    paired_events: set[int] = set()
    response_origins: dict[int, tuple[int, dict[str, Any]]] = {}
    for key, responses in response_entries.items():
        for response, event in zip(responses, event_entries.get(key, []), strict=False):
            paired_events.add(event[0])
            response_origins[response[0]] = min(response, event, key=lambda row: row[0])
    seen_ids: set[str] = set()
    for number, record in records:
        record_type = str(record.get("type") or "unknown")
        payload = record.get("payload")
        if record_type == "session_meta":
            continue
        if not isinstance(payload, dict):
            payload = {"raw_payload": payload}
        timestamp = _timestamp(record.get("timestamp", payload.get("timestamp")))
        if record_type == "response_item":
            role, text, kind = _codex_record_text(payload)
        elif record_type == "event_msg" and (event := _event_message(payload)) is not None:
            role, text = event
            if number in paired_events:
                continue
            kind = "event_fallback"
        else:
            role = "system"
            kind = "record"
            text = f"[Codex {record_type}/{payload.get('type', 'unknown')} record; preserved in raw artifact]"
        origin_number, origin_record = response_origins.get(number, (number, record))
        origin_payload = origin_record.get("payload")
        msg_id = _identifier(
            origin_payload.get("id") if isinstance(origin_payload, dict) else None, f"line-{origin_number}"
        )
        if msg_id in seen_ids:
            _warn(batch, f"{name}:{number}: duplicate message id disambiguated by record line.")
            msg_id = f"{msg_id}:line:{number}"
        seen_ids.add(msg_id)
        # Preserve tool call ids as correlation fields, not message identifiers;
        # a call and its output usually share a call_id.
        metadata = _metadata(
            {
                "raw_record": record,
                "artifact": f"codex/{name}",
                "line": number,
                "call_id": payload.get("call_id"),
                "untrusted": True,
            }
        )
        conv.messages.append(
            Message(
                id=msg_id,
                role=_clean_text(role),
                text=_clean_text(text),
                timestamp=timestamp,
                kind=_clean_text(kind),
                metadata=metadata,
            )
        )
    conv.messages.sort(key=lambda message: _sort_time(message.timestamp))
    if not conv.created_at and records:
        conv.created_at = _timestamp(records[0][1].get("timestamp"))
    if records:
        conv.updated_at = _timestamp(records[-1][1].get("timestamp"))
    if not meta.get("title"):
        conv.title = suggest_codex_title(
            [message.text for message in conv.messages if message.role == "user"], fallback
        )
    conv.metadata = _metadata(
        {
            "session_meta": meta,
            "artifact": f"codex/{name}",
            "valid_records": len(records),
            "project_inferred_from_cwd": bool(inferred_project),
            "untrusted": True,
        }
    )
    if not meta:
        _warn(batch, f"{name}: session_meta missing; conversation id falls back to filename.")
    return conv


def codex_files(path: Path) -> list[Path]:
    """Safely enumerate the same allowlisted rollout files used by the importer."""
    return [candidate for candidate, _ in _codex_paths(Path(path).absolute(), ImportBatch())]


def parse_codex(
    path: Path, account: str, project: str = "", *, only_paths: set[Path] | None = None
) -> ImportBatch:
    """Read only sessions/archived_sessions or one explicitly selected JSONL."""
    path = Path(path).absolute()
    batch = ImportBatch()
    total = 0
    selected = {Path(candidate).absolute() for candidate in only_paths} if only_paths is not None else None
    for candidate, name in _codex_paths(path, batch):
        if selected is not None and candidate not in selected:
            continue
        if len(batch.artifacts) >= MAX_ARCHIVE_ENTRIES:
            _warn(batch, "Codex file count limit reached; remaining files not imported.")
            break
        blob = _snapshot(candidate, batch)
        if blob is None:
            continue
        if total + len(blob) > MAX_TOTAL_BYTES:
            batch.skipped += 1
            _warn(batch, f"Skipped {name}: total import byte limit exceeded.")
            continue
        total += len(blob)
        batch.artifacts.append((f"codex/{name}", blob, "application/x-ndjson"))
        batch.conversations.append(_parse_codex_snapshot(blob, name, account, project, batch))
    if not project and any(conv.project for conv in batch.conversations):
        _warn(
            batch,
            "Project labels were inferred from cwd directory basenames; different paths with the same basename share a label. Set an explicit project label to distinguish them.",
        )
    _warn(
        batch,
        "Codex coverage includes local rollout files only; cloud-only chats, attachments outside rollouts and account settings are not imported.",
    )
    return batch
