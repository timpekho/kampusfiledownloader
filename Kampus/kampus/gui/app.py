"""Main customtkinter application class."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import customtkinter as ctk
import requests

from ..auth import create_session, ensure_logged_in
from ..config import Config, load_config
from ..manifest import Manifest
from .workers import EventBus, Event


log = logging.getLogger(__name__)


class App(ctk.CTk):
    """
    Top-level GUI window. Owns shared state (config, manifest, http session)
    and hosts a CTkTabview with one tab per workflow step.
    """

    def __init__(self, project_root: Path) -> None:
        super().__init__()

        self.project_root = project_root
        self.cfg: Config = load_config(project_root, require_credentials=False)
        self.manifest = Manifest(self.cfg.manifest_path)
        self.bus = EventBus()
        self._session: Optional[requests.Session] = None
        self._session_lock = threading.Lock()
        self._session_logged_in = False
        self.last_crawl: list = []  # filled by download tab

        # Status bar counters.
        self.counters = {
            "found": 0,
            "selected": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "staged": 0,
        }

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.title("Kampus UW Downloader")
        self.geometry("1000x720")
        self.minsize(820, 560)

        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._poll_bus)

    # ----- layout --------------------------------------------------------

    def _build_layout(self) -> None:
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.tabview = ctk.CTkTabview(self)
        self.tabview.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 4))

        # Lazy import: tabs depend on App so we import after class body.
        from .tabs.account import AccountTab
        from .tabs.courses import CoursesTab
        from .tabs.download import DownloadTab
        from .tabs.notebooklm import NotebookLMTab

        for name in ("Konto", "Kursy", "Pobieranie", "NotebookLM"):
            self.tabview.add(name)

        self.tab_account = AccountTab(self.tabview.tab("Konto"), app=self)
        self.tab_account.pack(fill="both", expand=True)

        self.tab_courses = CoursesTab(self.tabview.tab("Kursy"), app=self)
        self.tab_courses.pack(fill="both", expand=True)

        self.tab_download = DownloadTab(self.tabview.tab("Pobieranie"), app=self)
        self.tab_download.pack(fill="both", expand=True)

        self.tab_notebooklm = NotebookLMTab(self.tabview.tab("NotebookLM"), app=self)
        self.tab_notebooklm.pack(fill="both", expand=True)

        # Status bar
        self.status_var = ctk.StringVar(value=self._format_status())
        self.status_bar = ctk.CTkLabel(
            self,
            textvariable=self.status_var,
            anchor="w",
            font=ctk.CTkFont(size=12),
        )
        self.status_bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))

    def _format_status(self) -> str:
        c = self.counters
        return (
            f"Znaleziono: {c['found']}  |  "
            f"Wybrane: {c['selected']}  |  "
            f"Pobrano: {c['downloaded']}  |  "
            f"Pominieto: {c['skipped']}  |  "
            f"Bledy: {c['failed']}  |  "
            f"NotebookLM: {c['staged']}"
        )

    def update_status(self, **counter_updates: int) -> None:
        for k, v in counter_updates.items():
            if k in self.counters:
                self.counters[k] = v
        self.status_var.set(self._format_status())

    # ----- shared session ------------------------------------------------

    def get_session(self) -> requests.Session:
        with self._session_lock:
            if self._session is None:
                self._session = create_session()
            return self._session

    def reset_session(self) -> None:
        with self._session_lock:
            self._session = None
            self._session_logged_in = False

    def login_blocking(self, *, username: str, password: str, force_refresh: bool = False) -> bool:
        """Performs CAS login synchronously on the calling thread. Returns True on success."""
        session = self.get_session()
        result = ensure_logged_in(
            session,
            username=username,
            password=password,
            cookie_cache_path=self.cfg.cookie_cache_path,
            base_url=self.cfg.kampus_base_url,
            force_refresh=force_refresh,
        )
        with self._session_lock:
            self._session_logged_in = result.logged_in
        return result.logged_in

    @property
    def is_logged_in(self) -> bool:
        return self._session_logged_in

    # ----- bus polling ---------------------------------------------------

    def _poll_bus(self) -> None:
        try:
            self.bus.drain(self._dispatch_event)
        finally:
            self.after(120, self._poll_bus)

    def _dispatch_event(self, ev: Event) -> None:
        # Fan-out to whichever tab cares about this tag.
        handler = getattr(self, f"_handle_{ev.tag}", None)
        if handler is not None:
            handler(ev)
        else:
            # Unhandled events are not fatal - just trace.
            log.debug("Unhandled event tag=%s kind=%s", ev.tag, ev.kind)

    # Per-tab routing - tabs register handlers by setting attributes on App.
    def _handle_login(self, ev: Event) -> None:
        if hasattr(self, "tab_account"):
            self.tab_account.handle_event(ev)

    def _handle_crawl(self, ev: Event) -> None:
        if hasattr(self, "tab_download"):
            self.tab_download.handle_event(ev)

    def _handle_download(self, ev: Event) -> None:
        if hasattr(self, "tab_download"):
            self.tab_download.handle_event(ev)

    def _handle_stage(self, ev: Event) -> None:
        if hasattr(self, "tab_notebooklm"):
            self.tab_notebooklm.handle_event(ev)

    # ----- shutdown ------------------------------------------------------

    def _on_close(self) -> None:
        try:
            self.manifest.close()
        except Exception:
            pass
        self.destroy()
