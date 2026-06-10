# ctx-scrub Claude v0.3 Quickstart

Use this flow for a real Claude Code project.

## 1. Check Readiness

```bash
cd /home/shikhar/openclaw/research/context-scrub
./ctxscrub doctor
```

## 2. Open The Interactive CLI

```bash
./ctxscrub
```

Keys:

- `j/k` or arrow keys: move
- `/`: filter sessions
- `Enter`: choose session
- `space`: mark/unmark transcript block
- `/`: find/filter inside the session transcript
- `c`: clear transcript filter
- `e`: edit replacement marker
- `r`: redact marked blocks
- `q`: quit

The default TUI does not ask for a word first. It opens the selected session as a readable transcript, including user messages, assistant messages, tool calls, and tool results. Mark the blocks you want removed, then redact them in one batch.

For the older exact-text match review, launch with:

```bash
./ctxscrub --query "text to remove"
```

## 3. Scriptable Flow

Use this when you want exact command control instead of the TUI.

### Find The Right Session

Avoid `--latest` for real work unless you are certain it is the session you want.

```bash
cd /home/shikhar/openclaw/research/context-scrub/claude
./ctx_scrub_claude.py list --project-contains "your-project-name" --limit 10
```

Copy the target `session=<uuid>` value.

### Inspect The Session

```bash
./ctx_scrub_claude.py inspect --session-id <session-id>
```

Proceed only if `missing_parent_refs=0`.

### Search Exact Text

```bash
./ctx_scrub_claude.py search --session-id <session-id> --query "text to remove"
```

Review the field paths and snippets carefully.

### Redact Safely

Dry-run first:

```bash
./ctx_scrub_claude.py redact --session-id <session-id> --query "text to remove"
```

Apply:

```bash
./ctx_scrub_claude.py redact --session-id <session-id> --query "text to remove" --apply
```

If Claude Code just wrote to the file, ctx-scrub refuses to mutate it for 15 seconds by default. Wait a moment, close/pause Claude Code, or explicitly override only if you know the session is not being written:

```bash
./ctx_scrub_claude.py redact --session-id <session-id> --query "text to remove" --apply --allow-recent
```

Or use interactive review:

```bash
./ctx_scrub_claude.py review --session-id <session-id> --query "text to remove"
```

The review command applies only if you type exactly:

```text
REDACT
```

### Verify

```bash
./ctx_scrub_claude.py verify --session-id <session-id> --query "text to remove"
```

Success means:

- JSONL parses
- parent links are intact
- the query is gone

### Continue Claude

```bash
./ctx_scrub_claude.py clean-prompt --session-id <session-id> --query "text to remove"
```

Use the generated continuation prompt when resuming Claude Code.

### Roll Back If Needed

```bash
./ctx_scrub_claude.py backups --session-id <session-id>
./ctx_scrub_claude.py restore --session-id <session-id>
```

Restore uses the latest backup for the session by default.

## Rules

- Redaction first, not message deletion.
- Use the transcript browser for visual review.
- Use exact-text commands when you already know the exact string.
- Do not mutate live sessions while Claude Code is actively writing.
- Prefer `--session-id` over `--latest`.
- Keep backups.
- Use `--allow-recent` only when you know Claude Code is not actively appending to that session.
