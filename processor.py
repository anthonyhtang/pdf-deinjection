from __future__ import annotations

import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import fitz
from PIL import Image


OutputFormat = Literal["JPEG", "PNG"]
ConflictMode = Literal["overwrite", "skip", "auto-rename"]
CancelEvent = threading.Event
ProgressCallback = Callable[[dict], None]


class ProcessingCancelled(Exception):
    """Raised when a running PDF conversion is cancelled."""


@dataclass(slots=True)
class FileProcessResult:
    """Outcome information for one processed PDF file."""

    source_path: Path
    output_path: Path | None
    success: bool
    message: str
    total_pages: int = 0
    processed_pages: int = 0
    skipped: bool = False


@dataclass(slots=True)
class DiskSpaceWarning:
    """Disk space estimate for a destination directory."""

    destination: Path
    estimated_bytes: int
    free_bytes: int

    @property
    def exceeds_threshold(self) -> bool:
        """Return whether the estimate exceeds 90% of free space."""

        return self.estimated_bytes > int(self.free_bytes * 0.9)


def resolve_output_path(
    source_path: Path,
    output_directory: Path | None,
    conflict_mode: ConflictMode,
) -> Path | None:
    """Return the output PDF path for a source file based on the conflict mode."""

    destination_dir = output_directory or source_path.parent
    destination_dir.mkdir(parents=True, exist_ok=True)

    base_path = destination_dir / f"{source_path.stem}_deinjected.pdf"
    if not base_path.exists():
        return base_path

    if conflict_mode == "overwrite":
        return base_path
    if conflict_mode == "skip":
        return None

    index = 1
    while True:
        candidate = destination_dir / f"{source_path.stem}_deinjected_{index}.pdf"
        if not candidate.exists():
            return candidate
        index += 1


def estimate_page_output_size(page: fitz.Page, dpi: int, image_format: OutputFormat) -> int:
    """Estimate the output bytes for a rendered page."""

    scale = dpi / 72.0
    width_px = max(1, int(round(page.rect.width * scale)))
    height_px = max(1, int(round(page.rect.height * scale)))
    compression_ratio = 10 if image_format == "JPEG" else 3
    return int((page.number + 1) * 0 + (width_px * height_px * 3) / compression_ratio)


def estimate_pdf_output_size(source_path: Path, dpi: int, image_format: OutputFormat) -> int:
    """Estimate the rasterized output size in bytes for one PDF."""

    document = fitz.open(source_path)
    try:
        if document.needs_pass:
            raise ValueError("Password-protected PDF")
        if document.page_count == 0:
            return 0
        return sum(estimate_page_output_size(document.load_page(i), dpi, image_format) for i in range(document.page_count))
    finally:
        document.close()


def check_batch_disk_space(
    source_paths: list[Path],
    dpi: int,
    image_format: OutputFormat,
    output_directory: Path | None,
) -> list[DiskSpaceWarning]:
    """Return per-destination warnings when estimated output is close to disk exhaustion."""

    grouped_estimates: dict[Path, int] = {}
    for source_path in source_paths:
        destination = (output_directory or source_path.parent).resolve()
        grouped_estimates[destination] = grouped_estimates.get(destination, 0) + estimate_pdf_output_size(
            source_path,
            dpi,
            image_format,
        )

    warnings: list[DiskSpaceWarning] = []
    for destination, estimated_bytes in grouped_estimates.items():
        free_bytes = shutil.disk_usage(destination).free
        warning = DiskSpaceWarning(
            destination=destination,
            estimated_bytes=estimated_bytes,
            free_bytes=free_bytes,
        )
        if warning.exceeds_threshold:
            warnings.append(warning)

    return warnings


def process_pdf(
    source_path: Path,
    dpi: int,
    image_format: OutputFormat = "JPEG",
    jpeg_quality: int = 85,
    output_directory: Path | None = None,
    conflict_mode: ConflictMode = "auto-rename",
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> FileProcessResult:
    """Rasterize a PDF into a new image-only PDF file."""

    cancel_event = cancel_event or threading.Event()
    output_path = resolve_output_path(source_path, output_directory, conflict_mode)
    if output_path is None:
        return FileProcessResult(
            source_path=source_path,
            output_path=None,
            success=True,
            skipped=True,
            message="Skipped because output file already exists.",
        )

    document: fitz.Document | None = None
    output_document: fitz.Document | None = None
    temp_output_path: Path | None = None

    try:
        document = fitz.open(source_path)
        if document.needs_pass:
            return FileProcessResult(
                source_path=source_path,
                output_path=None,
                success=False,
                message="Password-protected PDF is not supported.",
            )
        if document.page_count == 0:
            return FileProcessResult(
                source_path=source_path,
                output_path=None,
                success=False,
                message="PDF contains zero pages.",
            )

        if progress_callback is not None:
            progress_callback(
                {
                    "event": "file_started",
                    "source_path": source_path,
                    "output_path": output_path,
                    "total_pages": document.page_count,
                }
            )

        output_document = fitz.open()
        with tempfile.TemporaryDirectory(prefix="pdf_deinjection_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            for page_index in range(document.page_count):
                if cancel_event.is_set():
                    raise ProcessingCancelled("Cancelled by user.")

                page = document.load_page(page_index)
                matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
                pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                image_path = temp_dir / f"page_{page_index + 1:04d}.{image_format.lower()}"

                if image_format == "JPEG":
                    image.save(image_path, format="JPEG", quality=jpeg_quality, optimize=True)
                else:
                    image.save(image_path, format="PNG", optimize=True)

                output_page = output_document.new_page(width=page.rect.width, height=page.rect.height)
                output_page.insert_image(output_page.rect, filename=str(image_path), keep_proportion=False)

                if progress_callback is not None:
                    progress_callback(
                        {
                            "event": "page_completed",
                            "source_path": source_path,
                            "output_path": output_path,
                            "page_index": page_index + 1,
                            "total_pages": document.page_count,
                        }
                    )

            temp_output_path = output_path.with_name(f"{output_path.stem}.partial.pdf")
            if temp_output_path.exists():
                temp_output_path.unlink()
            output_document.save(temp_output_path, garbage=4, deflate=True)

        if output_path.exists() and conflict_mode == "overwrite":
            output_path.unlink()
        temp_output_path.replace(output_path)

        result = FileProcessResult(
            source_path=source_path,
            output_path=output_path,
            success=True,
            message="OK",
            total_pages=document.page_count,
            processed_pages=document.page_count,
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "event": "file_completed",
                    "source_path": source_path,
                    "output_path": output_path,
                    "total_pages": document.page_count,
                }
            )
        return result
    except ProcessingCancelled:
        if temp_output_path is not None and temp_output_path.exists():
            temp_output_path.unlink(missing_ok=True)
        return FileProcessResult(
            source_path=source_path,
            output_path=output_path,
            success=False,
            message="Cancelled",
        )
    except Exception as exc:
        if temp_output_path is not None and temp_output_path.exists():
            temp_output_path.unlink(missing_ok=True)
        return FileProcessResult(
            source_path=source_path,
            output_path=output_path,
            success=False,
            message=str(exc),
        )
    finally:
        if output_document is not None:
            output_document.close()
        if document is not None:
            document.close()
