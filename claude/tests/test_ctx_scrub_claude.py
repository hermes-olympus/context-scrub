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
            "message": {"role": "assistant", "content": [{"type": "text", "text": "saw SECRET_BETA"}]},
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
        self.assertEqual(len(matches), 3)
        self.assertEqual(
            {match.field_path for match in matches},
            {"$.message.content", "$.message.content[0].text", "$.lastPrompt"},
        )

    def test_structured_redaction_preserves_parent_links(self) -> None:
        rows = self.mod.read_rows(self.path)
        new_rows, count = self.mod.redact_rows(rows, "SECRET_BETA", "[REDACTED]")
        self.assertEqual(count, 3)
        refs, missing = self.mod.validate_parent_chain(new_rows)
        self.assertEqual(refs, 1)
        self.assertEqual(missing, 0)
        dumped = "".join(self.mod.json_dumps(row) for row in new_rows)
        self.assertNotIn("SECRET_BETA", dumped)
        self.assertIn("[REDACTED]", dumped)

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


if __name__ == "__main__":
    unittest.main()
