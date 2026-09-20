"""NotebookLM - stage downloaded files into a drag-and-drop ready folder."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

import customtkinter as ctk

from ...manifest import ManifestRecord
from ...notebooklm import (
    SUPPORTED_EXTENSIONS,
    StageResult,
    open_notebooklm_in_browser,
    stage_course,
)
from ..workers import Event


if TYPE_CHECKING:
    from ..app import App


@dataclass
class _CourseEntry:
    course_url: str
    course_name: str
    records: List[ManifestRecord]
    supported_count: int
    unsupported_count: int

    @property
    def label(self) -> str:
        return (
            f"{self.course_name}  -  gotowe: {self.supported_count}, "
            f"pominiete: {self.unsupported_count}"
        )


class NotebookLMTab(ctk.CTkFrame):
    def __init__(self, master, *, app: "App") -> None:
        super().__init__(master)
        self.app = app

        self.grid_rowconfigure(4, weight=1)
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Eksport do NotebookLM",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=14, pady=(16, 4), sticky="w")

        ctk.CTkLabel(
            self,
            text=(
                "NotebookLM nie ma publicznego API. Skrypt skopiuje pliki obslugiwane "
                "natywnie (PDF/TXT/MD/HTML) do folderu '_NotebookLM/<kurs>/' i wygeneruje "
                "_index.md. Potem wystarczy kliknac 'Otworz NotebookLM' i przeciagnac "
                "tam folder lub same pliki."
            ),
            wraplength=900,
            justify="left",
            text_color=("gray40", "gray70"),
        ).grid(row=1, column=0, columnspan=3, padx=14, pady=(0, 12), sticky="w")

        ctk.CTkLabel(self, text="Kurs:").grid(row=2, column=0, padx=(14, 8), pady=8, sticky="e")
        self.course_var = ctk.StringVar(value="(brak danych - najpierw cos pobierz)")
        self.course_menu = ctk.CTkOptionMenu(
            self,
            variable=self.course_var,
            values=[self.course_var.get()],
            command=self._on_course_chosen,
        )
        self.course_menu.grid(row=2, column=1, padx=(0, 8), pady=8, sticky="ew")

        self.refresh_btn = ctk.CTkButton(
            self, text="Odswiez liste", width=120, command=self.refresh_courses
        )
        self.refresh_btn.grid(row=2, column=2, padx=(0, 14), pady=8, sticky="w")

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, columnspan=3, padx=14, pady=(8, 8), sticky="w")

        self.stage_btn = ctk.CTkButton(
            actions,
            text="Przygotuj folder dla NotebookLM",
            command=self._on_stage_click,
            fg_color="#1f7a3a",
            hover_color="#155a2c",
        )
        self.stage_btn.pack(side="left", padx=(0, 8))

        self.open_browser_btn = ctk.CTkButton(
            actions,
            text="Otworz NotebookLM",
            command=self._on_open_notebooklm,
        )
        self.open_browser_btn.pack(side="left", padx=(0, 8))

        self.open_folder_btn = ctk.CTkButton(
            actions,
            text="Otworz folder",
            command=self._on_open_staged_folder,
            state="disabled",
        )
        self.open_folder_btn.pack(side="left")

        self.log_box = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=4, column=0, columnspan=3, padx=14, pady=(8, 14), sticky="nsew")
        self.log_box.configure(state="disabled")

        self._entries: List[_CourseEntry] = []
        self._last_staged_dir: Optional[Path] = None

        self.refresh_courses()

    # --- public API used by other tabs ---------------------------------

    def refresh_courses(self) -> None:
        entries = self._collect_courses()
        self._entries = entries
        if not entries:
            self.course_menu.configure(values=["(brak danych - najpierw cos pobierz)"])
            self.course_var.set("(brak danych - najpierw cos pobierz)")
            self.stage_btn.configure(state="disabled")
            return
        labels = [e.label for e in entries]
        self.course_menu.configure(values=labels)
        if self.course_var.get() not in labels:
            self.course_var.set(labels[0])
        self.stage_btn.configure(state="normal")

    # --- collection ----------------------------------------------------

    def _collect_courses(self) -> List[_CourseEntry]:
        # Group manifest records by course_url.
        groups: Dict[str, List[ManifestRecord]] = defaultdict(list)
        for rec in self.app.manifest.all_records():
            groups[rec.course_url].append(rec)

        # Try to enrich with names from the most recent crawl.
        url_to_name: Dict[str, str] = {}
        for cf in getattr(self.app, "last_crawl", []) or []:
            url_to_name[cf.course_url] = cf.course_name

        entries: List[_CourseEntry] = []
        for course_url, recs in groups.items():
            if course_url in url_to_name:
                course_name = url_to_name[course_url]
            else:
                # Fallback: use the parent directory of any local file.
                course_name = self._guess_course_name_from_records(recs)
            supported = sum(
                1
                for r in recs
                if Path(r.local_path).suffix.lower() in SUPPORTED_EXTENSIONS
                and Path(r.local_path).exists()
            )
            unsupported = len(recs) - supported
            entries.append(
                _CourseEntry(
                    course_url=course_url,
                    course_name=course_name,
                    records=recs,
                    supported_count=supported,
                    unsupported_count=unsupported,
                )
            )
        entries.sort(key=lambda e: e.course_name.lower())
        return entries

    def _guess_course_name_from_records(self, recs: List[ManifestRecord]) -> str:
        for r in recs:
            try:
                p = Path(r.local_path)
                if p.parent.name:
                    return p.parent.name
            except Exception:
                continue
        return "Nieznany kurs"

    # --- callbacks -----------------------------------------------------

    def _on_course_chosen(self, _value: str) -> None:
        self.open_folder_btn.configure(state="disabled")

    def _selected_entry(self) -> Optional[_CourseEntry]:
        label = self.course_var.get()
        for e in self._entries:
            if e.label == label:
                return e
        return None

    def _on_stage_click(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._log("Wybierz kurs.")
            return

        self._log(f"--- {entry.course_name} ---")
        self._log(
            f"Plikow w manifescie: {len(entry.records)}; "
            f"obslugiwanych przez NotebookLM: {entry.supported_count}"
        )
        self.stage_btn.configure(state="disabled", text="Przygotowuje...")
        self.open_folder_btn.configure(state="disabled")

        app = self.app
        course_url = entry.course_url
        course_name = entry.course_name
        base_path = self.app.cfg.download_base_path

        def task(_emit):
            return stage_course(
                app.manifest,
                course_url=course_url,
                course_name=course_name,
                base_path=base_path,
            )

        self.app.bus.run(task, tag="stage")

    def _on_open_notebooklm(self) -> None:
        ok = open_notebooklm_in_browser()
        self._log(f"NotebookLM otwarty w przegladarce: {ok}")

    def _on_open_staged_folder(self) -> None:
        if not self._last_staged_dir:
            return
        path = self._last_staged_dir
        try:
            import os
            import sys

            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess

                subprocess.Popen(["open", str(path)])
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            self._log(f"Nie udalo sie otworzyc folderu: {exc}")

    # --- event handler --------------------------------------------------

    def handle_event(self, ev: Event) -> None:
        if ev.kind == "done":
            self.stage_btn.configure(state="normal", text="Przygotuj folder dla NotebookLM")
            res: StageResult = ev.payload
            self._last_staged_dir = res.dest_dir
            self.app.update_status(staged=self.app.counters["staged"] + res.staged_count)
            self._log(f"Skopiowano {res.staged_count} plikow do {res.dest_dir}")
            for p in res.staged[:50]:
                self._log(f"  + {p.name}")
            if res.staged_count > 50:
                self._log(f"  ... i {res.staged_count - 50} wiecej")
            if res.skipped:
                self._log(f"Pominieto {res.skipped_count}:")
                for rec, reason in res.skipped[:50]:
                    self._log(f"  - {Path(rec.local_path).name}: {reason}")
                if res.skipped_count > 50:
                    self._log(f"  ... i {res.skipped_count - 50} wiecej")
            self._log(f"Index: {res.dest_dir / '_index.md'}")
            self.open_folder_btn.configure(state="normal")
            self.refresh_courses()
        elif ev.kind == "error":
            self.stage_btn.configure(state="normal", text="Przygotuj folder dla NotebookLM")
            self._log(f"BLAD: {ev.payload}")

    # --- log helpers ---------------------------------------------------

    def _log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
