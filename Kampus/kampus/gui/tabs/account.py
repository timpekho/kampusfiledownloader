"""Konto - login/password fields, save to .env, test login."""

from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from ..envfile import upsert_env
from ..workers import Event


if TYPE_CHECKING:
    from ..app import App


class AccountTab(ctk.CTkFrame):
    def __init__(self, master, *, app: "App") -> None:
        super().__init__(master)
        self.app = app
        self._password_visible = True

        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Logowanie do CAS UW (login.uw.edu.pl)",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=14, pady=(16, 4), sticky="w")

        ctk.CTkLabel(
            self,
            text=(
                "Login zostanie wyslany do logowanie.uw.edu.pl tylko podczas "
                "uwierzytelnienia. Zapis do .env trzyma haslo plain-text - traktuj "
                "plik .env jak sekret."
            ),
            wraplength=720,
            justify="left",
            text_color=("gray40", "gray70"),
        ).grid(row=1, column=0, columnspan=3, padx=14, pady=(0, 12), sticky="w")

        ctk.CTkLabel(self, text="Login:").grid(row=2, column=0, padx=(14, 8), pady=8, sticky="e")
        self.login_var = ctk.StringVar(value=self.app.cfg.username)
        self.login_entry = ctk.CTkEntry(self, textvariable=self.login_var, width=320)
        self.login_entry.grid(row=2, column=1, padx=(0, 8), pady=8, sticky="ew")

        ctk.CTkLabel(self, text="Haslo:").grid(row=3, column=0, padx=(14, 8), pady=8, sticky="e")
        self.password_var = ctk.StringVar(value=self.app.cfg.password)
        self.password_entry = ctk.CTkEntry(
            self,
            textvariable=self.password_var,
            show="",  # widoczne domyslnie - na zyczenie usera
            width=320,
        )
        self.password_entry.grid(row=3, column=1, padx=(0, 8), pady=8, sticky="ew")

        self.toggle_btn = ctk.CTkButton(
            self,
            text="Ukryj",
            width=72,
            command=self._toggle_password_visibility,
        )
        self.toggle_btn.grid(row=3, column=2, padx=(0, 14), pady=8, sticky="w")

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=4, column=0, columnspan=3, padx=14, pady=(16, 8), sticky="w")

        self.save_btn = ctk.CTkButton(
            actions, text="Zapisz do .env", command=self._on_save_env
        )
        self.save_btn.pack(side="left", padx=(0, 8))

        self.test_btn = ctk.CTkButton(
            actions,
            text="Testuj logowanie",
            command=self._on_test_login,
            fg_color="#1f7a3a",
            hover_color="#155a2c",
        )
        self.test_btn.pack(side="left", padx=(0, 8))

        self.refresh_btn = ctk.CTkButton(
            actions,
            text="Wyczysc cache cookies",
            command=self._on_clear_cookies,
            fg_color="#7a3a1f",
            hover_color="#5a2c15",
        )
        self.refresh_btn.pack(side="left")

        self.status_var = ctk.StringVar(value="")
        self.status_label = ctk.CTkLabel(
            self,
            textvariable=self.status_var,
            anchor="w",
            wraplength=720,
            justify="left",
        )
        self.status_label.grid(row=5, column=0, columnspan=3, padx=14, pady=(8, 14), sticky="ew")

    # --- callbacks ------------------------------------------------------

    def _toggle_password_visibility(self) -> None:
        self._password_visible = not self._password_visible
        self.password_entry.configure(show="" if self._password_visible else "*")
        self.toggle_btn.configure(text="Ukryj" if self._password_visible else "Pokaz")

    def _on_save_env(self) -> None:
        username = self.login_var.get().strip()
        password = self.password_var.get()
        if not username or not password:
            self._set_status("Login i haslo nie moga byc puste.", error=True)
            return
        env_path = self.app.project_root / ".env"
        try:
            upsert_env(
                env_path,
                {
                    "KAMPUS_USERNAME": username,
                    "KAMPUS_PASSWORD": password,
                },
            )
        except OSError as exc:
            self._set_status(f"Nie udalo sie zapisac .env: {exc}", error=True)
            return

        # Reload config so other tabs see fresh values immediately.
        from ..app import App  # noqa: F401  (purely for type hint)
        try:
            from ...config import load_config

            self.app.cfg = load_config(self.app.project_root, require_credentials=False)
        except Exception:
            pass

        self._set_status(f"Zapisano login/haslo do {env_path}.", error=False)

    def _on_clear_cookies(self) -> None:
        path = self.app.cfg.cookie_cache_path
        try:
            if path.exists():
                path.unlink()
            self.app.reset_session()
            self._set_status(f"Cache cookies wyczyszczony ({path}).", error=False)
        except OSError as exc:
            self._set_status(f"Nie udalo sie usunac cache: {exc}", error=True)

    def _on_test_login(self) -> None:
        username = self.login_var.get().strip()
        password = self.password_var.get()
        if not username or not password:
            self._set_status("Wpisz login i haslo.", error=True)
            return

        self.test_btn.configure(state="disabled", text="Loguje...")
        self._set_status("Trwa logowanie do CAS...", error=False)

        def task(emit):
            ok = self.app.login_blocking(username=username, password=password, force_refresh=False)
            return ok

        self.app.bus.run(task, tag="login")

    # --- event router ---------------------------------------------------

    def handle_event(self, ev: Event) -> None:
        if ev.kind == "done":
            ok = bool(ev.payload)
            self.test_btn.configure(state="normal", text="Testuj logowanie")
            if ok:
                self._set_status("Zalogowano. Cookies zapisane do cache.", error=False)
            else:
                self._set_status(
                    "Logowanie nie powiodlo sie. Sprawdz login/haslo (i sprobuj 'Wyczysc cache cookies').",
                    error=True,
                )
        elif ev.kind == "error":
            self.test_btn.configure(state="normal", text="Testuj logowanie")
            self._set_status(f"Blad logowania: {ev.payload}", error=True)

    def _set_status(self, text: str, *, error: bool) -> None:
        color = ("#b22222", "#ff8888") if error else ("#1f7a3a", "#7fffaa")
        self.status_var.set(text)
        self.status_label.configure(text_color=color)
