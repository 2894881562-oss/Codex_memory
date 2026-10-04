"""Bounded, deterministic keyword evidence for handoffs; no model or embeddings.

This is a selector over the existing index, not an inferred user biography or
an automatic replacement for a manually confirmed checkpoint.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

from codex_memory.security import redact

_WORDS = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*|[\u3400-\u9fff]+")
_STOP = frozenset(
    "the and for with from this that into please continue project 当前 项目 继续 工作 完成 需要 进行 这个 本次 可以 以及 然后 我们 我的 希望 能够 实现 功能".split()
)
_SIGNALS = (
    "决定",
    "约定",
    "必须",
    "禁止",
    "不要",
    "偏好",
    "选择",
    "保留",
    "改为",
    "decision",
    "prefer",
    "must",
    "never",
)
_ENVELOPES = (
    "# agents.md",
    "<environment_context>",
    "<instructions>",
    "<user_instructions>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<app-context>",
)


def keywords(text: str, limit: int = 12) -> list[str]:
    """Literal terms plus Chinese character n-grams, not semantic segmentation.

    Space-separated task keywords are the most predictable input. Long Chinese
    runs get overlapping 3/2-character windows, sampled over at most 256 chars.
    """
    exact, grams, short = [], [], []
    for token in _WORDS.findall(text[:2048]):
        folded = token.casefold()
        if folded in _STOP or len(token) < 2:
            continue
        if token.isascii() or len(token) <= 6:
            exact.append(folded)
        if not token.isascii() and len(token) > 2:
            token = token[:256]
            for size, output in ((3, grams), (2, short)):
                positions = list(range(len(token) - size + 1))
                # Bound extraction even for a checkpoint containing one huge run.
                if len(positions) > 16:
                    positions = [round(i * (len(positions) - 1) / 15) for i in range(16)]
                output.extend(token[i : i + size] for i in positions)
    result = []
    # Keep both indexed terms and short Chinese terms instead of letting grams
    # consume the whole selection budget before any two-character term is seen.
    for group in (exact, grams[: max(1, limit // 2)], short):
        for term in group:
            if term not in _STOP and term not in result:
                result.append(term)
            if len(result) == limit:
                return result
    return result


def anchors(task: str, checkpoint: dict | None, fallback: str = "") -> dict[str, float]:
    """Explicit task outranks checkpoint next steps, decisions and goal."""
    result: dict[str, float] = {}
    groups = [(task, 6.0)]
    if checkpoint:
        groups += [
            (checkpoint[key], weight)
            for key, weight in (("next_steps", 3.0), ("decisions", 2.5), ("goal", 2.0))
        ]
    elif not task:
        groups.append((fallback, 1.0))
    for text, weight in groups:
        for term in keywords(text, 8 if task and weight < 6 else 12):
            result[term] = max(result.get(term, 0), weight)
    # SQL expression size and scan work are bounded, independently of input size.
    return dict(sorted(result.items(), key=lambda item: -item[1])[:24])


def excerpt(text: str, terms: dict[str, float], size: int = 900) -> tuple[str, int]:
    text = redact(text)
    folded = text.casefold()
    positions = [(weight, folded.find(term), term) for term, weight in terms.items() if term in folded]
    position = max(positions, default=(0, 0, ""), key=lambda item: (item[0], len(item[2]), -item[1]))[1]
    start = max(0, position - min(160, size // 4))
    return text[start : start + size], start


def _score(text: str, terms: dict[str, float], role: str = "") -> float:
    folded = text.casefold()
    matches = sum(weight for term, weight in terms.items() if term in folded)
    if not matches:
        return 0
    signals = sum(cue in folded for cue in _SIGNALS)
    explicit = any(weight >= 6 and term in folded for term, weight in terms.items())
    return (
        matches
        + (100 if explicit else 0)
        + min(3, signals) * 1.5
        + {"user": 50, "assistant": 4, "tool": -10}.get(role, 0)
    )


def select_evidence(
    con: sqlite3.Connection,
    terms: dict[str, float],
    project: str = "",
    conversation_id: str = "",
    limit: int = 8,
) -> list[dict]:
    """Union indexed OR retrieval and a bounded short-term fallback, then rerank.

    SQLite selects a bounded source window rather than materializing entire long
    tool messages in Python. Candidate limits are a recall tradeoff, not a claim
    that all relevant historical decisions have been found.
    """
    if not terms:
        return []
    clauses, filters = ["1=1"], []
    if project:
        clauses.append("c.project=?")
        filters.append(project)
    if conversation_id:
        clauses.append("c.id=?")
        filters.append(conversation_id)
    fts = con.execute("SELECT 1 FROM sqlite_master WHERE name='message_fts'").fetchone()
    long_terms = [term for term in terms if len(term) >= 3]
    short_terms = [term for term in terms if len(term) < 3][:6]
    # Locate the highest-priority matching literal term, including matches near
    # the end of a message. SQLite LIKE/LOWER case handling is ASCII-oriented.
    location = "coalesce(" + ",".join("nullif(instr(lower(m.text),?),0)" for _ in terms) + ",1)"
    columns = (
        "m.id AS message_id,m.conversation_id,substr(m.role,1,16) AS role,substr(m.timestamp,1,64) AS timestamp,"
        "substr(m.branch,1,128) AS branch,length(m.branch) AS branch_chars,m.ordinal,substr(m.kind,1,32) AS kind,"
        "substr(c.source,1,20) AS source,substr(c.account,1,256) AS account,substr(c.project,1,256) AS project,"
        "substr(m.text,1,100) AS lead,length(m.text) AS total_chars,"
        f"max(0,{location}-161) AS text_offset,substr(m.text,max(1,{location}-160),1600) AS text"
    )
    window_values = list(terms) * 2
    candidates: dict[str, dict] = {}
    queries = []
    if fts and long_terms:
        explicit = [term for term in long_terms if terms[term] >= 6]
        for group in [explicit, long_terms] if explicit and explicit != long_terms else [long_terms]:
            match = " OR ".join('"' + term.replace('"', '""') + '"' for term in group)
            queries.append(
                (
                    " JOIN message_fts f ON f.message_id=m.id",
                    "message_fts MATCH ?",
                    [match],
                    "bm25(message_fts),c.updated_at DESC,m.id",
                    48,
                )
            )
    scan_terms = short_terms if fts else list(terms)[:12]
    explicit_scan = [term for term in scan_terms if terms[term] >= 6]
    if explicit_scan:
        scan_terms = explicit_scan
    if scan_terms:
        like = " OR ".join("m.text LIKE ? ESCAPE '\\'" for _ in scan_terms)
        escaped = [
            "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for term in scan_terms
        ]
        queries.append(
            (
                "",
                "(" + like + ")",
                escaped,
                "CASE m.role WHEN 'user' THEN 0 WHEN 'assistant' THEN 1 ELSE 2 END,c.updated_at DESC,m.ordinal DESC,m.id",
                48,
            )
        )
    # Conversation text is primary. Tool outputs are evidence fallback only;
    # never package private reasoning, developer envelopes or opaque status logs.
    for category in ("m.kind='message' AND m.role IN ('user','assistant')", "m.role='tool'"):
        for join, condition, values, order, cap in queries:
            rows = con.execute(
                "SELECT "
                + columns
                + " FROM messages m JOIN conversations c ON c.id=m.conversation_id"
                + join
                + " WHERE "
                + " AND ".join([*clauses, category, condition])
                + " ORDER BY "
                + order
                + " LIMIT ?",
                [*window_values, *filters, *values, cap],
            ).fetchall()
            for rank, row in enumerate(rows):
                item = dict(row)
                lead = item.pop("lead").strip().casefold()
                item["text"] = redact(item["text"])
                item["score"] = _score(item["text"], terms, item["role"]) + 1 / (rank + 1)
                if lead.startswith(_ENVELOPES):
                    item["score"] -= 50
                item["matched_terms"] = [term for term in terms if term in item["text"].casefold()]
                if not item["matched_terms"]:
                    continue
                old = candidates.get(item["message_id"])
                if old is None or item["score"] > old["score"]:
                    candidates[item["message_id"]] = item
        if candidates:
            break
    ordered = sorted(candidates.values(), key=lambda item: (-item["score"], item["message_id"]))
    selected, counts, seen = [], {}, set()
    for item in ordered:
        cid = item["conversation_id"]
        digest = hashlib.sha256((item["role"] + "\n" + " ".join(item["text"].split())).encode()).hexdigest()
        if counts.get(cid, 0) >= 3 or digest in seen:
            continue
        selected.append(item)
        seen.add(digest)
        counts[cid] = counts.get(cid, 0) + 1
        if len(selected) == limit:
            break
    return selected


def context_neighbors(con: sqlite3.Connection, hit: dict) -> list[dict]:
    """One adjacent record each side, same known branch; not a causal-chain claim."""
    if hit.get("branch_chars", len(hit["branch"])) > len(hit["branch"]):
        return []
    rows = con.execute(
        "SELECT id,substr(role,1,16) AS role,substr(branch,1,128) AS branch,ordinal,substr(text,1,260) AS text,length(text) AS total_chars FROM messages "
        "WHERE conversation_id=? AND branch=? AND ordinal BETWEEN ? AND ? AND id<>? AND kind='message' AND role IN ('user','assistant') ORDER BY ordinal,id LIMIT 2",
        (hit["conversation_id"], hit["branch"], hit["ordinal"] - 1, hit["ordinal"] + 1, hit["message_id"]),
    ).fetchall()
    return [{**dict(row), "text": redact(row["text"])} for row in rows]


def relevant_checkpoints(
    con: sqlite3.Connection, project: str, latest_id: str, terms: dict[str, float]
) -> list[dict]:
    if not project or not terms:
        return []
    rows = con.execute(
        "SELECT id,created_at,account,substr(goal,1,2048) AS goal,substr(decisions,1,4096) AS decisions,"
        "substr(next_steps,1,2048) AS next_steps FROM checkpoints WHERE project=? AND id<>? ORDER BY created_at DESC,id DESC LIMIT 100",
        (project, latest_id),
    ).fetchall()
    selected = []
    for row in rows:
        item = dict(row)
        text = "\n".join(item[key] for key in ("goal", "decisions", "next_steps"))
        item["score"] = _score(text, terms)
        if item["score"]:
            item["text"], _ = excerpt(text, terms, 600)
            selected.append(item)
    return sorted(selected, key=lambda item: (-item["score"], item["id"]))[:2]


class TextBudget:
    """Prioritized sections with a reserved safety footer and visible truncation."""

    def __init__(self, max_chars: int, footer: str):
        self.capacity = max_chars - len(footer) - 2
        self.footer = footer
        self.parts: list[str] = []

    @property
    def remaining(self) -> int:
        return self.capacity - sum(len(part) + 2 for part in self.parts)

    def add(self, text: str, cap: int | None = None) -> None:
        available = min(self.remaining - 2, cap if cap is not None else self.remaining - 2)
        if available < 30:
            return
        if len(text) > available:
            marker = "\n[摘录已截断]"
            text = text[: available - len(marker)] + marker
        self.parts.append(text)

    def finish(self) -> str:
        return "\n\n".join([*self.parts, self.footer])
