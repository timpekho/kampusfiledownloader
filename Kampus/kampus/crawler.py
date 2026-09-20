from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from . import BS_PARSER


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileCandidate:
    url: str
    course_url: str
    course_name: str
    suggested_filename: Optional[str]
    head_headers: dict
    section_name: Optional[str] = None
    order_in_section: Optional[int] = None


@dataclass
class CourseFiles:
    course_url: str
    course_name: str
    candidates: List[FileCandidate] = field(default_factory=list)


_HTML_CT_RE = re.compile(r"^(?:text/html|application/xhtml\+xml)\b", re.IGNORECASE)
_FILENAME_STAR_RE = re.compile(
    r"filename\*\s*=\s*([^'\"]*)'[^']*'([^;]+)", re.IGNORECASE
)
_FILENAME_QUOTED_RE = re.compile(r'filename\s*=\s*"([^"]+)"', re.IGNORECASE)
_FILENAME_BARE_RE = re.compile(r"filename\s*=\s*([^;]+)", re.IGNORECASE)

_SECTION_ID_RE = re.compile(r"^section-(\d+)$")


def _normalize_url(url: str) -> str:
    """Strip fragment and trailing whitespace; keep query intact."""
    parsed = urlparse(url.strip())
    return urlunparse(parsed._replace(fragment=""))


def _is_html_response(headers: Mapping[str, str]) -> bool:
    ct = headers.get("Content-Type") or headers.get("content-type") or ""
    return bool(_HTML_CT_RE.search(ct))


def _is_file_response(headers: Mapping[str, str]) -> bool:
    cd = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    if cd.lower().lstrip().startswith("attachment"):
        return True
    if _is_html_response(headers):
        return False
    ct = headers.get("Content-Type") or headers.get("content-type") or ""
    return bool(ct.strip())


def parse_filename(headers: Mapping[str, str], url: str) -> Optional[str]:
    cd = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    if cd:
        m = _FILENAME_STAR_RE.search(cd)
        if m:
            encoding = (m.group(1) or "utf-8").strip() or "utf-8"
            try:
                return unquote(m.group(2).strip(), encoding=encoding)
            except LookupError:
                return unquote(m.group(2).strip())
        m = _FILENAME_QUOTED_RE.search(cd)
        if m:
            return m.group(1).strip()
        m = _FILENAME_BARE_RE.search(cd)
        if m:
            return m.group(1).strip().strip('"').strip("'")

    path = urlparse(url).path
    if path:
        last = path.rsplit("/", 1)[-1]
        if last:
            return unquote(last)
    return None


def _extract_course_name(soup: BeautifulSoup) -> Optional[str]:
    for selector in ("h1.h2", "header h1", "h1", "div.page-header-headings h1"):
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return None


def _extract_section_name(section_el) -> Optional[str]:
    """Best-effort extraction of a section's display name."""
    # 1. Common explicit attributes set by some formats.
    for attr in ("data-sectionname", "data-section-name", "aria-label"):
        v = section_el.get(attr)
        if v and v.strip():
            return v.strip()

    # 2. Headings inside the section element.
    for selector in (
        ".sectionname",
        ".section-title",
        ".tile-title",
        ".course-section-header",
        "h2",
        "h3",
        "h4",
    ):
        el = section_el.select_one(selector)
        if el:
            text = el.get_text(" ", strip=True)
            if text and text.lower() not in ("ogolne", "general"):
                return text

    return None


def _course_id_from_url(course_url: str) -> Optional[str]:
    qs = parse_qs(urlparse(course_url).query)
    ids = qs.get("id")
    return ids[0] if ids else None


def _is_same_course_view(link: str, course_id: Optional[str]) -> bool:
    """Detects /course/view.php URLs that target a specific section of `course_id`."""
    if course_id is None:
        return False
    parsed = urlparse(link)
    if "/course/view.php" not in parsed.path.lower():
        return False
    qs = parse_qs(parsed.query)
    if qs.get("id", [None])[0] != course_id:
        return False
    # Section-targeted variants (Tiles plugin uses 'section' or 'singlesec',
    # standard formats use 'section').
    return any(k in qs for k in ("section", "singlesec", "expand", "sectionid"))


def _is_section_view_url(link: str, course_id: Optional[str] = None) -> bool:
    """
    True for any URL we recognise as a Moodle section view.

    Covers both layouts seen on UW Kampus:
      - `course/view.php?id=<course>&section=<n>` (Tiles, classic single-section)
      - `course/section.php?id=<section_id>`     (Moodle 4.x section subpage)
    """
    parsed = urlparse(link)
    path = parsed.path.lower()
    if "/course/section.php" in path:
        # `id` here is the section id, not the course id; we cannot verify
        # course membership from the URL alone, so accept all.
        qs = parse_qs(parsed.query)
        return "id" in qs
    if _is_same_course_view(link, course_id):
        return True
    return False


def _link_is_followable(
    link: str,
    base_host: str,
    allowed_modules: Sequence[str],
    course_id: Optional[str],
) -> bool:
    if not link:
        return False
    low = link.lower()
    if low.startswith(("mailto:", "javascript:", "tel:", "#")):
        return False

    parsed = urlparse(link)
    if parsed.netloc and parsed.netloc.lower() != base_host:
        return False

    path = parsed.path.lower()
    if "/pluginfile.php" in path:
        return True
    for mod in allowed_modules:
        needle = "/" + mod.strip("/").lower() + "/"
        if needle in path:
            return True
    if _is_section_view_url(link, course_id):
        return True
    return False


def _extract_links(
    soup: BeautifulSoup,
    base_url: str,
    allowed_modules: Sequence[str],
    course_id: Optional[str],
) -> List[str]:
    base_host = urlparse(base_url).netloc.lower()
    out: List[str] = []
    seen: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        absolute = urljoin(base_url, href)
        absolute = _normalize_url(absolute)
        if not _link_is_followable(absolute, base_host, allowed_modules, course_id):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
    return out


def _find_section_seed_in(
    sec_el, base_url: str, course_id: Optional[str]
) -> Optional[str]:
    """
    Look for a link inside a section element that opens the section view
    (e.g. Tiles' `<a class="tile-link">`, Moodle 4.x section subpage links).
    Returns the absolute URL or None.
    """
    candidates = []
    for a in sec_el.find_all("a", href=True):
        cls = " ".join(a.get("class") or []).lower()
        href = a["href"].strip()
        absolute = _normalize_url(urljoin(base_url, href))
        if "/course/section.php" in urlparse(absolute).path.lower():
            return absolute
        if _is_same_course_view(absolute, course_id):
            return absolute
        if "tile-link" in cls or "section-link" in cls or "sectionname" in cls:
            candidates.append(absolute)
    return candidates[0] if candidates else None


def _parse_sections(
    soup: BeautifulSoup,
    base_url: str,
    allowed_modules: Sequence[str],
    course_id: Optional[str],
) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """
    Walk a course-view DOM and discover sections.

    Returns:
      - url_to_section: mapping from a child URL (module/folder/pluginfile/section view)
        to the human-readable section name it belongs to
      - section_view_seeds: list of (url, section_name) pairs we should also enqueue
        directly (e.g. Tiles "open this section" links that load content lazily,
        or Moodle 4.x `course/section.php?id=...` subpages)
    """
    url_to_section: Dict[str, str] = {}
    section_view_seeds: List[Tuple[str, str]] = []
    base_host = urlparse(base_url).netloc.lower()

    section_selectors = [
        "li.section",
        "div.section",
        "section.section",
        "li.tile",
        "div.tile",
        "[data-region='section']",
        "[data-for='section']",
        "[data-region='course-section']",
    ]

    seen_section_keys: Set[str] = set()
    seen_seed_urls: Set[str] = set()

    for selector in section_selectors:
        for sec_el in soup.select(selector):
            sec_id = sec_el.get("id") or ""
            sec_data_id = (
                sec_el.get("data-section")
                or sec_el.get("data-sectionid")
                or sec_el.get("data-section-id")
                or ""
            )
            key = sec_id or sec_data_id or id(sec_el)
            key = str(key)
            if key in seen_section_keys:
                continue
            seen_section_keys.add(key)

            name = _extract_section_name(sec_el)
            m = _SECTION_ID_RE.match(sec_id or "")
            section_num = m.group(1) if m else (sec_data_id or None)

            # Find a "go to this section" link directly on the tile if the
            # layout uses lazy-loaded subpages.
            seed_from_tile = _find_section_seed_in(sec_el, base_url, course_id)

            if not name:
                if section_num:
                    name = f"Sekcja {section_num}"
                else:
                    # Try to scrape the seed link's anchor text as a last resort.
                    if seed_from_tile:
                        for a in sec_el.find_all("a", href=True):
                            href_abs = _normalize_url(
                                urljoin(base_url, a["href"].strip())
                            )
                            if href_abs == seed_from_tile:
                                txt = a.get_text(" ", strip=True)
                                if txt:
                                    name = txt
                                    break
                if not name:
                    continue  # nothing useful

            # Map child links (modules, folders, files, section views) to this section.
            for a in sec_el.find_all("a", href=True):
                href = a["href"].strip()
                absolute = _normalize_url(urljoin(base_url, href))
                if not _link_is_followable(
                    absolute, base_host, allowed_modules, course_id
                ):
                    continue
                url_to_section.setdefault(absolute, name)

            # Prefer the explicit tile link; otherwise synthesise a fallback
            # `?section=N` seed if we know both the section number and course id.
            if seed_from_tile and seed_from_tile not in seen_seed_urls:
                section_view_seeds.append((seed_from_tile, name))
                url_to_section.setdefault(seed_from_tile, name)
                seen_seed_urls.add(seed_from_tile)
            elif section_num and course_id:
                base_view = urlunparse(urlparse(base_url)._replace(query=""))
                seed = _normalize_url(
                    f"{base_view}?id={course_id}&section={section_num}"
                )
                if seed not in seen_seed_urls:
                    section_view_seeds.append((seed, name))
                    url_to_section.setdefault(seed, name)
                    seen_seed_urls.add(seed)

    # Safety net: scan every anchor on the page. Some layouts render the
    # tile cards far away from the structural `<li class="section">` (or omit
    # them entirely) and the only signal of a section is a link to
    # `course/section.php?id=...`. Pick those up too, using the link text or
    # any heading inside as the section name.
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        absolute = _normalize_url(urljoin(base_url, href))
        if not _is_section_view_url(absolute, course_id):
            continue
        if absolute in seen_seed_urls:
            continue
        name = a.get_text(" ", strip=True)
        if not name:
            inner = a.find(["h1", "h2", "h3", "h4", "h5"])
            if inner:
                name = inner.get_text(" ", strip=True)
        if not name:
            name = a.get("aria-label") or a.get("title") or ""
        if not name:
            continue
        section_view_seeds.append((absolute, name))
        url_to_section.setdefault(absolute, name)
        seen_seed_urls.add(absolute)

    return url_to_section, section_view_seeds


def _extract_page_section_name(soup: BeautifulSoup) -> Optional[str]:
    """
    On a single-section page (e.g. `course/section.php?id=N`), pull the section
    name from the page itself so we can attach it to inherited candidates.
    Tries the most specific selectors first to avoid grabbing the course title.
    """
    selectors = (
        ".single-section .sectionname",
        "li.section .sectionname",
        ".course-section-header h2",
        ".course-section-header h3",
        "h2.sectionname",
        ".sectionname",
        "header.section-header h2",
        "header.section-header h3",
        "div.page-header-headings h2",
    )
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if text:
                return text
    return None


_H5P_INTEGRATION_RE = re.compile(
    r"H5PIntegration\s*=\s*(\{.*?\})\s*;?\s*\n", re.DOTALL
)
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _slug_for_filename(text: str, max_len: int = 80) -> str:
    """Light cleanup of a string so it survives as a filename component."""
    cleaned = _INVALID_FILENAME_CHARS.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:max_len] if cleaned else ""


def _extract_activity_title(soup: BeautifulSoup) -> Optional[str]:
    """Title of the H5P activity (used as the suggested filename)."""
    for sel in (
        "h2.h2,h2.activity-title",
        ".activity-header h2",
        "header.activity-header h2",
        "div.page-header-headings h1",
        "h1.h2",
        "h1",
        "h2",
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if text:
                return text
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return None


def _h5p_balanced_json(text: str, start: int) -> Optional[str]:
    """
    Starting at `start` (which must point at '{'), walk forward over the JSON
    object respecting strings + escapes, and return the balanced substring.
    Returns None if no balanced object is found.
    """
    depth = 0
    in_string = False
    escape = False
    i = start
    while i < len(text):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _extract_h5p_from_integration(
    soup: BeautifulSoup,
) -> List[Tuple[str, Optional[str]]]:
    """
    Parse Moodle's `var H5PIntegration = {...};` blob (server-side rendered)
    and return a list of (package_url, title) for each content item.
    """
    out: List[Tuple[str, Optional[str]]] = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if "H5PIntegration" not in text:
            continue
        marker = "H5PIntegration"
        idx = 0
        while True:
            i = text.find(marker, idx)
            if i == -1:
                break
            idx = i + len(marker)
            # Walk to the first '{' after the assignment.
            j = text.find("{", idx)
            if j == -1:
                continue
            blob = _h5p_balanced_json(text, j)
            if not blob:
                continue
            try:
                data = json.loads(blob)
            except json.JSONDecodeError:
                continue
            contents = data.get("contents") or {}
            if not isinstance(contents, dict):
                continue
            for _cid, item in contents.items():
                if not isinstance(item, dict):
                    continue
                pkg = item.get("url") or item.get("exportUrl")
                title = item.get("title") or item.get("metadata", {}).get("title")
                if pkg:
                    out.append((pkg, title))
    return out


def _extract_h5p_from_iframes(
    soup: BeautifulSoup, base_url: str
) -> List[Tuple[str, Optional[str]]]:
    """
    Each Moodle H5P iframe has src like
    `/h5p/embed.php?url=<urlencoded_pluginfile_to_.h5p>`. Pull the package URL
    out of the `url` query parameter.
    """
    out: List[Tuple[str, Optional[str]]] = []
    for iframe in soup.find_all("iframe", src=True):
        src = iframe["src"].strip()
        if not src:
            continue
        absolute = urljoin(base_url, src)
        parsed = urlparse(absolute)
        if "/h5p/embed.php" not in parsed.path.lower() and "h5p" not in parsed.path.lower():
            # Some themes pre-resolve the iframe to point straight at .h5p.
            if not absolute.lower().endswith(".h5p"):
                continue
        qs = parse_qs(parsed.query)
        pkg_raw = (qs.get("url") or qs.get("pkgurl") or [None])[0]
        if pkg_raw:
            pkg = unquote(pkg_raw)
            out.append((pkg, iframe.get("title")))
        elif absolute.lower().endswith(".h5p"):
            out.append((absolute, iframe.get("title")))
    return out


def _extract_h5p_package_urls(
    soup: BeautifulSoup, page_url: str
) -> List[Tuple[str, Optional[str]]]:
    """
    Return absolute URLs of all `.h5p` packages embedded on this page,
    paired with a best-effort suggested filename.
    """
    found: List[Tuple[str, Optional[str]]] = []
    found.extend(_extract_h5p_from_iframes(soup, page_url))
    found.extend(_extract_h5p_from_integration(soup))

    if not found:
        return []

    page_title = _extract_activity_title(soup)

    seen: Set[str] = set()
    out: List[Tuple[str, Optional[str]]] = []
    multi = sum(1 for _ in found) > 1
    for idx, (raw_url, raw_title) in enumerate(found, start=1):
        absolute = _normalize_url(urljoin(page_url, raw_url))
        if absolute in seen:
            continue
        seen.add(absolute)

        title = raw_title or page_title
        suggested: Optional[str] = None
        if title:
            slug = _slug_for_filename(title)
            if slug:
                if multi and not raw_title:
                    suggested = f"{slug} ({idx}).h5p"
                else:
                    suggested = f"{slug}.h5p"

        out.append((absolute, suggested))
    return out


_MOD_VIEW_RE = re.compile(r"/mod/[^/]+/view\.php$", re.IGNORECASE)


def _is_html_only_url(url: str) -> bool:
    """
    True for URL patterns that always return HTML pages (never a downloadable
    file). Lets the crawler skip a HEAD round-trip for these URLs, which is the
    biggest speedup for indexing-heavy courses.
    """
    path = urlparse(url).path.lower()
    if "/pluginfile.php" in path:
        return False
    if "/course/view.php" in path or "/course/section.php" in path:
        return True
    if _MOD_VIEW_RE.search(path):
        return True
    return False


def _safe_head(
    session: requests.Session, url: str, *, timeout: float
) -> Optional[requests.Response]:
    try:
        r = session.head(url, allow_redirects=True, timeout=timeout)
        if r.status_code in (405, 501):  # method not allowed
            return None
        return r
    except requests.RequestException as exc:
        log.debug("HEAD failed for %s: %s", url, exc)
        return None


@dataclass
class _UrlResult:
    """
    Output of `_process_url`: everything found by visiting one URL, with no
    shared-state side effects. The wave-driver in `crawl_course` merges these
    in the main thread, so workers don't need locks.
    """

    candidate: Optional[FileCandidate] = None
    course_name: Optional[str] = None
    url_to_section: Dict[str, str] = field(default_factory=dict)
    url_to_suggested_name: Dict[str, str] = field(default_factory=dict)
    url_to_order: Dict[str, int] = field(default_factory=dict)
    new_urls: List[Tuple[str, Optional[str]]] = field(default_factory=list)


def _process_url(
    url: str,
    parent_section: Optional[str],
    suggested_override: Optional[str],
    order_override: Optional[int],
    *,
    session: requests.Session,
    allowed_modules: Sequence[str],
    course_id: Optional[str],
    course_url: str,
    head_timeout: float,
    get_timeout: float,
) -> _UrlResult:
    """
    Worker function: fetch + parse one URL. Returns what was discovered without
    mutating any shared state. Safe to call from multiple threads.
    """
    out = _UrlResult()

    # Skip the HEAD round-trip for URLs we know always return HTML.
    head = (
        None
        if _is_html_only_url(url)
        else _safe_head(session, url, timeout=head_timeout)
    )

    if head is not None and _is_file_response(head.headers):
        out.candidate = FileCandidate(
            url=url,
            course_url=course_url,
            course_name="",
            suggested_filename=suggested_override
            or parse_filename(head.headers, url),
            head_headers=dict(head.headers),
            section_name=parent_section,
            order_in_section=order_override,
        )
        return out

    try:
        r = session.get(url, allow_redirects=True, timeout=get_timeout)
    except requests.RequestException as exc:
        log.warning("GET failed for %s: %s", url, exc)
        return out

    if not r.ok:
        log.debug("Skipping %s (status %s)", url, r.status_code)
        return out

    if not _is_html_response(r.headers):
        if _is_file_response(r.headers):
            out.candidate = FileCandidate(
                url=url,
                course_url=course_url,
                course_name="",
                suggested_filename=suggested_override
                or parse_filename(r.headers, url),
                head_headers=dict(r.headers),
                section_name=parent_section,
                order_in_section=order_override,
            )
        return out

    soup = BeautifulSoup(r.text, BS_PARSER)

    extracted = _extract_course_name(soup)
    if extracted:
        out.course_name = extracted

    is_course_or_section_view = (
        url == course_url or _is_section_view_url(url, course_id)
    )
    section_name = parent_section
    if is_course_or_section_view:
        mapping, seeds = _parse_sections(
            soup, str(r.url), allowed_modules, course_id
        )
        out.url_to_section.update(mapping)
        for seed_url, seed_name in seeds:
            out.new_urls.append((seed_url, seed_name))

    if (
        section_name is None
        and is_course_or_section_view
        and url != course_url
    ):
        page_section = _extract_page_section_name(soup)
        if page_section:
            section_name = page_section
            out.url_to_section[url] = page_section

    h5p_targets = _extract_h5p_package_urls(soup, str(r.url))
    if h5p_targets:
        log.info("H5P: %d paczek na %s", len(h5p_targets), url)
    for pkg_url, suggested in h5p_targets:
        if suggested:
            out.url_to_suggested_name[pkg_url] = suggested
        # Preserve the order of the H5P activity as seen on the section page.
        if order_override is not None:
            out.url_to_order.setdefault(pkg_url, order_override)
        out.new_urls.append((pkg_url, section_name))

    # Assign a stable, pedagogical order to URLs discovered on a section page
    # by using their DOM order. This is later propagated to the final file
    # candidates (and to derived H5P package URLs) as `order_in_section`.
    links = _extract_links(soup, str(r.url), allowed_modules, course_id)
    if is_course_or_section_view and url != course_url:
        for idx, link in enumerate(links):
            out.url_to_order.setdefault(link, idx)
    for link in links:
        out.new_urls.append((link, section_name))

    return out


def crawl_course(
    session: requests.Session,
    course_url: str,
    *,
    allowed_modules: Sequence[str],
    max_depth: int = 3,
    head_timeout: float = 20.0,
    get_timeout: float = 30.0,
    max_workers: int = 8,
) -> CourseFiles:
    """
    Wave-based parallel BFS crawl of a single Moodle course.

    Each BFS depth level (wave) is processed concurrently with up to
    `max_workers` threads; results from a wave are merged in the main thread
    before scheduling the next wave (so no locks are needed in workers).
    Tracks the section each file belongs to and the suggested filename for
    H5P packages.
    """
    t_total = time.monotonic()
    course_url = _normalize_url(course_url)
    course_id = _course_id_from_url(course_url)

    visited: Set[str] = {course_url}
    candidates: List[FileCandidate] = []
    course_name: Optional[str] = None
    url_to_section: Dict[str, str] = {}
    url_to_suggested_name: Dict[str, str] = {}
    url_to_order: Dict[str, int] = {}

    current_wave: List[Tuple[str, Optional[str], Optional[int]]] = [(course_url, None, None)]
    depth = 0

    log.info(
        "crawl_course start: %s (max_depth=%d, workers=%d)",
        course_url,
        max_depth,
        max_workers,
    )

    while current_wave and depth <= max_depth:
        t_wave = time.monotonic()
        wave_size = len(current_wave)
        workers_for_wave = max(1, min(max_workers, wave_size))

        with ThreadPoolExecutor(
            max_workers=workers_for_wave, thread_name_prefix="crawl"
        ) as pool:
            futures = [
                pool.submit(
                    _process_url,
                    url,
                    sec,
                    url_to_suggested_name.get(url),
                    url_to_order.get(url),
                    session=session,
                    allowed_modules=allowed_modules,
                    course_id=course_id,
                    course_url=course_url,
                    head_timeout=head_timeout,
                    get_timeout=get_timeout,
                )
                for url, sec, _ord in current_wave
            ]
            results = [f.result() for f in as_completed(futures)]

        # Merge in the main thread - lock-free aggregation.
        next_wave: List[Tuple[str, Optional[str], Optional[int]]] = []
        for r in results:
            if r.candidate is not None:
                candidates.append(r.candidate)
            if r.course_name and not course_name:
                course_name = r.course_name
            for k, v in r.url_to_section.items():
                url_to_section.setdefault(k, v)
            for k, v in r.url_to_suggested_name.items():
                url_to_suggested_name.setdefault(k, v)
            for k, v in r.url_to_order.items():
                url_to_order.setdefault(k, v)
            for new_url, new_section in r.new_urls:
                if new_url in visited:
                    continue
                visited.add(new_url)
                inferred = url_to_section.get(new_url) or new_section
                next_wave.append((new_url, inferred, url_to_order.get(new_url)))

        log.info(
            "BFS fala %d: %d URL-i w %.2fs (kandydatow lacznie: %d, kolejka nast: %d)",
            depth,
            wave_size,
            time.monotonic() - t_wave,
            len(candidates),
            len(next_wave),
        )

        current_wave = next_wave
        depth += 1

    final_name = course_name or f"course-{urlparse(course_url).query or 'unknown'}"

    # Backfill course_name into candidates discovered before the name was known.
    if any(not c.course_name for c in candidates):
        candidates = [
            FileCandidate(
                url=c.url,
                course_url=c.course_url,
                course_name=c.course_name or final_name,
                suggested_filename=c.suggested_filename,
                head_headers=c.head_headers,
                section_name=c.section_name,
                order_in_section=c.order_in_section,
            )
            for c in candidates
        ]

    deduped = _dedupe_candidates(candidates)
    log.info(
        "crawl_course done: '%s', %d kandydatow, %.2fs",
        final_name,
        len(deduped),
        time.monotonic() - t_total,
    )

    return CourseFiles(
        course_url=course_url,
        course_name=final_name,
        candidates=deduped,
    )


def _dedupe_candidates(items: Iterable[FileCandidate]) -> List[FileCandidate]:
    seen: Set[str] = set()
    out: List[FileCandidate] = []
    for c in items:
        if c.url in seen:
            continue
        seen.add(c.url)
        out.append(c)
    return out
