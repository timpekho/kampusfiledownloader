from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    url TEXT PRIMARY KEY,
    course_url TEXT NOT NULL,
    local_path TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    content_length INTEGER,
    sha256 TEXT,
    downloaded_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_course ON files(course_url);
"""


@dataclass(frozen=True)
class ManifestRecord:
    url: str
    course_url: str
    local_path: str
    etag: Optional[str]
    last_modified: Optional[str]
    content_length: Optional[int]
    sha256: Optional[str]
    downloaded_at: str


def _parse_http_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


class Manifest:
    """
    Thread-safe SQLite-backed manifest of downloaded files.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get(self, url: str) -> Optional[ManifestRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE url = ?", (url,)
            ).fetchone()
        if row is None:
            return None
        return ManifestRecord(
            url=row["url"],
            course_url=row["course_url"],
            local_path=row["local_path"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            content_length=row["content_length"],
            sha256=row["sha256"],
            downloaded_at=row["downloaded_at"],
        )

    def all_records(self) -> Iterable[ManifestRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM files").fetchall()
        for row in rows:
            yield ManifestRecord(
                url=row["url"],
                course_url=row["course_url"],
                local_path=row["local_path"],
                etag=row["etag"],
                last_modified=row["last_modified"],
                content_length=row["content_length"],
                sha256=row["sha256"],
                downloaded_at=row["downloaded_at"],
            )

    def records_for_course(self, course_url: str) -> list[ManifestRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE course_url = ? ORDER BY downloaded_at DESC",
                (course_url,),
            ).fetchall()
        return [
            ManifestRecord(
                url=row["url"],
                course_url=row["course_url"],
                local_path=row["local_path"],
                etag=row["etag"],
                last_modified=row["last_modified"],
                content_length=row["content_length"],
                sha256=row["sha256"],
                downloaded_at=row["downloaded_at"],
            )
            for row in rows
        ]

    def should_download(self, url: str, head_headers: Mapping[str, str]) -> bool:
        """
        Decide whether to (re)download given a URL and freshly fetched HEAD headers.

        Rules (in order):
          1. No record -> True
          2. Local file missing -> True
          3. ETag matches recorded -> False
          4. Last-Modified <= recorded -> False
          5. Content-Length differs from recorded -> True
          6. Default -> False (skip)
        """
        rec = self.get(url)
        if rec is None:
            return True
        if not Path(rec.local_path).exists():
            return True

        new_etag = head_headers.get("ETag") or head_headers.get("etag")
        if rec.etag and new_etag and rec.etag == new_etag:
            return False

        new_lm_str = head_headers.get("Last-Modified") or head_headers.get("last-modified")
        old_lm = _parse_http_date(rec.last_modified)
        new_lm = _parse_http_date(new_lm_str)
        if old_lm and new_lm and new_lm <= old_lm:
            return False

        new_len_str = head_headers.get("Content-Length") or head_headers.get("content-length")
        try:
            new_len = int(new_len_str) if new_len_str is not None else None
        except (TypeError, ValueError):
            new_len = None
        if rec.content_length is not None and new_len is not None and rec.content_length != new_len:
            return True

        return False

    def upsert(
        self,
        *,
        url: str,
        course_url: str,
        local_path: Path,
        head_headers: Mapping[str, str],
        sha256: Optional[str],
    ) -> None:
        etag = head_headers.get("ETag") or head_headers.get("etag")
        last_modified = head_headers.get("Last-Modified") or head_headers.get("last-modified")
        content_length_raw = head_headers.get("Content-Length") or head_headers.get("content-length")
        try:
            content_length = int(content_length_raw) if content_length_raw is not None else None
        except (TypeError, ValueError):
            content_length = None

        downloaded_at = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO files (url, course_url, local_path, etag, last_modified,
                                   content_length, sha256, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    course_url=excluded.course_url,
                    local_path=excluded.local_path,
                    etag=excluded.etag,
                    last_modified=excluded.last_modified,
                    content_length=excluded.content_length,
                    sha256=excluded.sha256,
                    downloaded_at=excluded.downloaded_at
                """,
                (
                    url,
                    course_url,
                    str(local_path),
                    etag,
                    last_modified,
                    content_length,
                    sha256,
                    downloaded_at,
                ),
            )

    def verify(self) -> list[tuple[ManifestRecord, str]]:
        """
        Recomputes SHA-256 of local files and returns list of (record, reason)
        for files that differ from manifest or are missing.
        """
        problems: list[tuple[ManifestRecord, str]] = []
        for rec in self.all_records():
            p = Path(rec.local_path)
            if not p.exists():
                problems.append((rec, "missing"))
                continue
            if not rec.sha256:
                continue
            sha = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    sha.update(chunk)
            if sha.hexdigest() != rec.sha256:
                problems.append((rec, "sha256-mismatch"))
        return problems
