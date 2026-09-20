"""
Kampus UW Downloader - desktop GUI (customtkinter).

Usage:
    python gui.py

CLI alternatives still work:
    python main.py
    python test_run.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from kampus.config import load_config
from kampus.gui import App


PROJECT_ROOT = Path(__file__).resolve().parent


def _setup_logging() -> None:
    try:
        cfg = load_config(PROJECT_ROOT, require_credentials=False)
        level_name = cfg.log_level
    except Exception:
        level_name = "INFO"
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=Console(), rich_tracebacks=True, show_path=False)],
    )


def main() -> int:
    _setup_logging()
    try:
        app = App(project_root=PROJECT_ROOT)
    except ValueError as exc:
        # load_config raised - usually missing DOWNLOAD_BASE_PATH in .env
        print(f"Konfiguracja: {exc}")
        print("Skopiuj .env.example do .env i uzupelnij DOWNLOAD_BASE_PATH.")
        return 2
    except ImportError as exc:
        print(f"Brak zaleznosci: {exc}")
        print("Uruchom: pip install -r requirements.txt")
        return 2

    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
