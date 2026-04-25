from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from icon_gen import generate_icon
from processor import build_log_entry, check_batch_disk_space, collect_pdf_files, process_pdf


CONFIG_FILENAME = "config.json"


def get_base_path() -> Path:
    """Return the application base directory for scripts and frozen builds."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(name: str) -> Path:
    """Resolve a bundled resource path for both source and PyInstaller runs."""

    bundle_root = Path(getattr(sys, "_MEIPASS", get_base_path()))
    return bundle_root / name


def load_config(config_path: Path) -> dict[str, Any]:
    """Load persisted application settings from config.json if available."""

    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config_path: Path, config: dict[str, Any]) -> None:
    """Persist application settings to config.json."""

    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def build_parser(defaults: dict[str, Any]) -> argparse.ArgumentParser:
    """Create the command-line argument parser."""

    parser = argparse.ArgumentParser(description="Rasterize PDFs into image-only PDFs.")
    parser.add_argument("inputs", nargs="*", help="PDF files or folders to process")
    parser.add_argument("--gui", action="store_true", help="Force GUI mode even when inputs are provided")
    parser.add_argument("--dpi", type=int, default=int(defaults.get("dpi", 150)), help="Render DPI (72-300)")
    parser.add_argument("--format", choices=["JPEG", "PNG"], default=str(defaults.get("format", "JPEG")), help="Intermediate image format")
    parser.add_argument("--quality", type=int, default=int(defaults.get("quality", 85)), help="JPEG quality (1-100)")
    parser.add_argument("--output-dir", type=Path, default=Path(defaults["output_dir"]) if defaults.get("output_dir") else None, help="Custom output directory")
    parser.add_argument("--conflict", choices=["overwrite", "skip", "auto-rename"], default=str(defaults.get("conflict_mode", "auto-rename")), help="Output conflict mode")
    parser.add_argument("--include-subfolders", action="store_true", default=bool(defaults.get("include_subfolders", False)), help="Recursively include PDFs from folder inputs")
    return parser


def run_cli(args: argparse.Namespace) -> int:
    """Process PDFs from the command line and print structured progress logs."""

    source_paths = collect_pdf_files([Path(item) for item in args.inputs], include_subfolders=args.include_subfolders)
    if not source_paths:
        print("No PDF files found.", file=sys.stderr)
        return 1

    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        warnings = check_batch_disk_space(source_paths, args.dpi, args.format, args.output_dir)
        for warning in warnings:
            print(
                f"WARNING: Estimated output for {warning.destination} is {warning.estimated_bytes} bytes while free space is {warning.free_bytes} bytes.",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"WARNING: Disk space check failed: {exc}", file=sys.stderr)

    exit_code = 0
    for source_path in source_paths:
        result = process_pdf(
            source_path=source_path,
            dpi=args.dpi,
            image_format=args.format,
            jpeg_quality=args.quality,
            output_directory=args.output_dir,
            conflict_mode=args.conflict,
        )
        print(
            build_log_entry(
                source_path=result.source_path,
                output_path=result.output_path,
                dpi=args.dpi,
                image_format=args.format,
                jpeg_quality=args.quality,
                success=result.success or result.skipped,
                message=result.message if not result.skipped else "SKIPPED",
            )
        )
        if not result.success and not result.skipped:
            exit_code = 1
    return exit_code


def run_gui(config: dict[str, Any], config_path: Path, icon_path: Path) -> int:
    """Launch the desktop GUI and persist settings on exit."""

    from ui import PdfDeinjectionApp

    app = PdfDeinjectionApp(config=config, icon_path=icon_path)
    app.mainloop()
    save_config(config_path, app.get_persisted_config())
    return 0


def main() -> int:
    """Run the application in GUI or CLI mode based on the provided arguments."""

    base_path = get_base_path()
    config_path = base_path / CONFIG_FILENAME
    config = load_config(config_path)

    icon_path = get_resource_path("icon.ico")
    if not icon_path.exists() and not getattr(sys, "frozen", False):
        icon_path = generate_icon(base_path / "icon.ico")

    parser = build_parser(config)
    args = parser.parse_args()

    if args.gui or not args.inputs:
        return run_gui(config, config_path, icon_path)
    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())