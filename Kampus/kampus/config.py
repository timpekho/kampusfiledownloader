from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from dotenv import load_dotenv
import os


def _split_csv(value: str) -> List[str]:
    parts = [p.strip() for p in value.split(",")]
    return [p for p in parts if p]


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    kampus_base_url: str
    download_base_path: Path
    allowed_modules: List[str]
    max_workers: int
    max_per_host: int
    max_depth: int
    cookie_cache_path: Path
    manifest_path: Path
    log_level: str

    def with_credentials(self, username: str, password: str) -> "Config":
        from dataclasses import replace

        return replace(self, username=username, password=password)


def load_config(project_root: Path, *, require_credentials: bool = True) -> Config:
    """
    Loads config from `.env` (and process env). Paths in env can be relative to project root.

    If `require_credentials` is False, missing username/password are tolerated
    (e.g. when they will be supplied interactively).
    """
    load_dotenv(project_root / ".env")

    username = os.getenv("KAMPUS_USERNAME", "").strip()
    password = os.getenv("KAMPUS_PASSWORD", "").strip()
    kampus_base_url = os.getenv("KAMPUS_BASE_URL", "https://kampus-kursy.ckc.uw.edu.pl/").strip()
    if not kampus_base_url.endswith("/"):
        kampus_base_url += "/"
    download_base_path_raw = os.getenv("DOWNLOAD_BASE_PATH", "").strip()
    allowed_modules_raw = os.getenv("ALLOWED_MODULES", "").strip()

    max_workers = int(os.getenv("MAX_WORKERS", "12"))
    max_per_host = int(os.getenv("MAX_PER_HOST", "8"))
    max_depth = int(os.getenv("MAX_DEPTH", "3"))

    cookie_cache_path_raw = os.getenv("COOKIE_CACHE_PATH", ".cache/cookies.pickle").strip()
    manifest_path_raw = os.getenv("MANIFEST_PATH", ".cache/manifest.sqlite").strip()
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()

    if require_credentials:
        if not username:
            raise ValueError("Missing KAMPUS_USERNAME in .env")
        if not password:
            raise ValueError("Missing KAMPUS_PASSWORD in .env")
    if not download_base_path_raw:
        raise ValueError("Missing DOWNLOAD_BASE_PATH in .env")

    download_base_path = Path(download_base_path_raw).expanduser()
    cookie_cache_path = Path(cookie_cache_path_raw)
    manifest_path = Path(manifest_path_raw)

    if not cookie_cache_path.is_absolute():
        cookie_cache_path = (project_root / cookie_cache_path).resolve()
    if not manifest_path.is_absolute():
        manifest_path = (project_root / manifest_path).resolve()

    allowed_modules = _split_csv(allowed_modules_raw) or [
        "mod/resource",
        "mod/folder",
    ]

    return Config(
        username=username,
        password=password,
        kampus_base_url=kampus_base_url,
        download_base_path=download_base_path,
        allowed_modules=allowed_modules,
        max_workers=max_workers,
        max_per_host=max_per_host,
        max_depth=max_depth,
        cookie_cache_path=cookie_cache_path,
        manifest_path=manifest_path,
        log_level=log_level,
    )

