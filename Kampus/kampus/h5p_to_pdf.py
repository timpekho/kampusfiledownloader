from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import img2pdf

from .crawler import FileCandidate
from .manifest import Manifest
from .sanitize import sanitize_component


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SectionPdfResult:
    section_dir: Path
    section_name: str
    pdf_path: Optional[Path]
    slide_count: int
    source_count: int
    deleted_sources: int
    skipped: bool = False
    error: Optional[str] = None


@dataclass(frozen=True)
class ConversionReport:
    sections: List[SectionPdfResult]
    total_pdfs: int
    total_slides: int
    elapsed_s: float


def _read_main_library(zf: zipfile.ZipFile) -> Optional[str]:
    try:
        meta = json.loads(zf.read("h5p.json"))
    except Exception:
        return None
    main = meta.get("mainLibrary")
    return str(main) if main is not None else None


def _extract_rel_paths_from_course_presentation_params(params: dict) -> List[str]:
    """
    CoursePresentation stores content in `params.presentation.slides`.
    Slides may hold:
      - background image in slideBackgroundSelector.imageSlideBackground.path
      - elements with action.library like H5P.Image and file.path
    """
    out: List[str] = []
    pres = (params.get("presentation") or {}) if isinstance(params, dict) else {}
    slides = pres.get("slides") or []
    if not isinstance(slides, list):
        return out

    for slide in slides:
        if not isinstance(slide, dict):
            continue

        bg = (slide.get("slideBackgroundSelector") or {}).get("imageSlideBackground") or {}
        if isinstance(bg, dict) and bg.get("path"):
            out.append(str(bg["path"]))

        elements = slide.get("elements") or []
        if not isinstance(elements, list):
            continue
        for el in elements:
            if not isinstance(el, dict):
                continue
            action = el.get("action")
            if not isinstance(action, dict):
                continue
            lib = str(action.get("library") or "")
            params2 = action.get("params") or {}
            if not isinstance(params2, dict):
                params2 = {}

            # Most common: H5P.Image -> params.file.path
            if "H5P.Image" in lib:
                file_obj = params2.get("file") or {}
                if isinstance(file_obj, dict) and file_obj.get("path"):
                    out.append(str(file_obj["path"]))

            # Nested CoursePresentation (rare) - recurse
            if "CoursePresentation" in lib:
                out.extend(_extract_rel_paths_from_course_presentation_params(params2))

    return out


def _extract_rel_paths_from_column_content(content: dict) -> List[str]:
    """
    Column stores items in `content.content[]` with each item like:
      {"content": {"library": "...", "params": {...}}}
    We'll traverse in order and extract image-like assets.
    """
    out: List[str] = []
    blocks = content.get("content") or []
    if not isinstance(blocks, list):
        return out

    for block in blocks:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if not isinstance(inner, dict):
            continue
        lib = str(inner.get("library") or "")
        params = inner.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        if "CoursePresentation" in lib:
            out.extend(_extract_rel_paths_from_course_presentation_params(params))
            continue

        if "H5P.Image" in lib:
            file_obj = params.get("file") or {}
            if isinstance(file_obj, dict) and file_obj.get("path"):
                out.append(str(file_obj["path"]))
            continue

        # Fallback: deep-scan for file paths that look like embedded images.
        out.extend(_deep_collect_asset_paths(params))

    return out


def _deep_collect_asset_paths(obj) -> List[str]:
    """
    Best-effort recursive collection of asset paths from arbitrary H5P params.
    Keeps traversal order to approximate reading order for many content types.
    """
    out: List[str] = []

    def rec(x) -> None:
        if isinstance(x, dict):
            # common pattern: {"path": "images/foo.jpg"}
            path = x.get("path")
            if isinstance(path, str) and _looks_like_image_asset(path):
                out.append(path)
            for v in x.values():
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)

    rec(obj)
    return out


def _looks_like_image_asset(path: str) -> bool:
    low = path.lower()
    if low.startswith(("http://", "https://")):
        return False
    return any(low.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))


def extract_slide_images(h5p_path: Path, work_dir: Path) -> List[Path]:
    """
    Unzip `.h5p` and extract slide background images for CoursePresentation in order.

    Returns a list of extracted image file paths in the order they should appear in
    the PDF. Returns [] when the package is not a CoursePresentation or has no
    background images.
    """
    images: List[Path] = []
    with zipfile.ZipFile(h5p_path, "r") as zf:
        main = _read_main_library(zf) or ""

        try:
            content = json.loads(zf.read("content/content.json"))
        except Exception as exc:
            log.warning("H5P -> PDF: brak/blad content.json w %s: %s", h5p_path.name, exc)
            return []

        rel_paths: List[str] = []
        if "CoursePresentation" in main:
            rel_paths = _extract_rel_paths_from_course_presentation_params(content)
        elif "H5P.Column" in main or main.strip() == "H5P.Column":
            rel_paths = _extract_rel_paths_from_column_content(content if isinstance(content, dict) else {})
        else:
            # Last resort: scan the whole content.json for image assets.
            rel_paths = _deep_collect_asset_paths(content)
            if rel_paths:
                log.warning(
                    "H5P -> PDF: fallback extract (mainLibrary=%s): %s",
                    main,
                    h5p_path.name,
                )
            else:
                log.warning(
                    "H5P -> PDF: pomijam nieobslugiwany typ (mainLibrary=%s): %s",
                    main,
                    h5p_path.name,
                )
                return []

        # Deduplicate while preserving order
        seen: set[str] = set()
        rel_paths = [p for p in rel_paths if isinstance(p, str)]
        rel_paths = [p for p in rel_paths if not (p in seen or seen.add(p))]

        for i, rel_path in enumerate(rel_paths):
            member = f"content/{rel_path}".replace("\\", "/")
            suffix = Path(rel_path).suffix or ".jpg"
            out = work_dir / f"{h5p_path.stem}_{i:04d}{suffix}"
            try:
                with zf.open(member) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except KeyError:
                continue
            images.append(out)

    return images


def convert_section_to_pdf(
    section_dir: Path,
    section_name: str,
    h5p_paths_in_order: List[Path],
    *,
    output_filename: str,
    delete_source: bool = True,
    progress: Optional[Callable[[int, int], None]] = None,
) -> SectionPdfResult:
    """
    Convert ordered list of `.h5p` packages (CoursePresentation) to a single PDF.
    """
    if not h5p_paths_in_order:
        return SectionPdfResult(
            section_dir=section_dir,
            section_name=section_name,
            pdf_path=None,
            slide_count=0,
            source_count=0,
            deleted_sources=0,
            skipped=True,
        )

    section_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = section_dir / output_filename

    deleted = 0
    slide_total = 0
    source_count = len(h5p_paths_in_order)

    with tempfile.TemporaryDirectory(prefix="h5p_pdf_") as tmp:
        tmp_dir = Path(tmp)
        all_images: List[Path] = []

        for idx, h5p in enumerate(h5p_paths_in_order, start=1):
            if progress:
                progress(idx - 1, source_count)
            imgs = extract_slide_images(h5p, tmp_dir)
            slide_total += len(imgs)
            all_images.extend(imgs)

        if progress:
            progress(source_count, source_count)

        if not all_images:
            return SectionPdfResult(
                section_dir=section_dir,
                section_name=section_name,
                pdf_path=None,
                slide_count=0,
                source_count=source_count,
                deleted_sources=0,
                skipped=True,
                error="no_slide_images",
            )

        try:
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert([str(p) for p in all_images]))
        except Exception as exc:
            return SectionPdfResult(
                section_dir=section_dir,
                section_name=section_name,
                pdf_path=None,
                slide_count=slide_total,
                source_count=source_count,
                deleted_sources=0,
                skipped=False,
                error=str(exc),
            )

    if delete_source:
        for p in h5p_paths_in_order:
            try:
                p.unlink(missing_ok=True)
                deleted += 1
            except OSError:
                pass

    return SectionPdfResult(
        section_dir=section_dir,
        section_name=section_name,
        pdf_path=pdf_path,
        slide_count=slide_total,
        source_count=source_count,
        deleted_sources=deleted,
        skipped=False,
    )


def _group_h5p_candidates(
    candidates: Iterable[FileCandidate],
    *,
    manifest: Manifest,
) -> Dict[Tuple[str, str], List[Tuple[int, Path]]]:
    """
    Returns mapping (course_name, section_name) -> list of (order, local_path_to_h5p).
    """
    grouped: Dict[Tuple[str, str], List[Tuple[int, Path]]] = {}
    for c in candidates:
        if not c.url.lower().endswith(".h5p"):
            continue
        sec = c.section_name or ""
        if not sec:
            continue
        rec = manifest.get(c.url)
        if rec is None or not rec.local_path:
            continue
        h5p_path = Path(rec.local_path)
        if not h5p_path.exists():
            continue
        order = c.order_in_section if c.order_in_section is not None else 10_000_000
        key = (c.course_name or "", sec)
        grouped.setdefault(key, []).append((order, h5p_path))
    return grouped


def convert_all_h5p_in_courses(
    candidates: Iterable[FileCandidate],
    *,
    manifest: Manifest,
    delete_source: bool = True,
    on_section_done: Optional[Callable[[SectionPdfResult], None]] = None,
) -> ConversionReport:
    """
    Convert all downloaded H5P CoursePresentation packages into one PDF per section.

    PDF is written inside the section directory (same folder where `.h5p` lives):
      <DOWNLOAD_BASE>/<course>/<section>/<section>.pdf

    By design this uses only local files (from manifest local_path), and does not
    perform additional network requests.
    """
    t0 = time.monotonic()
    grouped = _group_h5p_candidates(candidates, manifest=manifest)

    results: List[SectionPdfResult] = []
    total_slides = 0
    total_pdfs = 0

    for (course_name, section_name), items in grouped.items():
        items_sorted = sorted(items, key=lambda t: (t[0], t[1].name))
        h5p_paths = [p for _order, p in items_sorted]
        if not h5p_paths:
            continue

        section_dir = h5p_paths[0].parent
        safe_pdf_name = sanitize_component(section_name, max_len=120) or section_name or "Wyklad"
        output_filename = f"{safe_pdf_name}.pdf"

        res = convert_section_to_pdf(
            section_dir=section_dir,
            section_name=section_name,
            h5p_paths_in_order=h5p_paths,
            output_filename=output_filename,
            delete_source=delete_source,
        )
        results.append(res)
        if res.pdf_path:
            total_pdfs += 1
        total_slides += res.slide_count
        if on_section_done:
            try:
                on_section_done(res)
            except Exception:
                log.exception("on_section_done crashed")

    return ConversionReport(
        sections=results,
        total_pdfs=total_pdfs,
        total_slides=total_slides,
        elapsed_s=time.monotonic() - t0,
    )

