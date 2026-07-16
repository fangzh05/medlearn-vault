"""Local-only, page-preserving native PDF text extraction. No OCR is used."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import statistics
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXTRACTION_VERSION = "1"


class PdfExtractionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExtractionResult:
    source_relative_path: str
    report: dict[str, Any]


def _backend() -> Any:
    try:
        import fitz  # type: ignore[import-untyped]
    except ImportError as exc:
        raise PdfExtractionError("PDF_EXTRACTION_BACKEND_UNAVAILABLE") from exc
    return fitz


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve(path: Path, code: str = "PDF_OUTPUT_PATH_UNSAFE") -> Path:
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise PdfExtractionError(code) from exc


def _planned_resolve(path: Path) -> Path:
    """Resolve a not-yet-created path through its nearest existing parent."""
    tail: list[str] = []
    probe = path
    while not probe.exists():
        tail.append(probe.name)
        probe = probe.parent
    return _resolve(probe).joinpath(*reversed(tail))


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def discover_pdfs(input_path: Path) -> list[Path]:
    if not input_path.exists():
        raise PdfExtractionError("PDF_NOT_FOUND")
    if input_path.is_file():
        if input_path.suffix.lower() != ".pdf":
            raise PdfExtractionError("PDF_NOT_FOUND")
        return [_resolve(input_path, "PDF_NOT_FOUND")]
    if not input_path.is_dir():
        raise PdfExtractionError("PDF_NOT_FOUND")
    root = _resolve(input_path)
    found: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories if not _is_link_or_junction(Path(current) / name)
        ]
        for name in files:
            candidate = Path(current) / name
            if name.lower().endswith(".pdf") and not _is_link_or_junction(candidate):
                resolved = candidate.resolve()
                if _inside(resolved, root):
                    found.append(resolved)
    return sorted(found, key=lambda item: item.as_posix().casefold())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _normalise(text: str) -> str:
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _atomic_write(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".medlearn-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise PdfExtractionError("PDF_OUTPUT_WRITE_FAILED") from exc


def _cleanup_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _output_dir(relative: Path, output_root: Path) -> Path:
    if relative.is_absolute() or "\x00" in relative.as_posix() or ".." in relative.parts:
        raise PdfExtractionError("PDF_OUTPUT_PATH_UNSAFE")
    result = output_root / relative.parent / relative.stem
    if not _inside(result.resolve(strict=False), output_root):
        raise PdfExtractionError("PDF_OUTPUT_PATH_UNSAFE")
    return result


def extract_pdf(
    pdf: Path, source_root: Path, output_root: Path, force: bool = False
) -> ExtractionResult:
    fitz = _backend()
    pdf = _resolve(pdf, "PDF_NOT_FOUND")
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf" or not _inside(pdf, source_root):
        raise PdfExtractionError("PDF_NOT_FOUND")
    relative = Path(pdf.relative_to(source_root).as_posix())
    destination = _output_dir(relative, output_root)
    source_sha256, source_size = _sha256(pdf), pdf.stat().st_size
    try:
        document = fitz.open(pdf)
    except Exception as exc:
        raise PdfExtractionError("PDF_UNREADABLE") from exc
    try:
        if document.needs_pass:
            raise PdfExtractionError("PDF_ENCRYPTED")
        if document.page_count <= 0:
            raise PdfExtractionError("PDF_ZERO_PAGES")
        pages: list[dict[str, object]] = []
        inspection: list[str] = []
        counts: list[int] = []
        empty = low = replacements = 0
        for number, page in enumerate(document, 1):
            text = _normalise(page.get_text("text", sort=False))
            if not text.strip():
                text = ""
            char_count, nonspace = len(text), len(re.sub(r"\s+", "", text))
            if text:
                counts.append(char_count)
                low += nonspace < 80
            else:
                empty += 1
            replacements += text.count("\ufffd")
            pages.append(
                {
                    "char_count": char_count,
                    "extraction_version": EXTRACTION_VERSION,
                    "pdf_page_number": number,
                    "source_file": pdf.name,
                    "source_relative_path": relative.as_posix(),
                    "source_sha256": source_sha256,
                    "text": text,
                    "text_status": "text" if text else "empty",
                }
            )
            inspection.append(f"===== PDF PAGE {number} =====\n\n{text}")
    except PdfExtractionError:
        raise
    except Exception as exc:
        raise PdfExtractionError("PDF_EXTRACTION_FAILED") from exc
    finally:
        document.close()
    total = sum(counts)
    meaningful = sum(len(re.sub(r"\s+", "", str(page["text"]))) >= 20 for page in pages)
    warnings: list[str] = []
    if empty:
        warnings.append("EMPTY_PAGES_PRESENT")
    if low:
        warnings.append("LOW_TEXT_PAGES_PRESENT")
    if meaningful / len(pages) <= 0.2:
        warnings.append("POSSIBLE_IMAGE_ONLY_PDF")
    if replacements:
        warnings.append("REPLACEMENT_CHARACTERS_PRESENT")
    if replacements / max(total, 1) >= 0.02:
        warnings.append("POSSIBLE_BROKEN_TEXT_LAYER")
    pages_bytes = b"".join(_json_bytes(page) for page in pages)
    fulltext_bytes = ("\n\n".join(inspection).rstrip("\n") + "\n").encode()

    def digest(value: bytes) -> str:
        return "sha256:" + hashlib.sha256(value).hexdigest()

    report: dict[str, Any] = {
        "backend_name": "PyMuPDF",
        "backend_version": str(fitz.VersionBind),
        "empty_pages": empty,
        "extraction_status": "success_with_warnings" if warnings else "success",
        "extraction_version": EXTRACTION_VERSION,
        "low_text_pages": low,
        "median_characters_per_nonempty_page": statistics.median(counts) if counts else 0,
        "output_digests": {
            "fulltext.txt": digest(fulltext_bytes),
            "pages.jsonl": digest(pages_bytes),
        },
        "output_files": ["pages.jsonl", "fulltext.txt", "report.json"],
        "pages_with_text": len(pages) - empty,
        "replacement_character_count": replacements,
        "source_byte_length": source_size,
        "source_file": pdf.name,
        "source_relative_path": relative.as_posix(),
        "source_sha256": source_sha256,
        "total_characters": total,
        "total_pages": len(pages),
        "warning_codes": warnings,
    }
    output = {
        "pages.jsonl": pages_bytes,
        "fulltext.txt": fulltext_bytes,
        "report.json": _json_bytes(report),
    }
    existing = destination.exists()
    identical = existing and all(
        (destination / name).is_file() and (destination / name).read_bytes() == content
        for name, content in output.items()
    )
    if identical:
        return ExtractionResult(relative.as_posix(), report)
    if existing and not force:
        raise PdfExtractionError("PDF_OUTPUT_CONFLICT")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / f".medlearn-stage-{uuid.uuid4().hex}"
    backup = destination.parent / f".medlearn-backup-{uuid.uuid4().hex}"
    try:
        stage.mkdir()
        for name, content in output.items():
            _atomic_write(stage / name, content)
        if existing:
            os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except OSError:
            if existing and backup.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            for child in backup.iterdir():
                child.unlink()
            backup.rmdir()
    except (PdfExtractionError, OSError):
        _cleanup_tree(stage)
        if backup.exists() and not destination.exists():
            try:
                os.replace(backup, destination)
            except OSError:
                pass
        _cleanup_tree(backup)
        raise
    return ExtractionResult(relative.as_posix(), report)


def extract_input(
    input_path: Path, output_root: Path, force: bool = False
) -> list[ExtractionResult | dict[str, str]]:
    pdfs = discover_pdfs(input_path)
    if not pdfs:
        raise PdfExtractionError("PDF_NOT_FOUND")
    source = _resolve(input_path.parent if input_path.is_file() else input_path)
    destination = _planned_resolve(output_root)
    if source == destination or _inside(destination, source) or _inside(source, destination):
        raise PdfExtractionError("PDF_OUTPUT_PATH_UNSAFE")
    output_root.mkdir(parents=True, exist_ok=True)
    destination = _resolve(output_root)
    results: list[ExtractionResult | dict[str, str]] = []
    for pdf in pdfs:
        try:
            results.append(extract_pdf(pdf, source, destination, force))
        except PdfExtractionError as exc:
            results.append(
                {"source_relative_path": pdf.relative_to(source).as_posix(), "error_code": exc.code}
            )
    return results
