# Engineering contract

The product archives and reuses context across accounts; it does not recreate OpenAI account history or grant cloud permissions. No model API calls. Python 3.11+, optional MCP SDK and cryptography for encrypted backup.

## Modules and ownership during initial build

- `models.py`: Conversation, Message, ImportBatch, ImportReport data contract.
- `importers.py` (importer agent): `parse_chatgpt(path: Path, account: str, project: str = "") -> ImportBatch`; `parse_codex(path: Path, account: str, project: str = "") -> ImportBatch`. Accept official ChatGPT ZIP/JSON/directory and explicit Codex root/JSONL. Preserve all branches, tool records, unknown raw data; never scan auth/config/browser/secrets. Retain raw allowlisted inputs in batch.artifacts. Report missing/malformed/truncated records. Do not mutate inputs.
- `store.py`, `service.py`, `security.py`, `backup.py` (root): persistence, indexing, queries, handoff and backup. Stable `MemoryService(data_dir: Path)` API below.
- `cli.py`, `server.py`, `mcp_server.py`, `web/` (interface agent): CLI, loopback UI, official SDK MCP.
- `README.md`, `docs/`, `scripts/`, `examples/` (documentation agent): Chinese operator docs, Windows bootstrapping, portable package and deployment examples, independent contract verification.

## MemoryService public contract

All return JSON-serializable dict/list/string, unless stated. Each operation opens its own SQLite connection (thread safe). `data_dir` property is an absolute Path. `db_path` points at `<data_dir>/memory.sqlite3`.

- `initialize() -> dict`: schema/version/status; idempotent.
- `stats() -> dict`: conversations, messages, artifacts, projects, checkpoints, accounts, db_path. Additive `conversation_sources` and `chatgpt_archive` distinguish latest ingestion from latest export diagnostics; coverage is limited to supplied files and does not imply cloud-account completeness. HTTP adds current server `runtime`, `sync` and `live` state.
- `import_chatgpt(path, account="default", project="") -> dict`: report.
- `import_codex(path=None, account="default", project="") -> dict`: default Path.home()/'.codex', report.
- `list_conversations(source="", account="", project="", limit=50, offset=0) -> list[dict]`: rows include id (internal stable key), title, source, account, project, updated_at, message_count.
- `fetch_conversation(conversation_id, limit=100, offset=0) -> dict`: conversation and messages, pagination (total, offset, has_more). IDs returned are internal IDs. Missing raises ValueError. Indexed output has token redaction. No raw artifact contents exposed.
- `fetch_message(message_id, offset=0, max_chars=12000) -> dict`: bounded character slice of one indexed/redacted message, with total_chars/has_more, internal ID and provenance fields. No raw or arbitrary file access. MCP uses this to retrieve messages too long for a single tool context.
- `search(query, source="", account="", project="", limit=20) -> list[dict]`: hits include conversation_id, message_id, title, role, text/snippet, source, account, project.
- `list_projects() -> list[dict]`: name, conversation_count, checkpoint_count, updated_at.
- `create_checkpoint(project, goal, completed="", decisions="", next_steps="", blockers="", account="default", source="manual") -> dict`: append only; returns id and supplied fields.
- `project_state(project) -> dict`: latest checkpoint, related conversations, counts.
- `list_user_memory(project="", include_proposed=False, limit=100) -> list[dict]`: latest global and exactly matched project records, confirmed by default; at most 100 entries. Proposed/withdrawn revisions do not fall back to older confirmed values.
- `save_user_memory(key, value, project="", status="proposed", evidence=None, account="default") -> dict`: append an explicit memory revision. Confirmed entries require existing archived user-message IDs; no automatic biography or inference.
- `handoff(project="", conversation_id="", max_chars=16000, *, task="") -> str`: bounded Markdown with latest checkpoint, task-related confirmed memories, historical evidence and source citations. Literal ranking, visible omissions and untrusted-data framing; at least one selector required. Task is at most 512 characters and is not saved as a preference.
- `export_handoff(path, project="", conversation_id="", max_chars=16000, *, task="") -> dict`: exclusive creation (no overwrite), returns path, chars.
- `configure_source(kind, path, account="default", project="") -> dict`: append/update managed source in local store (`kind` = codex/chatgpt/project); never store credentials.
- `list_sources() -> list[dict]`: id, kind, path, account, project, last_run, last_report.
- `sync_sources() -> list[dict]`: idempotent scan of configured sources.
- `ingest_live(payload: dict) -> dict`: ChatGPT browser live capture. Payload: source="chatgpt", account (user label), conversation_id (URL conversation UUID), title, project optional, messages=[{id,role,text,timestamp optional}], metadata optional. Capture only open/visible DOM, not hidden branches; append revisions, never delete missing messages. Return import report. 500 messages max/request, 5 MB max/body enforced by server. Indexed secret redaction; raw JSON stored as original artifact.
- `archive_project(path, project="", account="default") -> dict`: archive explicit project files, filtered credentials/build artifacts; preserve file revisions in content-addressed raw store. Use git ls-files for Git projects; non-Git supported with exclusions. Do not recurse into data_dir or symlinks/junctions. Warn on excluded/oversize files. Store git HEAD/branch/status plus versioned file hashes (not a separate patch/diff).
- `list_project_files(project, limit=100, offset=0) -> list[dict]`: current file rows, names/hashes/size; no arbitrary local reads.
- `fetch_project_file(project, relative_path, max_chars=20000) -> dict`: latest archived text only, redacted, bounded; missing/binary returns explicit limitation.
- `audit() -> dict`: SQLite integrity, raw blob SHA256 integrity, coverage warnings and counts.
- `backup(path, password=None) -> dict`: SQLite online consistent snapshot + raw archive + source state; exclusive ZIP or password encrypted container, portable, no auth files.
- `restore(path, password=None) -> dict`: restore into a NEW EMPTY data directory only; safe paths; existing vault never overwritten; verify schema/blob hashes. The constructor must not eagerly initialize an empty restore destination.

Store uses content-addressed archives and deterministic conversation/message IDs scoped by source and account. Import does not delete any existing data. Reimport changed messages appends revisions; indexed active version updates. Missing messages in a later export remain archived, with historical provenance. Import reports must distinguish unchanged from ingested rows. Search limits 1..100; paging limits 1..500; handoff char budget 1000..100000. Runtime backup retention has no automatic deletion.

Version timestamps are normalized to UTC. Older exports/offline captures add historical revisions without rolling back newer active text; export creation timestamps and graph can enrich a live observation. Fulltext search does not delete-scan FTS for new rows. Codex watcher parses full context but, after verifying the previous byte prefix hash, stores only exact append bytes plus a raw SHA256 snapshot chain manifest and submits only newer record lines. Rewritten/truncated files fall back to full snapshots. Per-file cursors keep healthy sources cached while temporary incomplete records retry. New readers skip schema DDL against an existing matching WAL store, so imports do not unnecessarily block reading.

Backup snapshot and pre-restore validation stay in memory, including SQLite temporary storage. Normal ZIP streams to its exclusive final output; encrypted ZIP stays bounded in memory. Limits: SQLite 2 GiB, each raw blob 512 MiB, expanded backup 8 GiB, encrypted ZIP 512 MiB. RAM requirements may include several buffers; runtime vault and final restored vault remain plaintext. A failed partial final output is retained and reported, never recursively cleaned or overwritten.

Restored managed-source paths are historical, not current authorization. Restore marks all sources `needs_confirmation`, without reading their paths. `sync_sources` skips them entirely until the user explicitly re-adds the same source parameters through CLI/dashboard. This also prevents an untrusted backup from authorizing broad automatic filesystem reads.

## UI/security boundary

0.3 ChatGPT import reports add `conversations_seen`, `messages_seen`, `warnings_omitted` and bounded `coverage`. Seen counts describe parsed input, not newly inserted rows. Coverage is `complete`, `partial` or `no_chat_history` for `provided_export_only`, with file/attachment counters. External pointers are never downloaded. Ignored non-history members may increment `skipped` without being a chat-history loss; actual rejected or unsupported content is warned. Warning output has at most 200 items including an omission notice; raw originals remain separate.

Bind only 127.0.0.1 by default; validate Host and Origin; restrict writes to local authenticated session with CSRF token. Serve static assets from fixed package paths, escape imported strings. Never expose arbitrary file reads/raw artifacts through network tools. Import paths are selected explicitly by local user via CLI or authenticated dashboard. No telemetry. MCP stdio is account-independent; HTTP is loopback only and not publicly deployed. Checkpoint mutation is opt-in (`--allow-writes`); do not expose arbitrary imports/backups/file writes as MCP tools.

Tests use unittest and tempfile.mkdtemp without recursive cleanup. Agent tests own unique files `tests/test_importers.py`, `tests/test_interfaces.py`; root owns core tests. Do not delete generated test directories.

## Live ChatGPT requirements and extension contract

The browser extension (Manifest V3 Chrome/Edge) observes only opened chatgpt.com pages and user-visible messages. MutationObserver + debounce saves within a few seconds; service-worker posts to loopback API; no OpenAI private endpoints, login tokens or API calls. User selects account label/project and pairs with local collector token. Per-chat IDs come from URL and message DOM IDs; do not ingest home/transient views with no stable conversation ID. Missing DOM turns do not delete stored content. Show connected/error status and explicit capture coverage; no guarantee of closed/mobile/temporary/virtualized history. Official export is needed for full historical coverage.

HTTP: `/api/live` POST with `Authorization: Bearer <collector-token>`; collector token persisted in data_dir, never query URL; only extension Origin `chrome-extension://...` or missing Origin with valid bearer accepted. Dashboard token/CSRF is separate. `/api/pairing` GET dashboard authenticated returns collector token for user copying to extension. Browser extension saves token with chrome.storage.local and never reads cookies. Responses never contain raw archive. `/api/live` body at most 5 MB. Other dashboard route design owned by interface agent.

`/api/resume` POST accepts the same collector bearer and `{project, conversation_id?, max_chars?}`; returns `{handoff}`. The extension uses storage/alarms/clipboardWrite to let the user explicitly read and copy a bounded local handoff, even with capture paused. It never injects or sends the handoff into a ChatGPT chat automatically. This direct path does not require MCP availability on the Plus account.

`/api/collector/status` POST uses the same collector Origin/Bearer boundary and an empty JSON object. It returns only current connectivity, package version, server start/check timestamps, bounded last-live labels/time and DOM-only coverage, without synchronizing sources or returning conversations, raw archives or tokens. Historical success is not current reachability. Account-label changes require pause and keep old queued labels; page captures must match the current manual label and a local random `capture_epoch`. A label change remains paused until the user confirms the target account and refreshes the page. Copy/download actions require local preview confirmation, not automatic cloud submission.

MCP also exposes archived project file list/fetch and `resume_project`; read operations share same account-independent vault, checkpoint save only explicit allow-writes. No automatic merge of native OpenAI sidebars.
