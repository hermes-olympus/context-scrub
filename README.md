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

The first supported provider is Claude Code. v0.1 focuses on safe exact-text redaction in Claude Code JSONL session files.

## Claude Code v0.2

```bash
./ctxscrub
```

Default interactive flow:

- pick a Claude Code session
- enter text to find
- review all matching context fields
- press `space` to mark/unmark matches
- press `r` to redact selected matches
- type `REDACT` to confirm
- ctx-scrub creates a backup and verifies the JSONL

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
- Exact text only in v0.1.
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

No embeddings or knowledge graph are included in v0.1.
