#!/usr/bin/env python3
"""Small Claude Code JSONL search/redaction prototype."""

from __future__ import annotations

import argparse
import curses
import json
import os
import shutil
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CLAUDE_PROJECTS = Path(os.environ.get("CTXSCRUB_CLAUDE_PROJECTS", Path.home() / ".claude" / "projects")).expanduser()
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BACKUP_DIR = SCRIPT_DIR / "backups"
AUDIT_LOG = SCRIPT_DIR / "audit.jsonl"
VERSION = "0.3.4"
MIN_TUI_HEIGHT = 8
MIN_TUI_WIDTH = 32
SESSION_PREVIEW_LINE_LIMIT = 80
SESSION_PREVIEW_BYTES_LIMIT = 256_000
LOW_VALUE_TRANSCRIPT_KINDS = {
    "agent-name",
    "ai-title",
    "attachment",
    "custom-title",
    "file-history-snapshot",
    "mode",
    "permission-mode",
    "queue-operation",
    "result",
    "started",
    "system",
    "thinking",
}


@dataclass
class SessionFile:
    path: Path
    session_id: str
    project: str
    mtime: float
    size: int
    rows: int
    is_subagent: bool


def is_subagent_path(path: Path) -> bool:
    return "subagents" in path.parts


def project_label(path: Path) -> str:
    try:
        rel = path.relative_to(CLAUDE_PROJECTS)
    except ValueError:
        return path.parent.name
    if len(rel.parts) < 2:
        return path.parent.name
    return rel.parts[0].replace("-", "/", 1).replace("-", "/")


@dataclass
class Match:
    line_no: int
    role: str
    uuid: str
    field_path: str
    snippet: str


@dataclass
class MatchSelection:
    match: Match
    selected: bool = False


@dataclass
class TranscriptItem:
    line_no: int
    role: str
    kind: str
    uuid: str
    field_path: str
    title: str
    body: Any


@dataclass
class TranscriptSelection:
    item: TranscriptItem
    selected: bool = False


def iter_jsonl_files() -> list[Path]:
    if not CLAUDE_PROJECTS.exists():
        return []
    return sorted(CLAUDE_PROJECTS.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def read_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
    return rows


def json_dumps(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def session_info(path: Path) -> SessionFile:
    rows = read_rows(path)
    stat = path.stat()
    return SessionFile(
        path=path,
        session_id=path.stem,
        project=path.parent.name,
        mtime=stat.st_mtime,
        size=stat.st_size,
        rows=len(rows),
        is_subagent=is_subagent_path(path),
    )


def select_paths(args: argparse.Namespace) -> list[Path]:
    files = iter_jsonl_files()
    include_subagents = getattr(args, "include_subagents", False)
    project_contains = getattr(args, "project_contains", None)
    last_minutes = getattr(args, "last_minutes", None)
    if not include_subagents:
        files = [p for p in files if not is_subagent_path(p)]
    if project_contains:
        files = [p for p in files if project_contains.lower() in str(p).lower()]
    if last_minutes:
        cutoff = datetime.now(timezone.utc).timestamp() - (last_minutes * 60)
        files = [p for p in files if p.stat().st_mtime >= cutoff]
    if args.path:
        return [Path(args.path).expanduser()]
    if args.session_id:
        matches = [p for p in files if p.stem == args.session_id]
        if not matches:
            raise SystemExit(f"No Claude JSONL found for session {args.session_id}")
        return matches
    if args.latest:
        if not files:
            raise SystemExit("No Claude JSONL files found")
        return [files[0]]
    return files


def list_sessions(args: argparse.Namespace) -> None:
    for info in [session_info(p) for p in select_paths(args)[: args.limit]]:
        when = datetime.fromtimestamp(info.mtime, tz=timezone.utc).isoformat()
        subagent = " subagent=true" if info.is_subagent else ""
        label = project_label(info.path)
        print(f"{when} rows={info.rows} size={info.size} session={info.session_id} project={label}{subagent}")
        print(f"  {info.path}")


def validate_parent_chain(rows: list[dict]) -> tuple[int, int]:
    uuids = {row.get("uuid") for row in rows if row.get("uuid")}
    refs = [row.get("parentUuid") for row in rows if row.get("parentUuid")]
    missing = sum(1 for ref in refs if ref not in uuids)
    return len(refs), missing


def short_snippet(value: str, query: str, width: int = 90) -> str:
    one_line = value.replace("\n", "\\n")
    hit = one_line.find(query)
    if hit == -1:
        return one_line[: width * 2]
    start = max(0, hit - width)
    end = min(len(one_line), hit + len(query) + width)
    return one_line[start:end]


def compact_text(value: Any, width: int = 160) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = " ".join(text.replace("\n", " ").split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "..."


def first_preview_text(path: Path) -> str:
    bytes_read = 0
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            bytes_read += len(line.encode("utf-8", errors="ignore"))
            if line_no > SESSION_PREVIEW_LINE_LIMIT or bytes_read > SESSION_PREVIEW_BYTES_LIMIT:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            role = str(message.get("role") or row.get("type") or "")
            if role not in {"user", "assistant"}:
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return compact_text(content, 72)
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        text = part["text"].strip()
                        if text:
                            return compact_text(text, 72)
    return ""


def role_label(row: dict, content_part: Any | None = None) -> tuple[str, str]:
    role = row_role(row) or str(row.get("type") or "event")
    kind = str(row.get("type") or "event")
    if isinstance(content_part, dict):
        part_type = str(content_part.get("type") or "")
        if part_type:
            kind = part_type
        if part_type == "tool_use":
            role = "tool call"
        elif part_type == "tool_result":
            role = "tool result"
    return role, kind


def row_title(row: dict, line_no: int, role: str, kind: str, body: Any) -> str:
    label = {
        "user": "USER",
        "assistant": "ASSISTANT",
        "tool call": "TOOL",
        "tool result": "RESULT",
        "last-prompt": "LAST PROMPT",
        "summary": "SUMMARY",
    }.get(role, role.upper())
    if kind in {"lastPrompt", "summary", "toolUseResult"}:
        label = {
            "lastPrompt": "LAST PROMPT",
            "summary": "SUMMARY",
            "toolUseResult": "TOOL RESULT",
        }[kind]
    if isinstance(body, dict) and body.get("name"):
        return f"{label} {body.get('name')}  line {line_no}"
    return f"{label}  line {line_no}"


def readable_tool_body(value: dict) -> str:
    title = str(value.get("name") or value.get("type") or "tool")
    lines = [title]
    if value.get("id"):
        lines.append(f"id: {value.get('id')}")
    tool_input = value.get("input")
    if tool_input not in (None, "", {}, []):
        lines.append("input:")
        lines.append(json.dumps(tool_input, ensure_ascii=False, indent=2, sort_keys=True))
    other = {key: item for key, item in value.items() if key not in {"type", "name", "id", "input"}}
    if other:
        lines.append("details:")
        lines.append(json.dumps(other, ensure_ascii=False, indent=2, sort_keys=True))
    return "\n".join(lines)


def readable_tool_result_body(value: dict) -> str:
    content = value.get("content")
    if isinstance(content, str):
        return content
    if content not in (None, "", [], {}):
        return json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True)
    other = {key: item for key, item in value.items() if key not in {"type", "tool_use_id", "is_error"}}
    if other:
        return json.dumps(other, ensure_ascii=False, indent=2, sort_keys=True)
    return compact_text(value, 2000)


def readable_transcript_body(item: TranscriptItem) -> str:
    body = item.body
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        if body.get("type") == "tool_use":
            return readable_tool_body(body)
        if body.get("type") == "tool_result":
            return readable_tool_result_body(body)
        return json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True)
    return json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True)


def is_low_value_transcript_item(item: TranscriptItem) -> bool:
    return item.kind in LOW_VALUE_TRANSCRIPT_KINDS or item.role in LOW_VALUE_TRANSCRIPT_KINDS


def transcript_visible_indexes(selections: list[TranscriptSelection], show_meta: bool) -> list[int]:
    return [
        idx
        for idx, selection in enumerate(selections)
        if show_meta or not is_low_value_transcript_item(selection.item)
    ]


def session_display_name(path: Path) -> str:
    stat = path.stat()
    when = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    first_text = first_preview_text(path)
    label = project_label(path)
    if first_text:
        return f"{when}  {label}  {first_text}"
    return f"{when}  {label}  {path.stem[:8]} size={stat.st_size}"


def build_transcript(rows: list[dict]) -> list[TranscriptItem]:
    items: list[TranscriptItem] = []
    for line_no, row in enumerate(rows, start=1):
        uuid = str(row.get("uuid") or "")
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = message.get("content") if message else None
        if isinstance(content, str):
            role, kind = role_label(row)
            items.append(
                TranscriptItem(
                    line_no=line_no,
                    role=role,
                    kind="text",
                    uuid=uuid,
                    field_path="$.message.content",
                    title=row_title(row, line_no, role, "text", content),
                    body=content,
                )
            )
            continue
        if isinstance(content, list):
            for idx, part in enumerate(content):
                role, kind = role_label(row, part)
                if isinstance(part, dict):
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        body = part["text"]
                        field_path = f"$.message.content[{idx}].text"
                    else:
                        body = part
                        field_path = f"$.message.content[{idx}]"
                else:
                    body = part
                    field_path = f"$.message.content[{idx}]"
                items.append(
                    TranscriptItem(
                        line_no=line_no,
                        role=role,
                        kind=kind,
                        uuid=uuid,
                        field_path=field_path,
                        title=row_title(row, line_no, role, kind, body),
                        body=body,
                    )
                )
            continue

        for key in ("lastPrompt", "summary", "toolUseResult"):
            if key in row:
                role, kind = role_label(row)
                body = row[key]
                items.append(
                    TranscriptItem(
                        line_no=line_no,
                        role=role,
                        kind=key,
                        uuid=uuid,
                        field_path=f"$.{key}",
                        title=row_title(row, line_no, role, key, body),
                        body=body,
                    )
                )
                break
        else:
            role, kind = role_label(row)
            items.append(
                TranscriptItem(
                    line_no=line_no,
                    role=role,
                    kind=kind,
                    uuid=uuid,
                    field_path="$",
                    title=row_title(row, line_no, role, kind, row),
                    body=row,
                )
            )
    return items


def iter_string_values(value: Any, prefix: str = "$"):
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            yield from iter_string_values(item, f"{prefix}[{idx}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_string_values(item, f"{prefix}.{key}")


def replace_string_values(value: Any, query: str, replacement: str) -> tuple[Any, int]:
    if isinstance(value, str):
        count = value.count(query)
        return value.replace(query, replacement), count
    if isinstance(value, list):
        new_items = []
        total = 0
        for item in value:
            new_item, count = replace_string_values(item, query, replacement)
            new_items.append(new_item)
            total += count
        return new_items, total
    if isinstance(value, dict):
        new_obj = {}
        total = 0
        for key, item in value.items():
            new_item, count = replace_string_values(item, query, replacement)
            new_obj[key] = new_item
            total += count
        return new_obj, total
    return value, 0


def parse_field_path(path: str) -> list[str | int]:
    if not path.startswith("$"):
        raise ValueError(f"Unsupported field path: {path}")
    tokens: list[str | int] = []
    i = 1
    while i < len(path):
        if path[i] == ".":
            i += 1
            start = i
            while i < len(path) and path[i] not in ".[":
                i += 1
            tokens.append(path[start:i])
        elif path[i] == "[":
            end = path.index("]", i)
            tokens.append(int(path[i + 1 : end]))
            i = end + 1
        else:
            raise ValueError(f"Unsupported field path: {path}")
    return tokens


def get_path_value(value: Any, tokens: list[str | int]) -> Any:
    current = value
    for token in tokens:
        current = current[token]
    return current


def set_path_value(value: Any, tokens: list[str | int], new_value: Any) -> None:
    if not tokens:
        raise ValueError("Refusing to replace the whole JSONL row")
    current = value
    for token in tokens[:-1]:
        current = current[token]
    current[tokens[-1]] = new_value


def redact_selected_matches(rows: list[dict], query: str, replacement: str, selections: list[Match]) -> tuple[list[dict], int]:
    by_line_and_path = {(selection.line_no, selection.field_path) for selection in selections}
    redacted_rows: list[dict] = []
    total = 0
    for line_no, row in enumerate(rows, start=1):
        new_row = json.loads(json_dumps(row))
        for _, field_path in [item for item in by_line_and_path if item[0] == line_no]:
            tokens = parse_field_path(field_path)
            value = get_path_value(new_row, tokens)
            if not isinstance(value, str):
                continue
            count = value.count(query)
            if count:
                set_path_value(new_row, tokens, value.replace(query, replacement))
                total += count
        redacted_rows.append(new_row)
    return redacted_rows, total


def redacted_value(value: Any, replacement: str) -> tuple[Any, int]:
    if isinstance(value, str):
        return replacement, 1
    if isinstance(value, list):
        new_items = []
        total = 0
        for item in value:
            new_item, count = redacted_value(item, replacement)
            new_items.append(new_item)
            total += count
        return new_items, total
    if isinstance(value, dict):
        new_obj = {}
        total = 0
        for key, item in value.items():
            if key in {"type", "name", "id"}:
                new_obj[key] = item
                continue
            new_item, count = redacted_value(item, replacement)
            new_obj[key] = new_item
            total += count
        return new_obj, total
    if value is None:
        return value, 0
    return replacement, 1


def redact_selected_transcript_items(
    rows: list[dict], replacement: str, selections: list[TranscriptItem]
) -> tuple[list[dict], int]:
    by_line_and_path = {(selection.line_no, selection.field_path) for selection in selections}
    redacted_rows: list[dict] = []
    total = 0
    for line_no, row in enumerate(rows, start=1):
        new_row = json.loads(json_dumps(row))
        for _, field_path in [item for item in by_line_and_path if item[0] == line_no]:
            if field_path == "$":
                continue
            tokens = parse_field_path(field_path)
            value = get_path_value(new_row, tokens)
            new_value, count = redacted_value(value, replacement)
            if count:
                set_path_value(new_row, tokens, new_value)
                total += count
        redacted_rows.append(new_row)
    return redacted_rows, total


def row_role(row: dict) -> str:
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    return str(message.get("role") or row.get("type") or "")


def search_text(path: Path, query: str) -> list[Match]:
    matches: list[Match] = []
    for idx, row in enumerate(read_rows(path), start=1):
        if query not in json_dumps(row):
            continue
        role = row_role(row)
        uuid = str(row.get("uuid") or "")
        for field_path, value in iter_string_values(row):
            if query not in value:
                continue
            snippet = short_snippet(value, query)
            matches.append(Match(idx, role, uuid, field_path, snippet))
    return matches


def print_matches(path: Path, matches: list[Match], limit: int | None = None) -> None:
    print(path)
    shown = matches if limit is None else matches[:limit]
    for match in shown:
        uuid_text = f" uuid={match.uuid}" if match.uuid else ""
        print(f"  line={match.line_no} role={match.role}{uuid_text}")
        print(f"    field={match.field_path}")
        print(f"    ...{match.snippet}...")
    if limit is not None and len(matches) > limit:
        print(f"  ...{len(matches) - limit} more matches hidden")


def redact_rows(rows: list[dict], query: str, replacement: str) -> tuple[list[dict], int]:
    redacted_rows: list[dict] = []
    total = 0
    for row in rows:
        new_row, count = replace_string_values(row, query, replacement)
        redacted_rows.append(new_row)
        total += count
    return redacted_rows, total


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json_dumps(row) + "\n" for row in rows), encoding="utf-8")


def backup_file(path: Path, backup_dir_arg: str | None) -> Path:
    backup_dir = Path(backup_dir_arg).expanduser() if backup_dir_arg else DEFAULT_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"{path.name}.ctxscrub-{stamp}.bak"
    shutil.copy2(path, backup)
    return backup


def assert_not_recently_modified(path: Path, threshold_seconds: int, allow_recent: bool) -> None:
    if allow_recent or threshold_seconds <= 0:
        return
    age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
    if age < threshold_seconds:
        raise SystemExit(
            f"Refusing to modify recently changed file ({age:.1f}s old): {path}\n"
            f"Close/pause Claude Code or wait, then retry. Override with --allow-recent."
        )


def audit(action: str, **fields: Any) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        **fields,
    }
    with AUDIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json_dumps(record) + "\n")


def iter_backups(backup_dir_arg: str | None = None, session_id: str | None = None) -> list[Path]:
    backup_dir = Path(backup_dir_arg).expanduser() if backup_dir_arg else DEFAULT_BACKUP_DIR
    if not backup_dir.exists():
        return []
    backups = sorted(backup_dir.glob("*.jsonl.ctxscrub-*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    if session_id:
        backups = [p for p in backups if p.name.startswith(f"{session_id}.jsonl.")]
    return backups


def cmd_inspect(args: argparse.Namespace) -> None:
    from collections import Counter

    for path in select_paths(args):
        rows = read_rows(path)
        refs, missing = validate_parent_chain(rows)
        types = Counter(row.get("type", "<none>") for row in rows)
        roles = Counter(row_role(row) for row in rows)
        print(path)
        print(f"  rows={len(rows)} size={path.stat().st_size}")
        print(f"  parent_refs={refs} missing_parent_refs={missing}")
        print(f"  event_types={dict(types.most_common())}")
        print(f"  roles={dict(roles.most_common())}")


def cmd_search(args: argparse.Namespace) -> None:
    total = 0
    for path in select_paths(args):
        matches = search_text(path, args.query)
        if not matches:
            continue
        total += len(matches)
        print_matches(path, matches, args.limit)
    print(f"matches={total}")


def cmd_verify(args: argparse.Namespace) -> None:
    failed = False
    for path in select_paths(args):
        rows = read_rows(path)
        refs, missing = validate_parent_chain(rows)
        contains = args.query in path.read_text(encoding="utf-8") if args.query else False
        status = "ok" if missing == 0 and not contains else "attention"
        print(f"{status}: {path}")
        print(f"  rows={len(rows)} parent_refs={refs} missing_parent_refs={missing}")
        if args.query:
            print(f"  contains_query={contains}")
        failed = failed or missing != 0 or contains
    if failed:
        raise SystemExit(1)


def apply_redaction(
    path: Path,
    query: str,
    replacement: str,
    backup_dir: str | None,
    recent_threshold_seconds: int,
    allow_recent: bool,
) -> tuple[int, Path]:
    assert_not_recently_modified(path, recent_threshold_seconds, allow_recent)
    rows = read_rows(path)
    new_rows, count = redact_rows(rows, query, replacement)
    if count == 0:
        return 0, Path("")

    backup = backup_file(path, backup_dir)
    write_rows(path, new_rows)
    verified_rows = read_rows(path)
    _, missing = validate_parent_chain(verified_rows)
    still_contains = query in path.read_text(encoding="utf-8")
    if missing or still_contains:
        shutil.copy2(backup, path)
        audit(
            "redact_failed_restored",
            path=str(path),
            backup=str(backup),
            replacement=replacement,
            missing_parent_refs=missing,
            contains_query=still_contains,
        )
        raise SystemExit(
            f"Redaction verification failed; restored backup. missing_parent_refs={missing} contains_query={still_contains}"
        )
    audit(
        "redact",
        path=str(path),
        backup=str(backup),
        occurrences=count,
        replacement=replacement,
        session_id=path.stem,
    )
    return count, backup


def apply_selected_redaction(
    path: Path,
    query: str,
    replacement: str,
    selections: list[Match],
    backup_dir: str | None,
    recent_threshold_seconds: int,
    allow_recent: bool,
) -> tuple[int, Path]:
    assert_not_recently_modified(path, recent_threshold_seconds, allow_recent)
    rows = read_rows(path)
    new_rows, count = redact_selected_matches(rows, query, replacement, selections)
    if count == 0:
        return 0, Path("")

    backup = backup_file(path, backup_dir)
    write_rows(path, new_rows)
    verified_rows = read_rows(path)
    _, missing = validate_parent_chain(verified_rows)
    remaining = search_text(path, query)
    selected_keys = {(selection.line_no, selection.field_path) for selection in selections}
    selected_still_contains = [match for match in remaining if (match.line_no, match.field_path) in selected_keys]
    if missing or selected_still_contains:
        shutil.copy2(backup, path)
        audit(
            "selected_redact_failed_restored",
            path=str(path),
            backup=str(backup),
            replacement=replacement,
            missing_parent_refs=missing,
            selected_still_contains=len(selected_still_contains),
        )
        raise SystemExit(
            "Selected redaction verification failed; restored backup. "
            f"missing_parent_refs={missing} selected_still_contains={len(selected_still_contains)}"
        )
    audit(
        "selected_redact",
        path=str(path),
        backup=str(backup),
        selected_matches=len(selections),
        occurrences=count,
        replacement=replacement,
        session_id=path.stem,
    )
    return count, backup


def apply_transcript_redaction(
    path: Path,
    replacement: str,
    selections: list[TranscriptItem],
    backup_dir: str | None,
    recent_threshold_seconds: int,
    allow_recent: bool,
) -> tuple[int, Path]:
    assert_not_recently_modified(path, recent_threshold_seconds, allow_recent)
    rows = read_rows(path)
    new_rows, count = redact_selected_transcript_items(rows, replacement, selections)
    if count == 0:
        return 0, Path("")

    backup = backup_file(path, backup_dir)
    write_rows(path, new_rows)
    verified_rows = read_rows(path)
    _, missing = validate_parent_chain(verified_rows)
    selected_keys = {(selection.line_no, selection.field_path) for selection in selections}
    remaining_selected = [
        item
        for item in build_transcript(verified_rows)
        if (item.line_no, item.field_path) in selected_keys and replacement not in item.body
    ]
    if missing or remaining_selected:
        shutil.copy2(backup, path)
        audit(
            "transcript_redact_failed_restored",
            path=str(path),
            backup=str(backup),
            replacement=replacement,
            missing_parent_refs=missing,
            remaining_selected=len(remaining_selected),
        )
        raise SystemExit(
            "Transcript redaction verification failed; restored backup. "
            f"missing_parent_refs={missing} remaining_selected={len(remaining_selected)}"
        )
    audit(
        "transcript_redact",
        path=str(path),
        backup=str(backup),
        selected_items=len(selections),
        redacted_values=count,
        replacement=replacement,
        session_id=path.stem,
    )
    return count, backup


def cmd_redact(args: argparse.Namespace) -> None:
    replacement = args.replacement or "[CTX_SCRUB_REDACTED]"
    for path in select_paths(args):
        rows = read_rows(path)
        _, count = redact_rows(rows, args.query, replacement)
        print(f"{path}")
        print(f"  occurrences={count}")
        if count == 0:
            continue
        if not args.apply:
            print("  dry_run=true")
            continue

        applied, backup = apply_redaction(
            path,
            args.query,
            replacement,
            args.backup_dir,
            args.recent_threshold_seconds,
            args.allow_recent,
        )
        refs, missing = validate_parent_chain(read_rows(path))
        print(f"  backup={backup}")
        print(f"  applied=true replaced={applied} parent_refs={refs} missing_parent_refs={missing} contains_query=False")


def cmd_review(args: argparse.Namespace) -> None:
    replacement = args.replacement or "[CTX_SCRUB_REDACTED]"
    targets = []
    for path in select_paths(args):
        matches = search_text(path, args.query)
        if not matches:
            continue
        print_matches(path, matches, args.limit)
        targets.append((path, len(matches)))

    if not targets:
        print("matches=0")
        return

    print(f"matched_files={len(targets)} replacement={replacement}")
    print("Type REDACT to apply redaction to all matched selected files, anything else to cancel.")
    answer = input("> ").strip()
    if answer != "REDACT":
        print("cancelled=true")
        return

    for path, _ in targets:
        applied, backup = apply_redaction(
            path,
            args.query,
            replacement,
            args.backup_dir,
            args.recent_threshold_seconds,
            args.allow_recent,
        )
        print(f"applied {applied} replacements in {path}")
        print(f"  backup={backup}")


def tui_write(stdscr, row: int, col: int, text: str, attr: int = curses.A_NORMAL) -> None:
    height, width = stdscr.getmaxyx()
    if row < 0 or row >= height or col < 0 or col >= width:
        return
    available = max(0, width - col - 1)
    if available <= 0:
        return
    try:
        stdscr.addnstr(row, col, text[:available], available, attr)
    except curses.error:
        pass


def tui_curs_set(visibility: int) -> None:
    try:
        curses.curs_set(visibility)
    except curses.error:
        pass


def tui_check_size(stdscr) -> bool:
    height, width = stdscr.getmaxyx()
    if height >= MIN_TUI_HEIGHT and width >= MIN_TUI_WIDTH:
        return True
    stdscr.erase()
    tui_write(stdscr, 0, 0, "ctxscrub needs a larger terminal", curses.A_REVERSE)
    tui_write(stdscr, 2, 0, f"Current: {width}x{height}")
    tui_write(stdscr, 3, 0, f"Minimum: {MIN_TUI_WIDTH}x{MIN_TUI_HEIGHT}")
    tui_write(stdscr, max(0, height - 2), 0, "Resize terminal, or press q to quit.")
    stdscr.refresh()
    return False


def tui_draw_header(stdscr, title: str, subtitle: str = "") -> None:
    height, width = stdscr.getmaxyx()
    tui_write(stdscr, 0, 0, title.ljust(width), curses.A_REVERSE)
    if subtitle and height > 1:
        tui_write(stdscr, 1, 0, subtitle)


def tui_status(stdscr, text: str) -> None:
    height, width = stdscr.getmaxyx()
    tui_write(stdscr, height - 1, 0, text.ljust(width), curses.A_REVERSE)


def tui_prompt(stdscr, prompt: str) -> str:
    curses.echo()
    tui_curs_set(1)
    height, width = stdscr.getmaxyx()
    stdscr.move(height - 2, 0)
    stdscr.clrtoeol()
    tui_write(stdscr, height - 2, 0, prompt)
    stdscr.refresh()
    raw = stdscr.getstr(height - 2, min(len(prompt), width - 2), max(1, width - len(prompt) - 1))
    curses.noecho()
    tui_curs_set(0)
    return raw.decode("utf-8", errors="replace").strip()


class SessionLabelCache:
    def __init__(self) -> None:
        self._labels: dict[Path, tuple[float, int, str]] = {}

    def label_for(self, path: Path) -> str:
        stat = path.stat()
        cached = self._labels.get(path)
        key = (stat.st_mtime, stat.st_size)
        if cached and cached[:2] == key:
            return cached[2]
        label = session_display_name(path)
        self._labels[path] = (*key, label)
        return label


def tui_select_session(stdscr, sessions: list[Path]) -> Path | None:
    index = 0
    offset = 0
    label_cache = SessionLabelCache()
    while True:
        stdscr.erase()
        if not tui_check_size(stdscr):
            if stdscr.getch() in (ord("q"), 27):
                return None
            continue
        height, width = stdscr.getmaxyx()
        tui_draw_header(stdscr, "ctxscrub Claude - choose session", "Enter: open  j/k/arrows: move  /: filter  q: quit")
        visible_rows = max(1, height - 4)
        if index < offset:
            offset = index
        if index >= offset + visible_rows:
            offset = index - visible_rows + 1
        for screen_row, path in enumerate(sessions[offset : offset + visible_rows], start=2):
            marker = ">" if offset + screen_row - 2 == index else " "
            label = f"{marker} {label_cache.label_for(path)}"
            attr = curses.A_REVERSE if marker == ">" else curses.A_NORMAL
            tui_write(stdscr, screen_row, 0, label, attr)
        tui_status(stdscr, f"{len(sessions)} sessions. Lazy labels: only visible rows are loaded.")
        key = stdscr.getch()
        if key in (ord("q"), 27):
            return None
        if key in (curses.KEY_DOWN, ord("j")) and index < len(sessions) - 1:
            index += 1
        elif key in (curses.KEY_UP, ord("k")) and index > 0:
            index -= 1
        elif key in (curses.KEY_ENTER, 10, 13):
            return sessions[index] if sessions else None
        elif key == ord("/"):
            query = tui_prompt(stdscr, "filter project/path: ")
            if query:
                filtered = [p for p in iter_jsonl_files() if not is_subagent_path(p) and query.lower() in str(p).lower()]
                if filtered:
                    sessions = filtered
                    index = 0
                    offset = 0


def tui_review_matches(stdscr, path: Path, query: str, matches: list[Match]) -> tuple[list[Match], str] | None:
    selections = [MatchSelection(match=match, selected=True) for match in matches]
    index = 0
    offset = 0
    replacement = "[CTX_SCRUB_REDACTED]"
    while True:
        stdscr.erase()
        if not tui_check_size(stdscr):
            if stdscr.getch() in (ord("q"), 27):
                return None
            continue
        height, width = stdscr.getmaxyx()
        selected_count = sum(1 for item in selections if item.selected)
        tui_draw_header(
            stdscr,
            "ctxscrub Claude - mark redactions",
            f"space: toggle  a: all  n: none  r: redact {selected_count}/{len(selections)}  e: replacement  q: quit",
        )
        tui_write(stdscr, 2, 0, f"Session: {path.stem}  Query: {query}  Replacement: {replacement}")
        visible_rows = max(1, height - 6)
        if index < offset:
            offset = index
        if index >= offset + visible_rows:
            offset = index - visible_rows + 1
        for screen_row, item in enumerate(selections[offset : offset + visible_rows], start=4):
            selected = "[x]" if item.selected else "[ ]"
            marker = ">" if offset + screen_row - 4 == index else " "
            match = item.match
            label = f"{marker} {selected} line {match.line_no} {match.role} {match.field_path} :: {match.snippet}"
            attr = curses.A_REVERSE if marker == ">" else curses.A_NORMAL
            tui_write(stdscr, screen_row, 0, label, attr)
        tui_status(stdscr, "Review carefully. Redaction changes only selected fields and creates a backup.")
        key = stdscr.getch()
        if key in (ord("q"), 27):
            return None
        if key in (curses.KEY_DOWN, ord("j")) and index < len(selections) - 1:
            index += 1
        elif key in (curses.KEY_UP, ord("k")) and index > 0:
            index -= 1
        elif key == ord(" "):
            selections[index].selected = not selections[index].selected
        elif key == ord("a"):
            for item in selections:
                item.selected = True
        elif key == ord("n"):
            for item in selections:
                item.selected = False
        elif key == ord("e"):
            new_replacement = tui_prompt(stdscr, "replacement: ")
            if new_replacement:
                replacement = new_replacement
        elif key == ord("r"):
            chosen = [item.match for item in selections if item.selected]
            if not chosen:
                tui_status(stdscr, "No matches selected. Press any key.")
                stdscr.getch()
                continue
            confirm = tui_prompt(stdscr, f"Type REDACT to apply {len(chosen)} selected redactions: ")
            if confirm == "REDACT":
                return chosen, replacement


def wrap_display_text(text: str, width: int, max_lines: int) -> list[str]:
    clean = text.replace("\t", "    ")
    wrapped: list[str] = []
    wrap_width = max(20, width)
    for line in clean.splitlines() or [clean]:
        if not line.strip():
            if wrapped:
                wrapped.append("")
            continue
        line_chunks = textwrap.wrap(
            line,
            width=wrap_width,
            replace_whitespace=False,
            drop_whitespace=False,
            subsequent_indent="  ",
        )
        wrapped.extend(chunk.rstrip() for chunk in line_chunks)
        if len(wrapped) >= max_lines:
            break
    if len(wrapped) > max_lines:
        wrapped = wrapped[:max_lines]
    if not wrapped:
        wrapped = [""]
    source_line_count = sum(max(1, len(textwrap.wrap(line, width=wrap_width))) for line in clean.splitlines() or [clean])
    if source_line_count > max_lines:
        wrapped[-1] = compact_text(wrapped[-1], max(20, width - 4)) + " ..."
    return wrapped


def render_transcript_block(
    item: TranscriptItem,
    width: int,
    current: bool,
    selected: bool,
    max_body_lines: int,
) -> list[str]:
    marker = ">" if current else " "
    checkbox = "[x]" if selected else "[ ]"
    header = compact_text(f"{marker} {checkbox} {item.title}", width)
    body_width = max(20, width - 4)
    body_lines = wrap_display_text(readable_transcript_body(item), body_width, max_body_lines)
    rendered = [header]
    rendered.extend(f"    {line}" for line in body_lines)
    rendered.append("")
    return rendered


def transcript_block_line_count(
    selection: TranscriptSelection,
    width: int,
    current: bool,
    max_body_lines: int,
) -> int:
    return len(
        render_transcript_block(
            selection.item,
            width,
            current=current,
            selected=selection.selected,
            max_body_lines=max_body_lines,
        )
    )


def ensure_transcript_offset_visible(
    selections: list[TranscriptSelection],
    visible_indexes: list[int],
    index: int,
    offset: int,
    visible_rows: int,
    width: int,
    current_body_lines: int,
    compact_body_lines: int,
) -> int:
    if not visible_indexes:
        return 0
    index = max(0, min(index, len(visible_indexes) - 1))
    offset = max(0, min(offset, index))

    while offset < index:
        rows_before_current = 0
        for local_index in range(offset, index):
            rows_before_current += transcript_block_line_count(
                selections[visible_indexes[local_index]],
                width,
                current=False,
                max_body_lines=compact_body_lines,
            )
        current_rows = transcript_block_line_count(
            selections[visible_indexes[index]],
            width,
            current=True,
            max_body_lines=current_body_lines,
        )
        if rows_before_current + min(current_rows, visible_rows) <= visible_rows:
            break
        offset += 1

    return offset


def tui_browse_transcript(stdscr, path: Path) -> tuple[list[TranscriptItem], str] | None:
    rows = read_rows(path)
    row_count = len(rows)
    selections = [TranscriptSelection(item=item, selected=False) for item in build_transcript(rows)]
    show_meta = False
    visible_indexes = transcript_visible_indexes(selections, show_meta)
    index = 0
    offset = 0
    replacement = "[CTX_SCRUB_REDACTED]"
    filter_text = ""

    while True:
        stdscr.erase()
        if not tui_check_size(stdscr):
            if stdscr.getch() in (ord("q"), 27):
                return None
            continue
        height, width = stdscr.getmaxyx()
        selected_count = sum(1 for item in selections if item.selected)
        subtitle = "space: mark  r: redact marked  m: meta  /: find/filter  c: clear  q: quit"
        tui_draw_header(stdscr, "ctxscrub Claude - session transcript", subtitle)
        filter_suffix = f"  filter={filter_text}" if filter_text else ""
        meta_suffix = "  meta=on" if show_meta else "  meta=off"
        tui_write(
            stdscr,
            2,
            0,
            f"{project_label(path)}  session={path.stem}  rows={row_count}  blocks={len(visible_indexes)}  marked={selected_count}{meta_suffix}{filter_suffix}",
        )

        if not visible_indexes:
            tui_write(stdscr, 4, 0, "No transcript blocks match the current filter.")
            tui_status(stdscr, "Press c to clear filter, / to search again, or q to quit.")
        else:
            index = max(0, min(index, len(visible_indexes) - 1))
            visible_rows = max(1, height - 6)
            current_body_lines = max(3, visible_rows - 2)
            compact_body_lines = 4
            if index < offset:
                offset = index
            offset = ensure_transcript_offset_visible(
                selections,
                visible_indexes,
                index,
                offset,
                visible_rows,
                width,
                current_body_lines,
                compact_body_lines,
            )
            screen_row = 4
            for local_index, visible_idx in enumerate(visible_indexes[offset:], start=offset):
                if screen_row >= height - 1:
                    break
                selection = selections[visible_idx]
                current = local_index == index
                max_body_lines = current_body_lines if current else compact_body_lines
                block_lines = render_transcript_block(
                    selection.item,
                    width,
                    current=current,
                    selected=selection.selected,
                    max_body_lines=max_body_lines,
                )
                for block_line_index, line in enumerate(block_lines):
                    if screen_row >= height - 1:
                        break
                    attr = curses.A_REVERSE if current and block_line_index == 0 else curses.A_NORMAL
                    tui_write(stdscr, screen_row, 0, line, attr)
                    screen_row += 1
            tui_status(
                stdscr,
                "Readable view hides low-value metadata by default. Press m for forensic/meta blocks.",
            )

        key = stdscr.getch()
        if key in (ord("q"), 27):
            return None
        if key in (curses.KEY_DOWN, ord("j")) and index < len(visible_indexes) - 1:
            index += 1
        elif key in (curses.KEY_UP, ord("k")) and index > 0:
            index -= 1
        elif key == curses.KEY_NPAGE:
            index = min(len(visible_indexes) - 1, index + max(1, height - 6))
        elif key == curses.KEY_PPAGE:
            index = max(0, index - max(1, height - 6))
        elif key == ord(" ") and visible_indexes:
            selections[visible_indexes[index]].selected = not selections[visible_indexes[index]].selected
        elif key == ord("e"):
            new_replacement = tui_prompt(stdscr, "replacement: ")
            if new_replacement:
                replacement = new_replacement
        elif key == ord("/"):
            query = tui_prompt(stdscr, "find/filter transcript: ")
            filter_text = query
            if query:
                query_lower = query.lower()
                visible_indexes = [
                    idx
                    for idx, selection in enumerate(selections)
                    if (show_meta or not is_low_value_transcript_item(selection.item))
                    and (
                        query_lower in selection.item.title.lower()
                        or query_lower in readable_transcript_body(selection.item).lower()
                    )
                ]
            else:
                visible_indexes = transcript_visible_indexes(selections, show_meta)
            index = 0
            offset = 0
        elif key == ord("c"):
            filter_text = ""
            visible_indexes = transcript_visible_indexes(selections, show_meta)
            index = 0
            offset = 0
        elif key == ord("m"):
            show_meta = not show_meta
            if filter_text:
                query_lower = filter_text.lower()
                visible_indexes = [
                    idx
                    for idx, selection in enumerate(selections)
                    if (show_meta or not is_low_value_transcript_item(selection.item))
                    and (
                        query_lower in selection.item.title.lower()
                        or query_lower in readable_transcript_body(selection.item).lower()
                    )
                ]
            else:
                visible_indexes = transcript_visible_indexes(selections, show_meta)
            index = 0
            offset = 0
        elif key == ord("r"):
            chosen = [item.item for item in selections if item.selected]
            if not chosen:
                tui_status(stdscr, "No transcript blocks marked. Press any key.")
                stdscr.getch()
                continue
            confirm = tui_prompt(stdscr, f"Type REDACT to replace {len(chosen)} marked blocks: ")
            if confirm == "REDACT":
                return chosen, replacement


def run_tui(stdscr, args: argparse.Namespace) -> None:
    tui_curs_set(0)
    stdscr.keypad(True)
    sessions = [p for p in iter_jsonl_files() if not is_subagent_path(p)]
    if args.project_contains:
        sessions = [p for p in sessions if args.project_contains.lower() in str(p).lower()]
    if not sessions:
        raise SystemExit("No Claude Code sessions found.")
    selected_session = tui_select_session(stdscr, sessions)
    if selected_session is None:
        return
    if args.query:
        matches = search_text(selected_session, args.query)
        if not matches:
            stdscr.erase()
            tui_draw_header(stdscr, "ctxscrub Claude", "No matches found. Press any key.")
            stdscr.getch()
            return
        review = tui_review_matches(stdscr, selected_session, args.query, matches)
        if review is None:
            return
        chosen, replacement = review
        applied, backup = apply_selected_redaction(
            selected_session,
            args.query,
            replacement,
            chosen,
            args.backup_dir,
            args.recent_threshold_seconds,
            args.allow_recent,
        )
        remaining_text = f"Remaining matches for query: {len(search_text(selected_session, args.query))}"
        selected_label = f"Selected fields: {len(chosen)}"
    else:
        review = tui_browse_transcript(stdscr, selected_session)
        if review is None:
            return
        chosen, replacement = review
        applied, backup = apply_transcript_redaction(
            selected_session,
            replacement,
            chosen,
            args.backup_dir,
            args.recent_threshold_seconds,
            args.allow_recent,
        )
        remaining_text = "Transcript blocks redacted by selection."
        selected_label = f"Marked blocks: {len(chosen)}"
    if review is None:
        return
    stdscr.erase()
    tui_draw_header(stdscr, "ctxscrub Claude - redaction complete")
    lines = [
        f"Redacted values: {applied}",
        selected_label,
        f"Backup: {backup}",
        remaining_text,
        "",
        "Press any key to exit.",
    ]
    for row, line in enumerate(lines, start=2):
        tui_write(stdscr, row, 0, line)
    stdscr.getch()


def cmd_tui(args: argparse.Namespace) -> None:
    curses.wrapper(run_tui, args)


def cmd_clean_prompt(args: argparse.Namespace) -> None:
    for path in select_paths(args):
        matches = search_text(path, args.query)
        print(f"# Clean Continuation Prompt For Claude Code")
        print()
        print(f"Session: `{path.stem}`")
        print(f"Source: `{path}`")
        print()
        print("## Context Cleanup")
        print()
        print(f"Redact this exact phrase before continuing: `{args.query}`")
        print(f"Matching fields found: {len(matches)}")
        print()
        print("## Suggested Next Prompt")
        print()
        print("Continue from this session, but ignore any previous details that were replaced with `[CTX_SCRUB_REDACTED]`.")
        print("Use only the remaining current task, decisions, files, and verified results.")


def cmd_backups(args: argparse.Namespace) -> None:
    backups = iter_backups(args.backup_dir, args.session_id)
    if not backups:
        print("backups=0")
        return
    for backup in backups[: args.limit]:
        when = datetime.fromtimestamp(backup.stat().st_mtime, tz=timezone.utc).isoformat()
        print(f"{when} size={backup.stat().st_size} backup={backup}")


def cmd_restore(args: argparse.Namespace) -> None:
    targets = select_paths(args)
    if len(targets) != 1:
        raise SystemExit("Restore needs exactly one target selected by --session-id or --path")
    target = targets[0]
    backup = Path(args.backup).expanduser() if args.backup else None
    if backup is None:
        backups = iter_backups(args.backup_dir, target.stem)
        if not backups:
            raise SystemExit(f"No backups found for session {target.stem}")
        backup = backups[0]
    if not backup.exists():
        raise SystemExit(f"Backup not found: {backup}")

    assert_not_recently_modified(target, args.recent_threshold_seconds, args.allow_recent)
    pre_restore = backup_file(target, args.backup_dir)
    shutil.copy2(backup, target)
    rows = read_rows(target)
    refs, missing = validate_parent_chain(rows)
    audit(
        "restore",
        path=str(target),
        restored_from=str(backup),
        pre_restore_backup=str(pre_restore),
        session_id=target.stem,
        missing_parent_refs=missing,
    )
    print(f"restored={target}")
    print(f"from_backup={backup}")
    print(f"pre_restore_backup={pre_restore}")
    print(f"rows={len(rows)} parent_refs={refs} missing_parent_refs={missing}")
    if missing:
        raise SystemExit(1)


def cmd_doctor(args: argparse.Namespace) -> None:
    files = iter_jsonl_files()
    top_level = [p for p in files if not is_subagent_path(p)]
    backups = iter_backups(None, None)
    print(f"ctx-scrub-claude version={VERSION}")
    print(f"claude_projects={CLAUDE_PROJECTS} exists={CLAUDE_PROJECTS.exists()}")
    print(f"jsonl_sessions={len(files)} top_level_sessions={len(top_level)}")
    print(f"default_backup_dir={DEFAULT_BACKUP_DIR} exists={DEFAULT_BACKUP_DIR.exists()} backups={len(backups)}")
    print(f"audit_log={AUDIT_LOG} exists={AUDIT_LOG.exists()}")
    if top_level:
        info = session_info(top_level[0])
        when = datetime.fromtimestamp(info.mtime, tz=timezone.utc).isoformat()
        print(f"latest_top_level={info.session_id} updated={when} project={project_label(info.path)}")
    print("status=ok")


def cmd_workflow(args: argparse.Namespace) -> None:
    print("v0.2 live Claude Code redaction workflow")
    print()
    print("Interactive flow:")
    print("   ./ctxscrub")
    print()
    print("Keys:")
    print("   j/k or arrows  move")
    print("   /              filter sessions")
    print("   space          mark/unmark match")
    print("   a              select all matches")
    print("   n              select none")
    print("   e              edit replacement")
    print("   r              redact selected matches")
    print("   q              quit")
    print()
    print("Scriptable flow:")
    print()
    print("1. Find the right session:")
    print("   ./ctx_scrub_claude.py list --project-contains '<project-name>' --limit 10")
    print()
    print("2. Inspect it:")
    print("   ./ctx_scrub_claude.py inspect --session-id <session-id>")
    print()
    print("3. Search exact text:")
    print("   ./ctx_scrub_claude.py search --session-id <session-id> --query '<text>'")
    print()
    print("4. Review interactively:")
    print("   ./ctx_scrub_claude.py review --session-id <session-id> --query '<text>'")
    print()
    print("5. Or dry-run then apply:")
    print("   ./ctx_scrub_claude.py redact --session-id <session-id> --query '<text>'")
    print("   ./ctx_scrub_claude.py redact --session-id <session-id> --query '<text>' --apply")
    print()
    print("6. Verify:")
    print("   ./ctx_scrub_claude.py verify --session-id <session-id> --query '<text>'")
    print()
    print("7. Roll back if needed:")
    print("   ./ctx_scrub_claude.py backups --session-id <session-id>")
    print("   ./ctx_scrub_claude.py restore --session-id <session-id>")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claude Code JSONL context scrubber prototype")
    parser.add_argument("--version", action="version", version=f"ctx-scrub-claude {VERSION}")
    parser.set_defaults(func=cmd_tui)
    parser.add_argument("--project-contains", help="Pre-filter TUI sessions whose path contains this text")
    parser.add_argument("--query", help="Pre-fill TUI search query")
    parser.add_argument("--backup-dir")
    parser.add_argument("--recent-threshold-seconds", type=int, default=15)
    parser.add_argument("--allow-recent", action="store_true", help="Allow mutation of a recently modified JSONL")
    sub = parser.add_subparsers(dest="cmd")

    def add_selectors(p: argparse.ArgumentParser) -> None:
        p.add_argument("--latest", action="store_true", help="Use the newest Claude JSONL session")
        p.add_argument("--session-id", help="Use a specific Claude session UUID")
        p.add_argument("--path", help="Use a specific JSONL path")
        p.add_argument("--project-contains", help="Only consider sessions whose path contains this text")
        p.add_argument("--last-minutes", type=int, help="Only consider sessions modified within this many minutes")
        p.add_argument("--include-subagents", action="store_true", help="Include subagent JSONL files")

    p_tui = sub.add_parser("tui", help="Open interactive terminal UI")
    p_tui.add_argument("--project-contains", help="Pre-filter sessions whose path contains this text")
    p_tui.add_argument("--query", help="Pre-fill search query")
    p_tui.add_argument("--backup-dir")
    p_tui.add_argument("--recent-threshold-seconds", type=int, default=15)
    p_tui.add_argument("--allow-recent", action="store_true", help="Allow mutation of a recently modified JSONL")
    p_tui.set_defaults(func=cmd_tui)

    p_list = sub.add_parser("list", help="List Claude JSONL sessions")
    add_selectors(p_list)
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=list_sessions)

    p_inspect = sub.add_parser("inspect", help="Inspect selected Claude JSONL sessions")
    add_selectors(p_inspect)
    p_inspect.set_defaults(func=cmd_inspect)

    p_search = sub.add_parser("search", help="Search for exact text")
    add_selectors(p_search)
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--limit", type=int, default=None, help="Maximum matches to show per file")
    p_search.set_defaults(func=cmd_search)

    p_review = sub.add_parser("review", help="Interactively review and redact exact text")
    add_selectors(p_review)
    p_review.add_argument("--query", required=True)
    p_review.add_argument("--replacement")
    p_review.add_argument("--backup-dir")
    p_review.add_argument("--limit", type=int, default=30, help="Maximum matches to show per file")
    p_review.add_argument("--recent-threshold-seconds", type=int, default=15)
    p_review.add_argument("--allow-recent", action="store_true", help="Allow mutation of a recently modified JSONL")
    p_review.set_defaults(func=cmd_review)

    p_verify = sub.add_parser("verify", help="Verify JSONL parse, parent links, and optional query absence")
    add_selectors(p_verify)
    p_verify.add_argument("--query")
    p_verify.set_defaults(func=cmd_verify)

    p_redact = sub.add_parser("redact", help="Replace exact text in selected JSONL files")
    add_selectors(p_redact)
    p_redact.add_argument("--query", required=True)
    p_redact.add_argument("--replacement")
    p_redact.add_argument("--backup-dir")
    p_redact.add_argument("--apply", action="store_true", help="Actually write changes; default is dry-run")
    p_redact.add_argument("--recent-threshold-seconds", type=int, default=15)
    p_redact.add_argument("--allow-recent", action="store_true", help="Allow mutation of a recently modified JSONL")
    p_redact.set_defaults(func=cmd_redact)

    p_clean = sub.add_parser("clean-prompt", help="Generate a clean continuation prompt after redaction")
    add_selectors(p_clean)
    p_clean.add_argument("--query", required=True)
    p_clean.set_defaults(func=cmd_clean_prompt)

    p_backups = sub.add_parser("backups", help="List ctx-scrub backups")
    p_backups.add_argument("--session-id", help="Filter backups by session UUID")
    p_backups.add_argument("--backup-dir")
    p_backups.add_argument("--limit", type=int, default=20)
    p_backups.set_defaults(func=cmd_backups)

    p_restore = sub.add_parser("restore", help="Restore a selected Claude JSONL session from backup")
    add_selectors(p_restore)
    p_restore.add_argument("--backup", help="Backup file to restore from; defaults to latest backup for session")
    p_restore.add_argument("--backup-dir")
    p_restore.add_argument("--recent-threshold-seconds", type=int, default=15)
    p_restore.add_argument("--allow-recent", action="store_true", help="Allow restore over a recently modified JSONL")
    p_restore.set_defaults(func=cmd_restore)

    p_doctor = sub.add_parser("doctor", help="Check local Claude/ctx-scrub readiness")
    p_doctor.set_defaults(func=cmd_doctor)

    p_workflow = sub.add_parser("workflow", help="Print the recommended v0.1 live-session workflow")
    p_workflow.set_defaults(func=cmd_workflow)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
