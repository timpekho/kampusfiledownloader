from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests

from .crawler import FileCandidate, parse_filename
from .manifest import Manifest
from .sanitize import sanitize_component, sanitize_filename, uniquify_path


log = logging.getLogger(__name__)


class ResultStatus(str, Enum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class DownloadResult:
    candidate: FileCandidate
    status: ResultStatus
    local_path: Optional[Path] = None
    error: Optional[str] = None


class HostRateLimiter:
    """Per-host concurrency limiter (thread-safe)."""

    def __init__(self, max_per_host: int) -> None:
        self._max = max(1, max_per_host)
        self._lock = threading.Lock()
        self._semaphores: Dict[str, threading.Semaphore] = {}

    def acquire(self, url: str) -> threading.Semaphore:
        host = urlparse(url).netloc.lower()
        with self._lock:
            sem = self._semaphores.get(host)
            if sem is None:
                sem = threading.Semaphore(self._max)
                self._semaphores[host] = sem
        return sem


def _final_filename(
    candidate: FileCandidate,
    response_headers: Optional[Dict[str, str]],
) -> str:
    name: Optional[str] = None
    if response_headers:
        name = parse_filename(response_headers, candidate.url)
    if not name:
        name = candidate.suggested_filename
    if not name:
        name = urlparse(candidate.url).path.rsplit("/", 1)[-1] or "file"

    # If filename has no extension, try to guess from Content-Type.
    if "." not in name and response_headers:
        ct = response_headers.get("Content-Type") or response_headers.get("content-type")
        if ct:
            ext = mimetypes.guess_extension(ct.split(";")[0].strip())
            if ext:
                name = f"{name}{ext}"

    return sanitize_filename(name)


def _build_target_path(
    base_path: Path,
    candidate: FileCandidate,
    response_headers: Optional[Dict[str, str]],
    manifest: Manifest,
) -> Path:
    course_dir = sanitize_component(candidate.course_name or "Untitled", max_len=120)
    filename = _final_filename(candidate, response_headers)

    parent = base_path / course_dir
    if candidate.section_name:
        section_dir = sanitize_component(candidate.section_name, max_len=120)
        if section_dir:
            parent = parent / section_dir

    target = parent / filename
    target.parent.mkdir(parents=True, exist_ok=True)

    record = manifest.get(candidate.url)
    if record and record.local_path == str(target):
        # Updating the same file - overwrite is fine.
        return target

    if target.exists():
        return uniquify_path(target)
    return target


def _is_unauthorized(response: requests.Response) -> bool:
    if response.status_code in (401, 403):
        return True
    final = str(response.url)
    if "/login/" in final or "logowanie.uw.edu.pl" in final:
        return True
    return False


def _stream_to_disk(
    response: requests.Response,
    tmp_path: Path,
    *,
    append: bool,
    sha: hashlib._Hash,
    seed_bytes: bytes = b"",
    chunk_size: int = 256 * 1024,
) -> None:
    mode = "ab" if append else "wb"
    if seed_bytes and append:
        sha.update(seed_bytes)
    with open(tmp_path, mode) as f:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            f.write(chunk)
            sha.update(chunk)


def _download_one(
    candidate: FileCandidate,
    *,
    session: requests.Session,
    manifest: Manifest,
    base_path: Path,
    rate_limiter: HostRateLimiter,
    refresh_auth: Optional[Callable[[], bool]],
    timeout: float = 60.0,
) -> DownloadResult:
    if not manifest.should_download(candidate.url, candidate.head_headers):
        return DownloadResult(candidate, ResultStatus.SKIPPED)

    sem = rate_limiter.acquire(candidate.url)
    sem.acquire()
    try:
        return _do_download(
            candidate=candidate,
            session=session,
            manifest=manifest,
            base_path=base_path,
            refresh_auth=refresh_auth,
            timeout=timeout,
        )
    finally:
        sem.release()


def _do_download(
    *,
    candidate: FileCandidate,
    session: requests.Session,
    manifest: Manifest,
    base_path: Path,
    refresh_auth: Optional[Callable[[], bool]],
    timeout: float,
    _retry: bool = True,
) -> DownloadResult:
    headers: Dict[str, str] = {}

    # Best-effort path resolution before requesting (so we know where .part lives).
    target = _build_target_path(base_path, candidate, None, manifest)
    tmp = target.with_suffix(target.suffix + ".part")

    seed_bytes = b""
    if tmp.exists() and tmp.stat().st_size > 0:
        size = tmp.stat().st_size
        headers["Range"] = f"bytes={size}-"
        with open(tmp, "rb") as f:
            seed_bytes = f.read()

    try:
        with session.get(
            candidate.url,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=timeout,
        ) as r:
            if _is_unauthorized(r):
                if _retry and refresh_auth and refresh_auth():
                    return _do_download(
                        candidate=candidate,
                        session=session,
                        manifest=manifest,
                        base_path=base_path,
                        refresh_auth=refresh_auth,
                        timeout=timeout,
                        _retry=False,
                    )
                return DownloadResult(
                    candidate, ResultStatus.FAILED, error=f"unauthorized ({r.status_code})"
                )

            r.raise_for_status()

            # Recompute target with response headers (better filename).
            target = _build_target_path(base_path, candidate, dict(r.headers), manifest)
            tmp = target.with_suffix(target.suffix + ".part")
            target.parent.mkdir(parents=True, exist_ok=True)

            sha = hashlib.sha256()
            append = "Range" in headers and r.status_code == 206
            if not append and tmp.exists():
                tmp.unlink()
                seed_bytes = b""
            _stream_to_disk(r, tmp, append=append, sha=sha, seed_bytes=seed_bytes)

            os.replace(tmp, target)

            manifest.upsert(
                url=candidate.url,
                course_url=candidate.course_url,
                local_path=target,
                head_headers=dict(r.headers),
                sha256=sha.hexdigest(),
            )
            log.info("Downloaded %s -> %s", candidate.url, target)
            return DownloadResult(candidate, ResultStatus.DOWNLOADED, local_path=target)
    except requests.RequestException as exc:
        return DownloadResult(candidate, ResultStatus.FAILED, error=str(exc))
    except OSError as exc:
        return DownloadResult(candidate, ResultStatus.FAILED, error=str(exc))


def download_all(
    candidates: Iterable[FileCandidate],
    *,
    session: requests.Session,
    manifest: Manifest,
    base_path: Path,
    max_workers: int = 6,
    max_per_host: int = 4,
    refresh_auth: Optional[Callable[[], bool]] = None,
    on_result: Optional[Callable[[DownloadResult], None]] = None,
    timeout: float = 60.0,
) -> List[DownloadResult]:
    items = list(candidates)
    if not items:
        return []

    rate_limiter = HostRateLimiter(max_per_host)
    results: List[DownloadResult] = []

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dl") as pool:
        futures: List[Future[DownloadResult]] = [
            pool.submit(
                _download_one,
                c,
                session=session,
                manifest=manifest,
                base_path=base_path,
                rate_limiter=rate_limiter,
                refresh_auth=refresh_auth,
                timeout=timeout,
            )
            for c in items
        ]
        for f in as_completed(futures):
            res = f.result()
            results.append(res)
            if on_result is not None:
                try:
                    on_result(res)
                except Exception:
                    log.exception("on_result callback raised")
    return results
