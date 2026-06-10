#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ctx_scrub_claude.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ctx_scrub_claude", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ctx_scrub_claude"] = module
    spec.loader.exec_module(module)
    return module


def write_fixture(path: Path) -> None:
    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "message": {"role": "user", "content": "keep ALPHA and remove SECRET_BETA"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "saw SECRET_BETA"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/SECRET_BETA.txt"}},
                ],
            },
        },
        {
            "type": "last-prompt",
            "lastPrompt": "remove SECRET_BETA",
            "leafUuid": "a1",
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class CtxScrubClaudeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fixture-session.jsonl"
        write_fixture(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_search_reports_field_paths(self) -> None:
        matches = self.mod.search_text(self.path, "SECRET_BETA")
        self.assertEqual(len(matches), 4)
        self.assertEqual(
            {match.field_path for match in matches},
            {
                "$.message.content",
                "$.message.content[0].text",
                "$.message.content[1].input.file_path",
                "$.lastPrompt",
            },
        )

    def test_structured_redaction_preserves_parent_links(self) -> None:
        rows = self.mod.read_rows(self.path)
        new_rows, count = self.mod.redact_rows(rows, "SECRET_BETA", "[REDACTED]")
        self.assertEqual(count, 4)
        refs, missing = self.mod.validate_parent_chain(new_rows)
        self.assertEqual(refs, 1)
        self.assertEqual(missing, 0)
        dumped = "".join(self.mod.json_dumps(row) for row in new_rows)
        self.assertNotIn("SECRET_BETA", dumped)
        self.assertIn("[REDACTED]", dumped)

    def test_selected_redaction_only_changes_marked_fields(self) -> None:
        matches = self.mod.search_text(self.path, "SECRET_BETA")
        selected = [match for match in matches if match.field_path == "$.message.content"]
        rows = self.mod.read_rows(self.path)
        new_rows, count = self.mod.redact_selected_matches(rows, "SECRET_BETA", "[REDACTED]", selected)
        self.assertEqual(count, 1)
        dumped = "".join(self.mod.json_dumps(row) for row in new_rows)
        self.assertIn("[REDACTED]", dumped)
        self.assertEqual(dumped.count("SECRET_BETA"), 3)

    def test_transcript_browser_surfaces_messages_and_tool_calls(self) -> None:
        rows = self.mod.read_rows(self.path)
        transcript = self.mod.build_transcript(rows)
        self.assertEqual([item.kind for item in transcript], ["text", "text", "tool_use", "lastPrompt"])
        self.assertIn("remove SECRET_BETA", transcript[0].body)
        self.assertIn("/tmp/SECRET_BETA.txt", self.mod.readable_transcript_body(transcript[2]))
        self.assertEqual(transcript[2].role, "tool call")

    def test_session_display_name_uses_bounded_preview(self) -> None:
        path = Path(self.tmp.name) / "preview-session.jsonl"
        first_row = {
            "type": "user",
            "uuid": "preview-u1",
            "message": {"role": "user", "content": "first useful session label"},
        }
        path.write_text(json.dumps(first_row) + "\n{not valid later json\n", encoding="utf-8")
        label = self.mod.session_display_name(path)
        self.assertIn("first useful session label", label)

    def test_transcript_block_renderer_wraps_readable_body(self) -> None:
        item = self.mod.TranscriptItem(
            line_no=7,
            role="assistant",
            kind="text",
            uuid="a7",
            field_path="$.message.content",
            title=self.mod.row_title({}, 7, "assistant", "text", ""),
            body="This is a longer answer that should wrap onto multiple body lines instead of becoming one unreadable row.",
        )
        lines = self.mod.render_transcript_block(item, width=52, current=True, selected=False, max_body_lines=4)
        self.assertTrue(lines[0].startswith("> [ ] ASSISTANT"))
        self.assertGreaterEqual(len([line for line in lines if line.startswith("    ")]), 2)

    def test_tool_block_renderer_shows_structured_input(self) -> None:
        item = self.mod.TranscriptItem(
            line_no=8,
            role="tool call",
            kind="tool_use",
            uuid="tool-8",
            field_path="$.message.content[1]",
            title="TOOL Read  line 8",
            body={"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/SECRET_BETA.txt"}},
        )
        lines = self.mod.render_transcript_block(item, width=70, current=True, selected=False, max_body_lines=8)
        text = "\n".join(lines)
        self.assertIn("TOOL Read", lines[0])
        self.assertIn("input:", text)
        self.assertIn("file_path", text)

    def test_transcript_redaction_replaces_marked_block_only(self) -> None:
        rows = self.mod.read_rows(self.path)
        transcript = self.mod.build_transcript(rows)
        selected = [item for item in transcript if item.kind == "tool_use"]
        new_rows, count = self.mod.redact_selected_transcript_items(rows, "[REDACTED]", selected)
        self.assertEqual(count, 1)
        dumped = "".join(self.mod.json_dumps(row) for row in new_rows)
        self.assertIn("[REDACTED]", dumped)
        self.assertNotIn("/tmp/SECRET_BETA.txt", dumped)
        self.assertIn("remove SECRET_BETA", dumped)
        self.assertIn('"name":"Read"', dumped)

    def test_cli_redact_apply_and_verify(self) -> None:
        backup_dir = Path(self.tmp.name) / "backups"
        result = subprocess.run(
            [
                str(SCRIPT),
                "redact",
                "--path",
                str(self.path),
                "--query",
                "SECRET_BETA",
                "--replacement",
                "[REDACTED]",
                "--backup-dir",
                str(backup_dir),
                "--apply",
                "--allow-recent",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertIn("applied=true", result.stdout)
        self.assertNotIn("SECRET_BETA", self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(list(backup_dir.glob("*.bak"))), 1)

        verify = subprocess.run(
            [str(SCRIPT), "verify", "--path", str(self.path), "--query", "SECRET_BETA"],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertIn("contains_query=False", verify.stdout)

    def test_tui_status_clips_to_avoid_curses_edge_errors(self) -> None:
        class EdgeSensitiveWindow:
            def __init__(self) -> None:
                self.calls = []

            def getmaxyx(self):
                return (5, 10)

            def addnstr(self, row, col, text, n, attr=0):
                self.calls.append((row, col, text, n, attr))
                if row == 4 and col + n >= 10:
                    raise self.mod.curses.error("addnwstr() returned ERR")

        window = EdgeSensitiveWindow()
        window.mod = self.mod
        self.mod.tui_status(window, "this is deliberately wider than the terminal")
        self.assertEqual(window.calls[0][0], 4)
        self.assertLess(window.calls[0][3], 10)


if __name__ == "__main__":
    unittest.main()
