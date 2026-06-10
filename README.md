# ctx-scrub

Local AI-client context scrubber.

Public repo:

```text
https://github.com/hermes-olympus/context-scrub
```

Clone on another machine:

```bash
git clone https://github.com/hermes-olympus/context-scrub.git
cd context-scrub/claude
./ctx_scrub_claude.py doctor
```

The first supported provider is Claude Code.

## Claude Code v0.3

```bash
./ctxscrub
```

Default interactive flow:

- pick a Claude Code session by project/time/first-message label
- browse the real session transcript: user messages, assistant replies, tool calls, and tool results
- press `space` to mark/unmark whole transcript blocks
- use `/` to find/filter inside the transcript when needed
- press `r` to redact marked blocks
- type `REDACT` to confirm
- ctx-scrub creates a backup and verifies the JSONL

Exact-text search still exists for command-line or pre-filtered workflows:

```bash
./ctxscrub --query "text to remove"
```

Scriptable flow:

```bash
cd claude
./ctx_scrub_claude.py list --project-contains "your-project" --limit 10
./ctx_scrub_claude.py inspect --session-id <session-id>
./ctx_scrub_claude.py search --session-id <session-id> --query "text to remove"
./ctx_scrub_claude.py review --session-id <session-id> --query "text to remove"
./ctx_scrub_claude.py verify --session-id <session-id> --query "text to remove"
```

Direct dry-run and apply:

```bash
./ctx_scrub_claude.py redact --session-id <session-id> --query "text to remove"
./ctx_scrub_claude.py redact --session-id <session-id> --query "text to remove" --apply
```

Rollback:

```bash
./ctx_scrub_claude.py backups --session-id <session-id>
./ctx_scrub_claude.py restore --session-id <session-id>
```

## Safety Model

- Redaction first, not message deletion.
- Transcript browser redacts marked blocks without requiring a search term.
- Exact-text redaction is still available for scriptable workflows.
- Dry-run by default.
- Timestamped backups before mutation.
- Audit log for applied mutations.
- Parent-link validation after writes.
- Recent-file mutation guard for live sessions.
- Subagent sessions excluded by default.

## Provider Roadmap

- `claude/` first
- Codex next
- Cursor / VS Code extension storage later

No embeddings or knowledge graph are included.
