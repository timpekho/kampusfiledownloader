# Kampus UW Downloader

Desktop tool that downloads course materials from University of Warsaw Moodle (Kampus) after CAS login.

It crawls selected courses, skips files you already have, downloads in parallel, and can prepare a folder ready to drop into NotebookLM. Use the GUI for everyday work, or the CLI for batch / test runs.

## Wymagania

- Python 3.10+
- Konto USOS / UW

## Instalacja

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Konfiguracja

```powershell
Copy-Item .env.example .env
Copy-Item courses.txt.example courses.txt
```

W `.env` uzupełnij:

- `KAMPUS_USERNAME` — login CAS / USOS  
- `KAMPUS_PASSWORD` — hasło  
- `DOWNLOAD_BASE_PATH` — folder na pliki (domyślnie `./Materialy`)

W `courses.txt` wklej URL-e kursów (jeden na linię).

> `.env` i `courses.txt` nie trafiają na GitHub (są w `.gitignore`).

## Uruchomienie

| Tryb | Komenda | Opis |
|------|---------|------|
| GUI | `python gui.py` | Najwygodniejszy — konto, kursy, pobieranie, NotebookLM |
| Batch | `python main.py` | Pobiera wszystkie kursy z `courses.txt` |
| Test | `python test_run.py` | Interaktywnie, jeden kurs |

Przydatne flagi CLI: `--dry-run`, `--course URL`, `--refresh-session`, `--verify`.

## Struktura projektu

```
kampus/          # kod (auth, crawler, downloader, GUI)
main.py          # CLI — wszystkie kursy
gui.py           # okno aplikacji
test_run.py      # CLI — tryb testowy
.env.example     # szablon konfiguracji
courses.txt.example
requirements.txt
```
