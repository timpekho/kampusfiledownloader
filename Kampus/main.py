from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import List, Optional

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from kampus.auth import create_session, ensure_logged_in
from kampus.config import Config, load_config
from kampus.crawler import CourseFiles, FileCandidate, crawl_course
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


def _load_courses(courses_file: Path) -> List[str]:
    if not courses_file.exists():
        return []
    out: List[str] = []
    for line in courses_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="kampus-uw-downloader",
        description="Download files from a UW Moodle (kampus) course.",
    )
    p.add_argument("--course", action="append", default=[], help="Course URL (repeatable). Overrides courses.txt.")
    p.add_argument("--dry-run", action="store_true", help="Crawl and decide, but do not download.")
    p.add_argument("--refresh-session", action="store_true", help="Force a fresh CAS login (ignore cookie cache).")
    p.add_argument("--verify", action="store_true", help="Verify SHA-256 of locally stored files against manifest.")
    return p.parse_args()


def _print_summary(per_course: dict[str, Counter], totals: Counter) -> None:
    table = Table(title="Podsumowanie pobierania", show_lines=False)
    table.add_column("Kurs", overflow="fold")
    table.add_column("Pobrano", justify="right")
    table.add_column("Pominieto", justify="right")
    table.add_column("Bledy", justify="right")
    for course, counter in per_course.items():
        table.add_row(
            course,
            str(counter.get(ResultStatus.DOWNLOADED, 0)),
            str(counter.get(ResultStatus.SKIPPED, 0)),
            str(counter.get(ResultStatus.FAILED, 0)),
        )
    table.add_section()
    table.add_row(
        "[bold]RAZEM[/bold]",
        f"[bold]{totals.get(ResultStatus.DOWNLOADED, 0)}[/bold]",
        f"[bold]{totals.get(ResultStatus.SKIPPED, 0)}[/bold]",
        f"[bold]{totals.get(ResultStatus.FAILED, 0)}[/bold]",
    )
    console.print(table)


def _verify_command(manifest: Manifest) -> int:
    problems = manifest.verify()
    if not problems:
        console.print("[green]Wszystkie pliki w manifescie pasuja do dysku.[/green]")
        return 0
    table = Table(title="Niezgodnosci manifestu")
    table.add_column("Powod")
    table.add_column("Lokalna sciezka", overflow="fold")
    table.add_column("URL", overflow="fold")
    for rec, reason in problems:
        table.add_row(reason, rec.local_path, rec.url)
    console.print(table)
    return 1


def _crawl_courses(
    session,
    course_urls: List[str],
    *,
    cfg: Config,
) -> List[CourseFiles]:
    crawled: List[CourseFiles] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Crawl[/bold blue] {task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Kursy", total=len(course_urls))
        for url in course_urls:
            progress.update(task, description=url)
            try:
                cf = crawl_course(
                    session,
                    url,
                    allowed_modules=cfg.allowed_modules,
                    max_depth=cfg.max_depth,
                )
                crawled.append(cf)
                console.log(
                    f"[cyan]{cf.course_name}[/cyan]: znaleziono {len(cf.candidates)} plikow"
                )
            except Exception as exc:
                logging.exception("Crawl failed for %s", url)
                console.log(f"[red]Crawl error[/red] {url}: {exc}")
            progress.advance(task)
    return crawled


def _download_courses(
    crawled: List[CourseFiles],
    *,
    session,
    manifest: Manifest,
    cfg: Config,
    refresh_auth,
) -> tuple[dict[str, Counter], Counter]:
    per_course: dict[str, Counter] = {}
    totals: Counter = Counter()

    total_files = sum(len(cf.candidates) for cf in crawled)
    if total_files == 0:
        console.print("[yellow]Brak plikow do pobrania.[/yellow]")
        return per_course, totals

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold green]Pobieranie[/bold green]"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("[D:{task.fields[d]} S:{task.fields[s]} F:{task.fields[f]}]"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("all", total=total_files, d=0, s=0, f=0)

        d = s = f = 0

        def on_result(res: DownloadResult) -> None:
            nonlocal d, s, f
            if res.status == ResultStatus.DOWNLOADED:
                d += 1
            elif res.status == ResultStatus.SKIPPED:
                s += 1
            else:
                f += 1
                console.log(f"[red]FAIL[/red] {res.candidate.url}: {res.error}")
            progress.update(task, advance=1, d=d, s=s, f=f)

        for cf in crawled:
            counter: Counter = Counter()
            results = download_all(
                cf.candidates,
                session=session,
                manifest=manifest,
                base_path=cfg.download_base_path,
                max_workers=cfg.max_workers,
                max_per_host=cfg.max_per_host,
                refresh_auth=refresh_auth,
                on_result=on_result,
            )
            for r in results:
                counter[r.status] += 1
                totals[r.status] += 1
            per_course[cf.course_name] = counter

    return per_course, totals


def main() -> int:
    args = _parse_args()
    try:
        cfg = load_config(PROJECT_ROOT)
    except ValueError as exc:
        console.print(f"[red]Konfiguracja:[/red] {exc}")
        console.print("Skopiuj `.env.example` do `.env` i uzupelnij dane.")
        return 2

    _setup_logging(cfg.log_level)
    log = logging.getLogger("kampus")

    cfg.download_base_path.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(cfg.manifest_path)

    try:
        if args.verify:
            return _verify_command(manifest)

        course_urls = args.course or _load_courses(PROJECT_ROOT / "courses.txt")
        if not course_urls:
            console.print("[red]Brak kursow.[/red] Dodaj URL do `courses.txt` lub uzyj `--course URL`.")
            return 2

        session = create_session()

        log.info("Logowanie...")
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
                "[red]Logowanie nie powiodlo sie.[/red] Sprawdz dane w .env "
                "i sprobuj `--refresh-session`."
            )
            return 3
        log.info(
            "Zalogowano (cache=%s) -> %s",
            "yes" if auth.used_cookie_cache else "no",
            auth.final_url,
        )

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

        crawled = _crawl_courses(session, course_urls, cfg=cfg)

        if args.dry_run:
            for cf in crawled:
                console.print(f"\n[bold]{cf.course_name}[/bold]  ({len(cf.candidates)} plikow)")
                for c in cf.candidates:
                    decision = "POBIERZ" if manifest.should_download(c.url, c.head_headers) else "pomin"
                    console.print(f"  [{decision}] {c.suggested_filename or c.url}")
            return 0

        per_course, totals = _download_courses(
            crawled,
            session=session,
            manifest=manifest,
            cfg=cfg,
            refresh_auth=refresh_auth,
        )
        _print_summary(per_course, totals)
        return 0 if totals.get(ResultStatus.FAILED, 0) == 0 else 1
    finally:
        manifest.close()


if __name__ == "__main__":
    sys.exit(main())
