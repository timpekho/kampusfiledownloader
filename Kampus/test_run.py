"""
Interactive test runner for Kampus UW Downloader.

Pyta o login i haslo (haslo nie jest echo'owane), pozwala wybrac
JEDEN kurs (z courses.txt lub recznie wpisany URL) i pobiera tylko go.

Uruchomienie:
    python test_run.py
    python test_run.py --dry-run            # tylko crawl, bez pobierania
    python test_run.py --refresh-session    # wymus nowy login CAS

Hasla NIE sa zapisywane w .env ani na dysku.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import List, Optional

from rich.console import Console
from rich.logging import RichHandler
from rich.prompt import Prompt
from rich.table import Table

from kampus.auth import create_session, ensure_logged_in
from kampus.config import load_config
from kampus.crawler import crawl_course
from kampus.downloader import DownloadResult, ResultStatus, download_all
from kampus.manifest import Manifest


PROJECT_ROOT = Path(__file__).resolve().parent
console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def _load_courses_from_file(path: Path) -> List[str]:
    if not path.exists():
        return []
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _pick_course(course_urls: List[str]) -> Optional[str]:
    if course_urls:
        console.print("\n[bold]Dostepne kursy z courses.txt:[/bold]")
        for i, url in enumerate(course_urls, start=1):
            console.print(f"  [cyan]{i}[/cyan]) {url}")
        console.print("  [cyan]m[/cyan]) wpisz wlasny URL recznie")
        console.print("  [cyan]q[/cyan]) wyjdz")
        choice = Prompt.ask(
            "Wybierz numer kursu",
            default="1",
        ).strip().lower()
    else:
        console.print("[yellow]courses.txt jest pusty.[/yellow]")
        choice = "m"

    if choice in ("q", "quit", "exit"):
        return None
    if choice == "m":
        url = Prompt.ask("Podaj URL kursu").strip()
        return url or None
    try:
        idx = int(choice)
        if 1 <= idx <= len(course_urls):
            return course_urls[idx - 1]
    except ValueError:
        pass
    console.print("[red]Nieprawidlowy wybor.[/red]")
    return None


def _ask_credentials(default_username: str) -> tuple[str, str]:
    console.print()
    console.rule("[bold]Logowanie CAS UW (login.uw.edu.pl)[/bold]")
    if default_username:
        username = Prompt.ask("Login", default=default_username).strip()
    else:
        username = Prompt.ask("Login").strip()

    while not username:
        console.print("[red]Login nie moze byc pusty.[/red]")
        username = Prompt.ask("Login").strip()

    # Visible password - widoczne po wpisaniu/wklejeniu (na zyczenie).
    # UWAGA: haslo zostanie wyswietlone na ekranie i moze trafic do scrollback terminala.
    password = Prompt.ask("Haslo (widoczne)").strip()
    while not password:
        console.print("[red]Haslo nie moze byc puste.[/red]")
        password = Prompt.ask("Haslo (widoczne)").strip()
    return username, password


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Interactive test runner (single course).")
    p.add_argument("--dry-run", action="store_true", help="Only crawl, do not download.")
    p.add_argument("--refresh-session", action="store_true", help="Force fresh CAS login.")
    p.add_argument("--debug", action="store_true", help="Verbose logging (CAS redirect chain).")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    try:
        cfg = load_config(PROJECT_ROOT, require_credentials=False)
    except ValueError as exc:
        console.print(f"[red]Konfiguracja:[/red] {exc}")
        console.print("Skopiuj `.env.example` do `.env` i ustaw przynajmniej DOWNLOAD_BASE_PATH.")
        return 2

    _setup_logging("DEBUG" if args.debug else cfg.log_level)
    log = logging.getLogger("kampus.test")

    courses = _load_courses_from_file(PROJECT_ROOT / "courses.txt")
    course_url = _pick_course(courses)
    if not course_url:
        console.print("[yellow]Anulowano.[/yellow]")
        return 0

    username, password = _ask_credentials(cfg.username)
    cfg = cfg.with_credentials(username, password)

    cfg.download_base_path.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(cfg.manifest_path)

    try:
        session = create_session()

        console.rule("[bold]Logowanie[/bold]")
        auth = ensure_logged_in(
            session,
            username=cfg.username,
            password=cfg.password,
            cookie_cache_path=cfg.cookie_cache_path,
            base_url=cfg.kampus_base_url,
            force_refresh=args.refresh_session,
        )
        if not auth.logged_in:
            console.print(
                "[red]Logowanie nie powiodlo sie.[/red] "
                f"Konczy sie na: [yellow]{auth.final_url}[/yellow]"
            )
            console.print(
                "Wskazowki: sprawdz login/haslo, czy nie masz aktywnego MFA, "
                "i sprobuj `python test_run.py --refresh-session --debug` "
                "zeby zobaczyc szczegoly chain'a redirectow."
            )
            return 3
        console.print(
            f"[green]Zalogowano[/green] (cache cookies: "
            f"{'tak' if auth.used_cookie_cache else 'nie'}) -> {auth.final_url}"
        )

        # Po sukcesie zerujemy haslo w pamieci - na ile to mozliwe w Pythonie.
        password = ""
        del password

        console.rule(f"[bold]Crawl kursu[/bold]")
        console.print(f"URL: [cyan]{course_url}[/cyan]")
        course_files = crawl_course(
            session,
            course_url,
            allowed_modules=cfg.allowed_modules,
            max_depth=cfg.max_depth,
        )
        console.print(
            f"Kurs: [bold]{course_files.course_name}[/bold] - "
            f"znaleziono [bold]{len(course_files.candidates)}[/bold] plikow"
        )

        if not course_files.candidates:
            console.print("[yellow]Brak plikow do pobrania w tym kursie.[/yellow]")
            return 0

        if args.dry_run:
            table = Table(title=f"Dry-run: {course_files.course_name}")
            table.add_column("Decyzja")
            table.add_column("Nazwa", overflow="fold")
            table.add_column("URL", overflow="fold")
            for c in course_files.candidates:
                decision = (
                    "[green]POBIERZ[/green]"
                    if manifest.should_download(c.url, c.head_headers)
                    else "[dim]pomin[/dim]"
                )
                table.add_row(decision, c.suggested_filename or "-", c.url)
            console.print(table)
            return 0

        def refresh_auth() -> bool:
            log.warning("Sesja wygasla, ponawiam logowanie CAS...")
            res = ensure_logged_in(
                session,
                username=cfg.username,
                password=cfg.password,
                cookie_cache_path=cfg.cookie_cache_path,
                base_url=cfg.kampus_base_url,
                force_refresh=True,
            )
            return res.logged_in

        counter: Counter = Counter()

        def on_result(res: DownloadResult) -> None:
            counter[res.status] += 1
            if res.status == ResultStatus.DOWNLOADED:
                console.print(f"[green]OK[/green] {res.local_path}")
            elif res.status == ResultStatus.SKIPPED:
                console.print(f"[dim]skip[/dim] {res.candidate.url}")
            else:
                console.print(f"[red]FAIL[/red] {res.candidate.url} - {res.error}")

        console.rule("[bold]Pobieranie[/bold]")
        download_all(
            course_files.candidates,
            session=session,
            manifest=manifest,
            base_path=cfg.download_base_path,
            max_workers=cfg.max_workers,
            max_per_host=cfg.max_per_host,
            refresh_auth=refresh_auth,
            on_result=on_result,
        )

        console.rule("[bold]Podsumowanie[/bold]")
        console.print(
            f"Pobrano: [green]{counter.get(ResultStatus.DOWNLOADED, 0)}[/green] | "
            f"Pominieto: [yellow]{counter.get(ResultStatus.SKIPPED, 0)}[/yellow] | "
            f"Bledy: [red]{counter.get(ResultStatus.FAILED, 0)}[/red]"
        )
        return 0 if counter.get(ResultStatus.FAILED, 0) == 0 else 1
    finally:
        manifest.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Przerwano przez uzytkownika.[/yellow]")
        sys.exit(130)
