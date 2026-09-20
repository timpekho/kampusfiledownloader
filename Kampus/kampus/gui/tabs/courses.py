"""Kursy - editable list of course URLs synced with courses.txt."""

from __future__ import annotations

from typing import TYPE_CHECKING, List

import customtkinter as ctk


if TYPE_CHECKING:
    from ..app import App


class CoursesTab(ctk.CTkFrame):
    def __init__(self, master, *, app: "App") -> None:
        super().__init__(master)
        self.app = app

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text="Lista kursow (1 URL na linie)",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, padx=14, pady=(16, 4), sticky="w")

        ctk.CTkLabel(
            self,
            text=(
                "Wklej linki do stron kursow (np. https://kampus-kursy.ckc.uw.edu.pl/"
                "course/view.php?id=12345). Linie zaczynajace sie od '#' sa ignorowane."
            ),
            wraplength=900,
            justify="left",
            text_color=("gray40", "gray70"),
        ).grid(row=1, column=0, padx=14, pady=(0, 8), sticky="w")

        self.textbox = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=13))
        self.textbox.grid(row=2, column=0, padx=14, pady=(0, 8), sticky="nsew")

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, padx=14, pady=(0, 8), sticky="ew")

        self.save_btn = ctk.CTkButton(actions, text="Zapisz do courses.txt", command=self._on_save)
        self.save_btn.pack(side="left", padx=(0, 8))

        self.reload_btn = ctk.CTkButton(actions, text="Wczytaj z dysku", command=self._reload_from_disk)
        self.reload_btn.pack(side="left", padx=(0, 8))

        self.status_var = ctk.StringVar(value="")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", text_color=("gray40", "gray70")).grid(
            row=4, column=0, padx=14, pady=(0, 14), sticky="ew"
        )

        self._reload_from_disk()

    # --- helpers --------------------------------------------------------

    @property
    def _courses_file(self):
        return self.app.project_root / "courses.txt"

    def get_urls(self) -> List[str]:
        """Returns the current URLs from the textbox (filtering blanks/comments)."""
        text = self.textbox.get("1.0", "end")
        out: List[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
        return out

    def _reload_from_disk(self) -> None:
        path = self._courses_file
        try:
            content = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError as exc:
            self.status_var.set(f"Nie mozna otworzyc {path}: {exc}")
            return
        self.textbox.delete("1.0", "end")
        if not content:
            content = "# https://kampus-kursy.ckc.uw.edu.pl/course/view.php?id=12345\n"
        self.textbox.insert("1.0", content)
        self.status_var.set(f"Wczytano: {path}")

    def _on_save(self) -> None:
        path = self._courses_file
        text = self.textbox.get("1.0", "end")
        # Validate non-comment lines as URLs
        bad = []
        for i, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if not (line.startswith("http://") or line.startswith("https://")):
                bad.append((i, line))

        if bad:
            preview = "; ".join(f"linia {i}: {l[:50]}" for i, l in bad[:3])
            self.status_var.set(f"Bledne URL-e ({len(bad)}). {preview}")
            return

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not text.endswith("\n"):
                text += "\n"
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.status_var.set(f"Blad zapisu: {exc}")
            return

        urls = self.get_urls()
        self.status_var.set(f"Zapisano {len(urls)} kursow do {path}")
