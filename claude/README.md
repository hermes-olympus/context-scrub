# ctx-scrub Claude Code Plan

Current prototype version: `0.1.0`

Quickstart: `QUICKSTART.md`

## Goal

Build a small local CLI/TUI that watches Claude Code conversation JSONL files, ranks stale or risky context, and helps create a cleaner next prompt/session before compaction or context drift.

The first version should not promise magical deletion from a model's already-loaded context window. It should safely operate on local Claude Code session files and generate clean continuation context for the next prompt or resumed session.

Note on Claude Code cost reporting: when using `claude -p --output-format json`, Claude Code reports an estimated `total_cost_usd` for the model calls. In a subscription setup this should be treated as usage accounting / estimated value unless billing separately confirms otherwise.

## Feasibility Check

Local inspection on 2026-06-10 found:

- Claude Code stores conversations under `~/.claude/projects/.../*.jsonl`.
- The local `~/.claude` directory is about 33 MB.
- There are 63 parseable JSONL files under `~/.claude/projects`.
- All inspected JSONL files parsed cleanly.
- Conversation records have stable `uuid` and `parentUuid` links.
- Across inspected files, parent references were internally consistent.
- Session-adjacent folders exist for `file-history`, `session-env`, `tool-results`, `subagents`, and workflow data.

This makes Claude Code a good first target for analysis, ranking, and clean-continuation generation.

## Live Resume Test

A disposable Claude Code session was created on 2026-06-10 using:

```text
session_id = 11111111-2222-4333-8444-555555555555
workspace = /home/shikhar/openclaw/research/context-scrub/claude/test-workspace
```

The test used two artificial sentinel facts:

```text
KEEP_FACT_CTXSCRUB_ALPHA=green
DELETE_FACT_CTXSCRUB_BETA=orange
```

Observed behavior:

- `claude -p --session-id <uuid>` created a normal JSONL session under `~/.claude/projects/...`.
- `claude -p --resume <uuid>` read prior context from the stored session and returned both sentinel facts.
- Redacting `DELETE_FACT_CTXSCRUB_BETA=orange` inside the JSONL kept the file parseable.
- The `uuid` / `parentUuid` chain stayed intact after text redaction.
- A later `claude -p --resume <uuid>` no longer surfaced the original delete value; it surfaced the redacted marker instead.
- Claude diagnostics reported `cache_miss_reason: messages_changed`, which is useful evidence that Claude noticed the changed local transcript and rebuilt context.

This confirms text redaction inside a Claude Code JSONL session can affect future resumes of that session, at least for simple content replacement.

This does not prove that arbitrary message deletion is safe. Redaction is the safer first mutation strategy.

## Important Constraint

A CLI cannot reliably remove text from the model's already-active in-memory context unless Claude Code exposes an official editable conversation-state API.

So the reliable first product is:

- watch the live JSONL
- analyze the conversation
- rank removable context
- let the user approve
- generate a clean continuation prompt or cleaned session artifact

Direct mutation of live JSONL should be treated as an advanced mode after compatibility testing.

## Storage Model Observed

Main conversation files:

```text
~/.claude/projects/<project-id>/<session-id>.jsonl
```

Related data:

```text
~/.claude/session-env/<session-id>/
~/.claude/file-history/<session-id>/
~/.claude/projects/<project-id>/<session-id>/tool-results/
~/.claude/projects/<project-id>/<session-id>/subagents/
~/.claude/projects/<project-id>/<session-id>/workflows/
```

Main JSONL event types seen:

- `user`
- `assistant`
- `attachment`
- `last-prompt`
- `queue-operation`
- `file-history-snapshot`
- `mode`
- `ai-title`
- `system`
- `permission-mode`

Most useful records for context cleanup are `user`, `assistant`, and selected `attachment` records.

## Reliability Assessment

### Reliable Now

- Read session JSONL files.
- Parse message records and metadata.
- Detect live/latest session by mtime.
- Chunk messages into user turns, assistant turns, tool results, and attachments.
- Score context for likely usefulness or removability.
- Generate a clean continuation prompt.
- Generate a local report showing what would be kept/dropped.
- Back up files before any edits.
- Redact specific text in copied or inactive Claude JSONL files, then verify parse and parent links.

### Medium Risk

- Redacting text inside JSONL records.
- Removing single messages from the middle of a parentUuid chain.
- Editing files while Claude Code is actively appending to them.
- Deleting tool-result files referenced by assistant messages.
- Redacting text that appears in generated summaries, tool transcripts, or Claude-managed memory-like transcript sections.

### High Risk

- Mutating active live sessions without locking.
- Rewriting parentUuid chains without extensive resume testing.
- Assuming deletion from disk changes the current model call's already-loaded context.

## Product Shape

CLI name:

```text
ctx-scrub
```

Current Claude-only prototype:

```text
/home/shikhar/openclaw/research/context-scrub/claude/ctx_scrub_claude.py
```

Claude-specific commands:

```bash
ctx-scrub claude list
ctx-scrub claude inspect --latest
ctx-scrub claude score --latest
ctx-scrub claude review --latest
ctx-scrub claude clean-prompt --latest
ctx-scrub claude redact --latest --query "text" --dry-run
ctx-scrub claude verify --latest --query "text"
```

Prototype equivalents:

```bash
./ctx_scrub_claude.py list --latest
./ctx_scrub_claude.py inspect --latest
./ctx_scrub_claude.py search --latest --query "text"
./ctx_scrub_claude.py review --latest --query "text"
./ctx_scrub_claude.py redact --latest --query "text"
./ctx_scrub_claude.py redact --latest --query "text" --apply
./ctx_scrub_claude.py verify --latest --query "text"
./ctx_scrub_claude.py clean-prompt --latest --query "text"
```

Initial behavior should be dry-run first.

The prototype currently supports:

- listing Claude Code JSONL sessions
- inspecting row counts, event types, roles, and parent-link integrity
- filtering sessions by project path, recency, and subagent inclusion
- exact phrase search with JSON field paths
- dry-run redaction by default
- interactive review requiring the exact confirmation word `REDACT`
- structured JSONL redaction across string fields
- timestamped backups before mutation
- backup listing and restore
- audit logging
- recent-file mutation guard with `--allow-recent` override
- automatic rollback if verification fails
- clean continuation prompt generation
- `doctor`, `workflow`, and `--version`

## Context Scoring

Score each chunk on five axes:

1. Relevance to current task
2. Recency
3. Sensitivity risk
4. Redundancy
5. Operational value

High-keep examples:

- Current user request
- Explicit instructions
- Active file paths
- Important decisions
- Current errors
- Test results
- Commands already run
- User preferences
- Safety constraints

High-drop examples:

- Repeated assistant status narration
- Obsolete plans
- Failed paths after a successful fix
- Long logs already summarized
- Duplicate tool output
- Old unrelated research
- Large attachments no longer needed
- Sensitive strings not required for the next step

## Embeddings And Live KG

Embeddings and a lightweight KG can help after the first workflow is reliable.

Recommended sequence:

1. Start with deterministic chunking and heuristic scoring.
2. Add local SQLite storage for chunk metadata.
3. Add optional local embeddings for similarity and duplicate detection.
4. Add a lightweight graph table for entities and relations.

Avoid Neo4j in the first version. SQLite is enough and safer for a portable CLI.

Suggested local schema:

```text
sessions(session_id, project_path, jsonl_path, started_at, updated_at)
chunks(chunk_id, session_id, uuid, parent_uuid, role, type, ts, text_hash, approx_tokens)
signals(chunk_id, relevance, recency, sensitivity, redundancy, operational_value, final_score)
entities(entity_id, label, kind)
mentions(chunk_id, entity_id)
relations(src_entity_id, relation, dst_entity_id, chunk_id)
```

## Clean Continuation Output

The MVP should generate:

```markdown
# Clean Continuation Context

## Current Goal
...

## Must Keep
...

## Decisions Made
...

## Files/Commands/Results
...

## Dropped Context
...

## Next Prompt
...
```

This is the most reliable way to improve a live conversation without depending on Claude Code internals.

## Direct Deletion Strategy

Only after MVP validation:

1. Require Claude Code session to be closed or paused.
2. Create timestamped backup.
3. Parse JSONL and build dependency graph.
4. If deleting complete tail segment, truncate safely.
5. If deleting middle chunks, prefer redaction over removal.
6. If removing records, repair parentUuid chain only in tested mode.
7. Verify JSONL parse and parent references.
8. Reopen Claude Code and test resume behavior.

Preferred destructive order:

1. `clean-prompt` output only
2. redaction of text within message content
3. deletion of whole tail segment
4. deletion of middle records with chain repair

## Tested Mutation Strategy

Start with redaction, not deletion.

For a target query:

1. Locate candidate session JSONL files.
2. Search for exact text matches.
3. Show every matching record and field path.
4. Create backup beside the original or in a ctx-scrub backup folder.
5. Replace exact sensitive text with a stable marker, for example:

```text
[CTX_SCRUB_REDACTED]
```

6. Re-parse the JSONL.
7. Verify `parentUuid` references still resolve.
8. Search again to confirm the original text is gone.
9. Resume the session manually or with a test prompt to confirm behavior.

Avoid deleting records in the MVP.

## MVP Build Plan

### Day 1

- Create Python CLI skeleton.
- Add Claude session discovery.
- Parse JSONL into normalized chunks.
- Detect latest/live session.
- Print session summary.
- Add exact query search.
- Add dry-run redaction report.
- Add structured field-path search output.
- Add inspect command.

### Day 2

- Add interactive review command.
- Add query search and secret-pattern scan.
- Add backup and verification commands.
- Add dry-run redaction for plain text inside message content.
- Add actual redaction behind a confirmation flag.
- Add resume-test helper for disposable sessions.
- Add clean continuation prompt command.

### Day 3

- Test with copied sessions, never live originals first.
- Add regression tests for JSONL parsing and parentUuid integrity.
- Add compatibility notes for Claude Code resume behavior.
- Decide whether live mutation is safe enough to expose.
- Add clean continuation generator.
- Add scoring heuristics only after search/redaction is reliable.

## Safety Rules

- Default to read-only.
- Never edit without backup.
- Never edit credentials files.
- Never modify active files unless user explicitly confirms.
- Prefer clean continuation over destructive deletion.
- Verify after every edit.
- Keep audit logs local only.

## Verdict

Claude Code is feasible and reliable as the first target for context ranking and clean continuation. Live hard-deletion from the active model context is not something to promise. Local JSONL cleanup and clean next-prompt generation are practical, useful, and buildable quickly.
