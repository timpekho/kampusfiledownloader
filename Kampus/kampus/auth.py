from __future__ import annotations

import logging
import os
import pickle
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import BS_PARSER


log = logging.getLogger(__name__)


DEFAULT_KAMPUS_BASE = "https://kampus-kursy.ckc.uw.edu.pl/"

# Known CAS hostnames at UW (current and legacy).
CAS_HOSTS = ("login.uw.edu.pl", "logowanie.uw.edu.pl")


def _probe_url(base_url: str) -> str:
    return urljoin(base_url, "my/")


def _build_retry_adapter() -> HTTPAdapter:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"HEAD", "GET", "POST"}),
        raise_on_status=False,
    )
    return HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)


def create_session(*, user_agent: str = "KampusUWDownloader/1.0") -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    adapter = _build_retry_adapter()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _on_cas_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(h in host for h in CAS_HOSTS)


def _looks_logged_in(response: requests.Response) -> bool:
    """
    A response counts as 'logged in' when:
      - we're on the Moodle host (not on CAS),
      - status is 200,
      - and the page does not look like a Moodle login screen.
    """
    final_url = str(response.url)
    if _on_cas_host(final_url):
        return False
    if response.status_code != 200:
        return False

    text = (response.text or "")
    text_low = text.lower()

    # If the response is the Moodle anonymous login page, it's not logged in.
    if "/login/index.php" in final_url and 'name="username"' in text_low:
        return False
    if 'action="' in text_low and "/cas/login" in text_low and 'name="username"' in text_low:
        return False

    return ("logout" in text_low) or ("wyloguj" in text_low)


def _safe_chmod_600(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        # Best-effort only (Windows may ignore).
        pass


def load_cookies(session: requests.Session, cookie_cache_path: Path) -> bool:
    if not cookie_cache_path.exists():
        return False
    try:
        with open(cookie_cache_path, "rb") as f:
            jar = pickle.load(f)
        session.cookies.update(jar)
        return True
    except Exception:
        return False


def save_cookies(session: requests.Session, cookie_cache_path: Path) -> None:
    cookie_cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cookie_cache_path, "wb") as f:
        pickle.dump(session.cookies, f)
    _safe_chmod_600(cookie_cache_path)


def probe_session(
    session: requests.Session,
    *,
    base_url: str = DEFAULT_KAMPUS_BASE,
    timeout: float = 20.0,
) -> bool:
    r = session.get(_probe_url(base_url), allow_redirects=True, timeout=timeout)
    return _looks_logged_in(r)


def _extract_form_fields(form) -> Dict[str, str]:
    """
    Extracts hidden inputs + pre-checked checkboxes/radios + first <select> options.
    Skips username/password (they will be supplied separately).
    """
    data: Dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name or name in ("username", "password"):
            continue
        itype = (inp.get("type") or "").lower()
        if itype in ("hidden",):
            data[name] = inp.get("value", "")
        elif itype in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                data[name] = inp.get("value", "on")
    # Some CAS variants put _eventId on a submit button, not in hidden inputs.
    submit = form.find("button", attrs={"type": "submit", "name": True})
    if submit and submit.get("name") not in data:
        data[submit["name"]] = submit.get("value", "")
    return data


def _find_login_form(soup: BeautifulSoup) -> Tuple[str, Dict[str, str]]:
    forms = soup.find_all("form")
    for form in forms:
        if not form.get("action"):
            continue
        has_user = form.select_one('input[name="username"]') is not None
        has_pass = form.select_one('input[name="password"]') is not None
        if has_user and has_pass:
            return form.get("action"), _extract_form_fields(form)
    raise RuntimeError("Could not find CAS login form (username/password inputs missing).")


def _log_redirect_chain(prefix: str, response: requests.Response) -> None:
    if not log.isEnabledFor(logging.DEBUG):
        return
    log.debug("%s history (%d hops):", prefix, len(response.history))
    for i, h in enumerate(response.history):
        log.debug("  [%d] %s %s -> %s", i, h.status_code, h.url, h.headers.get("Location", ""))
    log.debug("%s final: %s %s", prefix, response.status_code, response.url)


@dataclass(frozen=True)
class AuthResult:
    used_cookie_cache: bool
    logged_in: bool
    final_url: str


def ensure_logged_in(
    session: requests.Session,
    *,
    username: str,
    password: str,
    cookie_cache_path: Path,
    base_url: str = DEFAULT_KAMPUS_BASE,
    timeout: float = 20.0,
    force_refresh: bool = False,
) -> AuthResult:
    """
    Ensures the session is authenticated into the given Moodle instance using UW CAS.
    """
    used_cache = False
    probe = _probe_url(base_url)

    if not force_refresh:
        used_cache = load_cookies(session, cookie_cache_path)
        if used_cache and probe_session(session, base_url=base_url, timeout=timeout):
            log.debug("Cookie cache valid, skipping CAS login.")
            return AuthResult(used_cookie_cache=True, logged_in=True, final_url=probe)

    start = urljoin(base_url, "login/index.php")
    log.debug("Starting CAS flow at %s", start)
    r = session.get(start, allow_redirects=True, timeout=timeout)
    _log_redirect_chain("login GET", r)

    if _looks_logged_in(r):
        log.debug("Already logged in after initial GET (cookies still valid?).")
        save_cookies(session, cookie_cache_path)
        return AuthResult(used_cookie_cache=used_cache, logged_in=True, final_url=str(r.url))

    if not _on_cas_host(str(r.url)):
        log.warning(
            "Expected to land on UW CAS (%s) but ended at %s",
            ", ".join(CAS_HOSTS),
            r.url,
        )

    soup = BeautifulSoup(r.text, BS_PARSER)
    action, hidden = _find_login_form(soup)
    log.debug("CAS form: action=%s hidden fields=%s", action, sorted(hidden.keys()))

    post_url = urljoin(str(r.url), action)
    payload = dict(hidden)
    payload.update({"username": username, "password": password})

    pr = session.post(post_url, data=payload, allow_redirects=True, timeout=timeout)
    _log_redirect_chain("CAS POST", pr)

    if _on_cas_host(str(pr.url)):
        # Still on CAS - bad credentials or unhandled prompt (MFA, captcha, T&C).
        soup_err = BeautifulSoup(pr.text, BS_PARSER)
        err = soup_err.select_one(".alert-danger, .errors, #status, #msg")
        if err:
            log.warning("CAS error: %s", err.get_text(" ", strip=True))
        return AuthResult(
            used_cookie_cache=used_cache, logged_in=False, final_url=str(pr.url)
        )

    final = session.get(probe, allow_redirects=True, timeout=timeout)
    _log_redirect_chain("probe GET", final)
    ok = _looks_logged_in(final)
    if ok:
        save_cookies(session, cookie_cache_path)
    return AuthResult(used_cookie_cache=used_cache, logged_in=ok, final_url=str(final.url))


def is_cas_redirect(url: str) -> bool:
    return _on_cas_host(url)

