from __future__ import annotations

import re
from pathlib import Path


_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|]+')
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f]+")

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_component(name: str, *, max_len: int = 200) -> str:
    """
    Sanitizes a single path component (file or directory name) to be safe on Windows/macOS/Linux.
    """
    name = name.strip()
    name = _CONTROL_CHARS_RE.sub("", name)
    name = _ILLEGAL_CHARS_RE.sub("_", name)

    # Windows trims trailing dots/spaces; avoid implicit collisions.
    name = name.rstrip(" .")

    if not name:
        name = "_"

    # Windows reserved device names.
    if name.upper() in _WINDOWS_RESERVED:
        name = f"_{name}"

    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
        if not name:
            name = "_"

    return name


def sanitize_filename(filename: str, *, max_len: int = 200) -> str:
    """
    Sanitizes filename but preserves extension if possible.
    """
    p = Path(filename)
    stem = p.stem or filename
    suffix = p.suffix if p.suffix and len(p.suffix) <= 20 else ""

    stem_clean = sanitize_component(stem, max_len=max_len)
    if suffix:
        # ensure full name length doesn't exceed max_len
        allowed = max_len - len(suffix)
        if allowed < 1:
            return sanitize_component(filename, max_len=max_len)
        stem_clean = sanitize_component(stem_clean, max_len=allowed)
        return f"{stem_clean}{suffix}"

    return sanitize_component(filename, max_len=max_len)


def uniquify_path(path: Path) -> Path:
    """
    If path exists, appends ' (2)', ' (3)' etc. before extension.
    """
    if not path.exists():
        return path

    parent = path.parent
    stem = path.stem
    suffix = path.suffix

    i = 2
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1

