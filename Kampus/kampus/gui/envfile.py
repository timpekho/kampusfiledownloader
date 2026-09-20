"""
Tiny `.env` editor: upsert one or more KEY=VALUE pairs while preserving the
file's existing layout (other keys, comments and blank lines stay untouched).
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Iterable, Mapping, Tuple


_KEY_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _quote_if_needed(value: str) -> str:
    if value == "":
        return ""
    needs = any(ch.isspace() or ch in '#"\'' for ch in value)
    if not needs:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def upsert_env(path: Path, updates: Mapping[str, str]) -> None:
    """
    Insert or replace `KEY=value` lines for each item in `updates` while keeping
    everything else in `path` intact. Creates the file if missing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    output: list[str] = []
    for line in existing_lines:
        m = _KEY_LINE_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            output.append(f"{key}={_quote_if_needed(remaining.pop(key))}")
        else:
            output.append(line)

    # Append any keys that weren't already present.
    if remaining:
        if output and output[-1].strip() != "":
            output.append("")
        for key, value in remaining.items():
            output.append(f"{key}={_quote_if_needed(value)}")

    text = "\n".join(output)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")

    # best-effort restrictive perms (no-op on Windows)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
