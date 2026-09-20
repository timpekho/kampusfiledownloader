"""Pobieranie - crawl, file picker, parallel download with progress."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

import customtkinter as ctk

from ...crawler import CourseFiles, FileCandidate, crawl_course
from ...downloader import DownloadResult, ResultStatus, download_all
from ...h5p_to_pdf import convert_all_h5p_in_courses, SectionPdfResult, ConversionReport
from ..workers import Event


if TYPE_CHECKING:
    from ..app import App


def _human_size(n: Optional[int]) -> str:
    if n is None or n <= 0:
        return "?"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0 or unit == "GB":
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} TB"


@dataclass
class _FileRow:
    candidate: FileCandidate
    status: str  # "nowy" | "aktualny" | "nieznany"
    var: ctk.BooleanVar
    checkbox: ctk.CTkCheckBox
    label: ctk.CTkLabel


@dataclass
class _CourseRow:
    course_files: CourseFiles
    header_var: ctk.BooleanVar
    header_checkbox: ctk.CTkCheckBox
    header_label: ctk.CTkLabel
    counter_label: ctk.CTkLabel
    files: List[_FileRow] = field(default_factory=list)


class DownloadTab(ctk.CTkFrame):
    def __init__(self, master, *, app: "App") -> None:
        super().__init__(master)
        self.app = app

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Top action bar
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=0, column=0, padx=14, pady=(14, 6), sticky="ew")
        actions.grid_columnconfigure(3, weight=1)

        self.crawl_btn = ctk.CTkButton(
            actions, text="Znajdz pliki", command=self._on_crawl_click
        )
        self.crawl_btn.grid(row=0, column=0, padx=(0, 8))

        self.download_btn = ctk.CTkButton(
            actions,
            text="Pobierz zaznaczone (0)",
            command=self._on_download_click,
            state="disabled",
            fg_color="#1f7a3a",
            hover_color="#155a2c",
        )
        self.download_btn.grid(row=0, column=1, padx=(0, 8))

        self.select_all_btn = ctk.CTkButton(
            actions, text="Zaznacz wszystkie", width=140, command=self._select_all, state="disabled"
        )
        self.select_all_btn.grid(row=0, column=2, padx=(0, 8))

        self.deselect_all_btn = ctk.CTkButton(
            actions, text="Odznacz wszystkie", width=140, command=self._deselect_all, state="disabled"
        )
        self.deselect_all_btn.grid(row=0, column=3, padx=(0, 8), sticky="w")

        # Progress + status
        self.progress = ctk.CTkProgressBar(self, mode="determinate")
        self.progress.grid(row=1, column=0, padx=14, pady=(4, 4), sticky="ew")
        self.progress.set(0.0)

        self.info_var = ctk.StringVar(
            value="Kliknij 'Znajdz pliki' aby zaindeksowac kursy z zakladki Kursy."
        )
        ctk.CTkLabel(
            self, textvariable=self.info_var, anchor="w", text_color=("gray40", "gray70")
        ).grid(row=3, column=0, padx=14, pady=(2, 8), sticky="ew")

        # Scrollable list of courses + files
        self.list_frame = ctk.CTkScrollableFrame(self, label_text="Pliki znalezione w kursach")
        self.list_frame.grid(row=2, column=0, padx=14, pady=(2, 4), sticky="nsew")
        self.list_frame.grid_columnconfigure(0, weight=1)

        # State
        self._course_rows: List[_CourseRow] = []
        self._row_index: Dict[str, _FileRow] = {}  # candidate.url -> _FileRow
        self._download_running = False
        self._download_total = 0
        self._download_done = 0
        self._download_counts = {"downloaded": 0, "skipped": 0, "failed": 0}

    # --- crawl ----------------------------------------------------------

    def _on_crawl_click(self) -> None:
        urls = self.app.tab_courses.get_urls()
        if not urls:
            self.info_var.set("Brak kursow w zakladce 'Kursy'. Dodaj URL i zapisz.")
            return

        cfg = self.app.cfg
        if not cfg.username or not cfg.password:
            self.info_var.set("Najpierw ustaw login/haslo w zakladce 'Konto'.")
            return

        self._clear_list()
        self.crawl_btn.configure(state="disabled", text="Indeksuje...")
        self.download_btn.configure(state="disabled", text="Pobierz zaznaczone (0)")
        self.select_all_btn.configure(state="disabled")
        self.deselect_all_btn.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self.info_var.set(f"Loguje i indeksuje {len(urls)} kursow...")

        app = self.app
        username = cfg.username
        password = cfg.password

        def task(emit):
            ok = app.login_blocking(username=username, password=password, force_refresh=False)
            if not ok:
                raise RuntimeError("Logowanie do CAS nie powiodlo sie.")
            emit("log", {"kind": "logged_in"})

            session = app.get_session()

            # Crawl multiple courses in parallel. Each course internally also
            # uses parallel BFS waves; cap concurrent courses to avoid
            # hammering the Moodle host with too many connections.
            t0 = time.monotonic()
            n_courses = len(urls)
            max_parallel = min(max(1, n_courses), 4)
            collected: List[Optional[CourseFiles]] = [None] * n_courses

            for url in urls:
                emit("log", {"kind": "crawling", "url": url})

            with ThreadPoolExecutor(
                max_workers=max_parallel, thread_name_prefix="course"
            ) as pool:
                future_to_idx = {
                    pool.submit(
                        crawl_course,
                        session,
                        url,
                        allowed_modules=cfg.allowed_modules,
                        max_depth=cfg.max_depth,
                    ): i
                    for i, url in enumerate(urls)
                }
                for f in as_completed(future_to_idx):
                    idx = future_to_idx[f]
                    try:
                        cf = f.result()
                    except Exception as exc:
                        emit("log", {"kind": "course_failed", "url": urls[idx], "error": str(exc)})
                        continue
                    collected[idx] = cf
                    emit("log", {"kind": "course_done", "course": cf})

            elapsed = time.monotonic() - t0
            ok_courses = [c for c in collected if c is not None]
            emit(
                "log",
                {
                    "kind": "crawl_summary",
                    "elapsed": elapsed,
                    "courses": len(ok_courses),
                    "files": sum(len(c.candidates) for c in ok_courses),
                },
            )
            return ok_courses

        self.app.bus.run(task, tag="crawl")

    # --- download -------------------------------------------------------

    def _on_download_click(self) -> None:
        selected = [
            r.candidate for r in self._row_index.values() if r.var.get()
        ]
        if not selected:
            self.info_var.set("Nic nie zaznaczono.")
            return

        self._download_running = True
        self._download_total = len(selected)
        self._download_done = 0
        self._download_counts = {"downloaded": 0, "skipped": 0, "failed": 0}

        self.crawl_btn.configure(state="disabled")
        self.download_btn.configure(state="disabled", text=f"Pobieram (0/{len(selected)})")
        self.select_all_btn.configure(state="disabled")
        self.deselect_all_btn.configure(state="disabled")
        for r in self._row_index.values():
            r.checkbox.configure(state="disabled")

        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0.0)
        self.info_var.set(f"Pobieram {len(selected)} plikow...")

        app = self.app
        cfg = self.app.cfg

        def refresh_auth() -> bool:
            return app.login_blocking(
                username=cfg.username,
                password=cfg.password,
                force_refresh=True,
            )

        def task(emit):
            session = app.get_session()

            def on_result(res: DownloadResult) -> None:
                emit("progress", res)

            results = download_all(
                selected,
                session=session,
                manifest=app.manifest,
                base_path=cfg.download_base_path,
                max_workers=cfg.max_workers,
                max_per_host=cfg.max_per_host,
                refresh_auth=refresh_auth,
                on_result=on_result,
            )

            total_h5p = sum(1 for c in selected if c.url.lower().endswith(".h5p"))
            if total_h5p:
                emit("log", {"kind": "h5p_pdf_start", "total_h5p": total_h5p})

                def on_section_done(res: SectionPdfResult) -> None:
                    emit("log", {"kind": "h5p_pdf_section", "result": res})

                report = convert_all_h5p_in_courses(
                    selected,
                    manifest=app.manifest,
                    delete_source=True,
                    on_section_done=on_section_done,
                )
                emit("log", {"kind": "h5p_pdf_done", "report": report})

            return results

        self.app.bus.run(task, tag="download")

    # --- event handler --------------------------------------------------

    def handle_event(self, ev: Event) -> None:
        if ev.tag == "crawl":
            self._handle_crawl_event(ev)
        elif ev.tag == "download":
            self._handle_download_event(ev)

    def _handle_crawl_event(self, ev: Event) -> None:
        if ev.kind == "log":
            payload = ev.payload or {}
            kind = payload.get("kind")
            if kind == "logged_in":
                self.info_var.set("Zalogowano. Indeksuje kursy rownolegle...")
            elif kind == "crawling":
                self.info_var.set(f"Crawl: {payload.get('url')}")
            elif kind == "course_done":
                cf: CourseFiles = payload["course"]
                self._add_course_to_list(cf)
                self._update_selected_counter()
            elif kind == "course_failed":
                self.info_var.set(
                    f"Blad kursu {payload.get('url')}: {payload.get('error')}"
                )
            elif kind == "crawl_summary":
                self.info_var.set(
                    f"Indeksowanie: {payload.get('files', 0)} plikow w "
                    f"{payload.get('courses', 0)} kursach w "
                    f"{payload.get('elapsed', 0):.2f}s"
                )
        elif ev.kind == "done":
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(0.0)
            self.crawl_btn.configure(state="normal", text="Znajdz pliki")
            collected: List[CourseFiles] = ev.payload or []
            total_files = sum(len(c.candidates) for c in collected)
            self.app.update_status(found=total_files)
            self.app.last_crawl = collected
            if total_files == 0:
                self.info_var.set("Crawl zakonczony - nie znaleziono zadnych plikow.")
            else:
                self.info_var.set(
                    f"Crawl zakonczony. Znaleziono {total_files} plikow w {len(collected)} kursach."
                )
            self.select_all_btn.configure(state="normal" if total_files else "disabled")
            self.deselect_all_btn.configure(state="normal" if total_files else "disabled")
            self._update_selected_counter()
            # Refresh the NotebookLM tab so it picks up new course names if any.
            if hasattr(self.app, "tab_notebooklm"):
                self.app.tab_notebooklm.refresh_courses()
        elif ev.kind == "error":
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(0.0)
            self.crawl_btn.configure(state="normal", text="Znajdz pliki")
            self.info_var.set(f"Blad crawl: {ev.payload}")

    def _handle_download_event(self, ev: Event) -> None:
        if ev.kind == "progress":
            res: DownloadResult = ev.payload
            self._download_done += 1
            if res.status == ResultStatus.DOWNLOADED:
                self._download_counts["downloaded"] += 1
            elif res.status == ResultStatus.SKIPPED:
                self._download_counts["skipped"] += 1
            else:
                self._download_counts["failed"] += 1

            row = self._row_index.get(res.candidate.url)
            if row is not None:
                self._mark_row_status(row, res)

            ratio = self._download_done / max(1, self._download_total)
            self.progress.set(ratio)
            self.download_btn.configure(text=f"Pobieram ({self._download_done}/{self._download_total})")
            self.info_var.set(
                f"Pobrano: {self._download_counts['downloaded']}  |  "
                f"Pominieto: {self._download_counts['skipped']}  |  "
                f"Bledy: {self._download_counts['failed']}"
            )
            self.app.update_status(
                downloaded=self._download_counts["downloaded"],
                skipped=self._download_counts["skipped"],
                failed=self._download_counts["failed"],
            )
        elif ev.kind == "done":
            self._download_running = False
            self.crawl_btn.configure(state="normal")
            self.select_all_btn.configure(state="normal")
            self.deselect_all_btn.configure(state="normal")
            for r in self._row_index.values():
                r.checkbox.configure(state="normal")
            self._update_selected_counter()
            self.progress.set(1.0)
            self.info_var.set(
                f"Gotowe. Pobrano {self._download_counts['downloaded']}, "
                f"pominieto {self._download_counts['skipped']}, "
                f"bledow {self._download_counts['failed']}."
            )
            if hasattr(self.app, "tab_notebooklm"):
                self.app.tab_notebooklm.refresh_courses()
        elif ev.kind == "error":
            self._download_running = False
            self.crawl_btn.configure(state="normal")
            self.select_all_btn.configure(state="normal")
            self.deselect_all_btn.configure(state="normal")
            for r in self._row_index.values():
                r.checkbox.configure(state="normal")
            self._update_selected_counter()
            self.info_var.set(f"Blad pobierania: {ev.payload}")

        elif ev.kind == "log":
            payload = ev.payload or {}
            kind = payload.get("kind")
            if kind == "h5p_pdf_start":
                self.info_var.set(
                    f"Konwertuje H5P -> PDF... ({payload.get('total_h5p', 0)} zrodel)"
                )
            elif kind == "h5p_pdf_section":
                res: SectionPdfResult = payload.get("result")
                if res and res.pdf_path:
                    self.info_var.set(
                        f"PDF gotowy: {res.pdf_path.name} ({res.slide_count} slajdow)"
                    )
                elif res:
                    self.info_var.set(
                        f"PDF pominiety: {res.section_name} ({res.error or 'brak slajdow'})"
                    )
            elif kind == "h5p_pdf_done":
                rep: ConversionReport = payload.get("report")
                if rep:
                    self.info_var.set(
                        f"Konwersja zakonczona: {rep.total_pdfs} PDF, "
                        f"{rep.total_slides} slajdow w {rep.elapsed_s:.1f}s"
                    )

    # --- list rendering -------------------------------------------------

    def _clear_list(self) -> None:
        for child in self.list_frame.winfo_children():
            child.destroy()
        self._course_rows.clear()
        self._row_index.clear()

    def _add_course_to_list(self, cf: CourseFiles) -> None:
        row_idx = len(self.list_frame.winfo_children())

        header_frame = ctk.CTkFrame(self.list_frame, fg_color=("gray85", "gray20"))
        header_frame.grid(row=row_idx, column=0, sticky="ew", padx=4, pady=(8, 2))
        header_frame.grid_columnconfigure(1, weight=1)

        header_var = ctk.BooleanVar(value=False)
        header_chk = ctk.CTkCheckBox(
            header_frame,
            text="",
            variable=header_var,
            width=24,
            command=lambda cf=cf: self._toggle_course(cf),
        )
        header_chk.grid(row=0, column=0, padx=(8, 4), pady=4)

        header_label = ctk.CTkLabel(
            header_frame,
            text=cf.course_name or cf.course_url,
            anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        header_label.grid(row=0, column=1, padx=4, pady=4, sticky="w")

        # Per-section file count summary in the course header.
        section_counts: Dict[str, int] = {}
        for c in cf.candidates:
            key = c.section_name or "(bez sekcji)"
            section_counts[key] = section_counts.get(key, 0) + 1
        section_summary = (
            f"{len(cf.candidates)} plikow w {len(section_counts)} sekcjach"
            if section_counts
            else f"{len(cf.candidates)} plikow"
        )
        counter_label = ctk.CTkLabel(
            header_frame,
            text=f"({section_summary})",
            anchor="e",
            text_color=("gray40", "gray70"),
        )
        counter_label.grid(row=0, column=2, padx=(4, 8), pady=4, sticky="e")

        course_row = _CourseRow(
            course_files=cf,
            header_var=header_var,
            header_checkbox=header_chk,
            header_label=header_label,
            counter_label=counter_label,
        )
        self._course_rows.append(course_row)

        # Group files by section, preserving the first-seen order.
        groups: List[tuple[str, List[FileCandidate]]] = []
        index: Dict[str, int] = {}
        for cand in cf.candidates:
            key = cand.section_name or "(bez sekcji)"
            if key not in index:
                index[key] = len(groups)
                groups.append((key, []))
            groups[index[key]][1].append(cand)

        any_new = False
        for section_name, members in groups:
            self._add_section_header(section_name, len(members))
            for cand in members:
                should = self.app.manifest.should_download(cand.url, cand.head_headers)
                status = "nowy" if should else "aktualny"
                self._add_file_row(course_row, cand, status=status, default_checked=should)
                if should:
                    any_new = True

        header_var.set(any_new)
        self._sync_header_checkbox(course_row)

    def _add_section_header(self, section_name: str, count: int) -> None:
        row_idx = len(self.list_frame.winfo_children())
        frame = ctk.CTkFrame(self.list_frame, fg_color="transparent")
        frame.grid(row=row_idx, column=0, sticky="ew", padx=20, pady=(4, 0))
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame,
            text=f"   {section_name}  ({count})",
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("gray30", "gray80"),
        ).grid(row=0, column=0, padx=(0, 4), pady=2, sticky="w")

    def _add_file_row(
        self,
        course_row: _CourseRow,
        candidate: FileCandidate,
        *,
        status: str,
        default_checked: bool,
    ) -> None:
        row_idx = len(self.list_frame.winfo_children())
        row_frame = ctk.CTkFrame(self.list_frame, fg_color="transparent")
        row_frame.grid(row=row_idx, column=0, sticky="ew", padx=44, pady=0)
        row_frame.grid_columnconfigure(1, weight=1)

        var = ctk.BooleanVar(value=default_checked)
        chk = ctk.CTkCheckBox(
            row_frame, text="", variable=var, width=24, command=self._on_any_file_toggle
        )
        chk.grid(row=0, column=0, padx=(0, 4), pady=2)

        size_str = _human_size(self._size_from_headers(candidate))
        text = f"{candidate.suggested_filename or candidate.url}  ({size_str})  [{status}]"
        label = ctk.CTkLabel(row_frame, text=text, anchor="w")
        label.grid(row=0, column=1, padx=4, pady=2, sticky="ew")

        file_row = _FileRow(
            candidate=candidate, status=status, var=var, checkbox=chk, label=label
        )
        course_row.files.append(file_row)
        self._row_index[candidate.url] = file_row

    def _size_from_headers(self, candidate: FileCandidate) -> Optional[int]:
        h = candidate.head_headers or {}
        raw = h.get("Content-Length") or h.get("content-length")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    # --- selection helpers ---------------------------------------------

    def _toggle_course(self, cf: CourseFiles) -> None:
        course_row = next((c for c in self._course_rows if c.course_files is cf), None)
        if course_row is None:
            return
        target = course_row.header_var.get()
        for f in course_row.files:
            f.var.set(target)
        self._update_selected_counter()

    def _on_any_file_toggle(self) -> None:
        for course_row in self._course_rows:
            self._sync_header_checkbox(course_row)
        self._update_selected_counter()

    def _sync_header_checkbox(self, course_row: _CourseRow) -> None:
        any_checked = any(f.var.get() for f in course_row.files)
        course_row.header_var.set(any_checked)

    def _select_all(self) -> None:
        for r in self._row_index.values():
            r.var.set(True)
        for c in self._course_rows:
            c.header_var.set(bool(c.files))
        self._update_selected_counter()

    def _deselect_all(self) -> None:
        for r in self._row_index.values():
            r.var.set(False)
        for c in self._course_rows:
            c.header_var.set(False)
        self._update_selected_counter()

    def _update_selected_counter(self) -> None:
        count = sum(1 for r in self._row_index.values() if r.var.get())
        self.app.update_status(selected=count)
        if self._download_running:
            return
        if count == 0:
            self.download_btn.configure(state="disabled", text="Pobierz zaznaczone (0)")
        else:
            self.download_btn.configure(state="normal", text=f"Pobierz zaznaczone ({count})")

    def _mark_row_status(self, row: _FileRow, res: DownloadResult) -> None:
        if res.status == ResultStatus.DOWNLOADED:
            new_status = "OK"
            color = ("#1f7a3a", "#7fffaa")
        elif res.status == ResultStatus.SKIPPED:
            new_status = "pominiety"
            color = ("gray50", "gray60")
        else:
            new_status = f"BLAD: {res.error or 'unknown'}"
            color = ("#b22222", "#ff8888")

        row.status = new_status
        size_str = _human_size(self._size_from_headers(row.candidate))
        text = f"{row.candidate.suggested_filename or row.candidate.url}  ({size_str})  [{new_status}]"
        row.label.configure(text=text, text_color=color)
