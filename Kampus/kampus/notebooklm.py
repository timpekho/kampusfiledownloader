"""
NotebookLM staging - copies/orgaznises downloaded files into a folder ready
to be drag-and-dropped into a NotebookLM notebook.
"""

from __future__ import annotations

import logging
import shutil
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .manifest import Manifest, ManifestRecord
from .sanitize import sanitize_component


log = logging.getLogger(__name__)


NOTEBOOKLM_URL = "https://notebooklm.google.com/"

# NotebookLM accepts these natively (PDF/text-like). DOCX/PPTX/etc. need conversion.
SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown", ".htm", ".html"}

# NotebookLM rejects sources larger than 200 MB.
MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024


@dataclass
class StageResult:
    course_name: str
    dest_dir: Path
    staged: List[Path] = field(default_factory=list)
    skipped: List[tuple[ManifestRecord, str]] = field(default_factory=list)

    @property
    def staged_count(self) -> int:
        return len(self.staged)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _human_size(num_bytes: Optional[int]) -> str:
    if num_bytes is None:
        return "?"
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _classify(record: ManifestRecord) -> tuple[bool, Optional[str]]:
    """
    Returns (should_stage, skip_reason).
    """
    p = Path(record.local_path)
    if not p.exists():
        return False, "lokalny plik nie istnieje (uruchom pobieranie ponownie)"

    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return False, f"rozszerzenie {ext or '<brak>'} - NotebookLM nie przyjmie bezposrednio"

    try:
        size = p.stat().st_size
    except OSError as exc:
        return False, f"blad odczytu rozmiaru: {exc}"

    if size > MAX_FILE_SIZE_BYTES:
        return False, f"za duzy ({_human_size(size)} > 200 MB)"

    return True, None


def stage_course(
    manifest: Manifest,
    *,
    course_url: str,
    course_name: str,
    base_path: Path,
    overwrite: bool = True,
) -> StageResult:
    """
    Copies files matching `course_url` from manifest into a NotebookLM-ready folder.

    Returns a `StageResult` with lists of staged Paths and skipped records.
    """
    safe_course = sanitize_component(course_name or "Untitled", max_len=120) or "Untitled"
    dest_dir = base_path / "_NotebookLM" / safe_course
    dest_dir.mkdir(parents=True, exist_ok=True)

    result = StageResult(course_name=course_name, dest_dir=dest_dir)

    records = manifest.records_for_course(course_url)
    log.debug("Staging %d records for course=%s", len(records), course_url)

    used_names: set[str] = set()
    for rec in records:
        ok, reason = _classify(rec)
        if not ok:
            result.skipped.append((rec, reason or "unknown"))
            continue

        src = Path(rec.local_path)
        # Use the sanitized destination filename; uniquify if collision within destination.
        dest_name = src.name
        candidate = dest_dir / dest_name
        i = 2
        while dest_name in used_names or (not overwrite and candidate.exists()):
            stem = src.stem
            suffix = src.suffix
            dest_name = f"{stem} ({i}){suffix}"
            candidate = dest_dir / dest_name
            i += 1
        used_names.add(dest_name)

        try:
            shutil.copy2(src, candidate)
            result.staged.append(candidate)
        except OSError as exc:
            result.skipped.append((rec, f"blad kopiowania: {exc}"))

    _write_index(result, course_url=course_url, records=records)
    return result


def _write_index(
    result: StageResult,
    *,
    course_url: str,
    records: List[ManifestRecord],
) -> None:
    """Generates `_index.md` listing staged files and skipped originals with reasons."""
    index_path = result.dest_dir / "_index.md"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Map staged path back to its source record for the index.
    by_path = {str(Path(r.local_path).resolve()): r for r in records}

    staged_lines: list[str] = []
    for p in result.staged:
        # Find the matching record (best-effort by name).
        match = None
        for r in records:
            if Path(r.local_path).name == p.name or by_path.get(str(p.resolve())) is r:
                match = r
                break
        size = _human_size(p.stat().st_size if p.exists() else None)
        if match:
            staged_lines.append(f"- `{p.name}` ({size}) - {match.url}")
        else:
            staged_lines.append(f"- `{p.name}` ({size})")

    skipped_lines = [
        f"- `{Path(r.local_path).name}` - {reason}" for r, reason in result.skipped
    ]

    body = [
        f"# {result.course_name}",
        "",
        f"Wygenerowano: {now}",
        f"Kurs: {course_url}",
        "",
        f"## Pliki gotowe do NotebookLM ({len(staged_lines)})",
        "",
        "Przeciagnij ten folder (lub same pliki) na pole 'Add source' w NotebookLM.",
        "",
        *(staged_lines or ["_(brak)_"]),
        "",
        f"## Pominiete ({len(skipped_lines)})",
        "",
        "Te pliki nie zostaly skopiowane - NotebookLM ich nie obsluguje natywnie.",
        "Mozesz je skonwertowac recznie do PDF i wrzucic osobno.",
        "",
        *(skipped_lines or ["_(brak)_"]),
        "",
    ]

    try:
        index_path.write_text("\n".join(body), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write index file %s: %s", index_path, exc)


def open_notebooklm_in_browser() -> bool:
    """Opens NotebookLM in the default web browser. Returns True on success."""
    try:
        return bool(webbrowser.open(NOTEBOOKLM_URL))
    except Exception as exc:
        log.warning("Could not open browser: %s", exc)
        return False
