# ctx-scrub Claude v0.1 Quickstart

Use this flow for a real Claude Code project.

## 1. Check Readiness

```bash
cd /home/shikhar/openclaw/research/context-scrub/claude
./ctx_scrub_claude.py doctor
```

## 2. Find The Right Session

Avoid `--latest` for real work unless you are certain it is the session you want.

```bash
./ctx_scrub_claude.py list --project-contains "your-project-name" --limit 10
```

Copy the target `session=<uuid>` value.

## 3. Inspect The Session

```bash
./ctx_scrub_claude.py inspect --session-id <session-id>
```

Proceed only if `missing_parent_refs=0`.

## 4. Search Exact Text

```bash
./ctx_scrub_claude.py search --session-id <session-id> --query "text to remove"
```

Review the field paths and snippets carefully.

## 5. Redact Safely

Dry-run first:

```bash
./ctx_scrub_claude.py redact --session-id <session-id> --query "text to remove"
```

Apply:

```bash
./ctx_scrub_claude.py redact --session-id <session-id> --query "text to remove" --apply
```

If Claude Code just wrote to the file, v0.1 refuses to mutate it for 15 seconds by default. Wait a moment, close/pause Claude Code, or explicitly override only if you know the session is not being written:

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

## 6. Verify

```bash
./ctx_scrub_claude.py verify --session-id <session-id> --query "text to remove"
```

Success means:

- JSONL parses
- parent links are intact
- the query is gone

## 7. Continue Claude

```bash
./ctx_scrub_claude.py clean-prompt --session-id <session-id> --query "text to remove"
```

Use the generated continuation prompt when resuming Claude Code.

## 8. Roll Back If Needed

```bash
./ctx_scrub_claude.py backups --session-id <session-id>
./ctx_scrub_claude.py restore --session-id <session-id>
```

Restore uses the latest backup for the session by default.

## Rules For v0.1

- Redaction first, not message deletion.
- Use exact text only.
- Do not mutate live sessions while Claude Code is actively writing.
- Prefer `--session-id` over `--latest`.
- Keep backups.
- Use `--allow-recent` only when you know Claude Code is not actively appending to that session.
