from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageOps
from tkinterdnd2 import DND_FILES, TkinterDnD

from processor import (
    OutputFormat,
    build_log_entry,
    collect_pdf_files,
    format_bytes,
    process_pdf,
    read_pdf_info,
    render_preview,
)


STATUS_ICONS = {
    "pending": "⏳",
    "processing": "⚙️",
    "done": "✅",
    "error": "❌",
    "skipped": "⏭️",
}

STATUS_COLORS = {
    "pending": ("#edf3fb", "#edf3fb"),
    "processing": ("#d9e8fb", "#d9e8fb"),
    "done": ("#ddf4e6", "#ddf4e6"),
    "error": ("#fde8ea", "#fde8ea"),
    "skipped": ("#f9f0d8", "#f9f0d8"),
}

PRESET_DPI_VALUES = (96, 150, 200, 300)
IDEAL_WINDOW_WIDTH = 1360
IDEAL_WINDOW_HEIGHT = 1280
BASE_MIN_WINDOW_WIDTH = 1040
BASE_MIN_WINDOW_HEIGHT = 880
SIDE_PANEL_WIDTH = 300
SIDE_PANEL_MIN_ACTUAL = 248
SIDE_PANEL_MAX_ACTUAL = 336
RIGHT_PANEL_WIDTH = 372
RIGHT_PANEL_MIN_ACTUAL = 336
RIGHT_PANEL_MAX_ACTUAL = 432
APP_BG_COLOR = "#f4f6f8"
SURFACE_COLOR = "#f4f6f8"
CARD_COLOR = "#eef3f8"
DROP_ZONE_COLOR = "#eef5ff"
ACCENT_TEXT_COLOR = "#486284"
RIGHT_PANEL_INNER_PADX = 18


@dataclass(slots=True)
class QueueFileEntry:
    """UI state for a queued PDF file."""

    path: Path
    page_count: int
    file_size: int
    width_points: float
    height_points: float
    status: str = "pending"
    estimated_output_size: int = 0
    output_path: Path | None = None
    error_message: str = ""


class CTkDnD(ctk.CTk, TkinterDnD.DnDWrapper):
    """CustomTkinter root window with tkinterdnd2 drag-and-drop support."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.TkdndVersion = TkinterDnD._require(self)


class PdfDeinjectionApp(CTkDnD):
    """Main desktop UI for PDF Deinjection."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        icon_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.screen_width = self.winfo_screenwidth()
        self.screen_height = self.winfo_screenheight()
        self.window_scaling = float(self._get_window_scaling())
        self.min_window_width, self.min_window_height = self._compute_min_window_size()
        self.default_window_width, self.default_window_height = self._compute_default_window_size()
        self.title("PDF Deinjection")
        self.geometry(f"{self.default_window_width}x{self.default_window_height}")
        self.minsize(self.min_window_width, self.min_window_height)
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=APP_BG_COLOR)

        self.icon_path = icon_path
        if icon_path is not None and icon_path.exists():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass

        self.progress_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.processing_active = False
        self.exit_after_cancel = False
        self.completed_output_dirs: set[Path] = set()
        self.desktop_path = Path.home() / "Desktop"

        self.queue_entries: dict[Path, QueueFileEntry] = {}
        self.row_widgets: dict[Path, ctk.CTkFrame] = {}
        self.selected_path: Path | None = None
        self.preview_photo: ctk.CTkImage | None = None
        self.preview_source_image: Image.Image | None = None

        defaults = config or {}
        self.dpi_var = tk.IntVar(value=int(defaults.get("dpi", 150)))
        self.format_var = tk.StringVar(value=str(defaults.get("format", "JPEG")))
        self.quality_var = tk.IntVar(value=int(defaults.get("quality", 85)))
        self.output_mode_var = tk.StringVar(value=str(defaults.get("output_mode", "same")))
        self.custom_output_var = tk.StringVar(value=str(defaults.get("output_dir", self.desktop_path)))
        self.conflict_var = tk.StringVar(value=str(defaults.get("conflict_mode", "auto-rename")))
        self.include_subfolders_var = tk.BooleanVar(value=bool(defaults.get("include_subfolders", False)))
        self.log_visible = False
        self.last_window_geometry = str(defaults.get("window_geometry", f"{self.default_window_width}x{self.default_window_height}"))

        self._build_layout()
        self._restore_geometry(defaults.get("window_geometry"))
        self._bind_events()
        self._update_quality_visibility()
        self._update_output_mode_state()
        self._refresh_progress_labels(0, 0, 0, 0)
        self._update_preview()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll_progress_queue)

    def get_persisted_config(self) -> dict[str, Any]:
        """Return the app settings that should be written to config.json."""

        try:
            exists = bool(self.winfo_exists())
        except tk.TclError:
            exists = False

        if exists:
            try:
                self.last_window_geometry = self.geometry()
            except tk.TclError:
                pass

        return {
            "dpi": int(self.dpi_var.get()),
            "format": self.format_var.get(),
            "quality": int(self.quality_var.get()),
            "output_mode": self.output_mode_var.get(),
            "output_dir": self.custom_output_var.get().strip(),
            "conflict_mode": self.conflict_var.get(),
            "include_subfolders": bool(self.include_subfolders_var.get()),
            "window_geometry": self.last_window_geometry,
        }

    def _compute_min_window_size(self) -> tuple[int, int]:
        min_width_actual = min(1240, max(BASE_MIN_WINDOW_WIDTH, int(self.screen_width * 0.52)))
        min_height_actual = min(int(self.screen_height * 0.82), max(BASE_MIN_WINDOW_HEIGHT, int(self.screen_height * 0.72)))
        return self._to_logical_window_size(min_width_actual, min_height_actual)

    def _compute_default_window_size(self) -> tuple[int, int]:
        default_width_actual = min(IDEAL_WINDOW_WIDTH, max(1180, int(self.screen_width * 0.68)))
        default_height_actual = min(int(self.screen_height * 0.95), max(1040, int(self.screen_height * 0.92)))
        default_width, default_height = self._to_logical_window_size(default_width_actual, default_height_actual)
        default_width = max(self.min_window_width, default_width)
        default_height = max(self.min_window_height, default_height)
        return default_width, default_height

    def _to_logical_window_size(self, width: int, height: int) -> tuple[int, int]:
        logical_width = max(1, int(round(width / self.window_scaling)))
        logical_height = max(1, int(round(height / self.window_scaling)))
        return logical_width, logical_height

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=0)

        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color=SURFACE_COLOR)
        self.main_frame.grid(row=0, column=0, sticky="nsew")
        self.main_frame.grid_columnconfigure(0, weight=0, minsize=SIDE_PANEL_WIDTH)
        self.main_frame.grid_columnconfigure(1, weight=1)
        self.main_frame.grid_columnconfigure(2, weight=0, minsize=RIGHT_PANEL_WIDTH)
        self.main_frame.grid_rowconfigure(0, weight=1)

        self.left_panel = ctk.CTkFrame(self.main_frame, width=SIDE_PANEL_WIDTH, fg_color=CARD_COLOR)
        self.left_panel.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        self.left_panel.grid_propagate(False)
        self.left_panel.grid_rowconfigure(1, weight=1)
        self.left_panel.grid_columnconfigure(0, weight=1)

        self.center_panel = ctk.CTkFrame(self.main_frame, fg_color=CARD_COLOR)
        self.center_panel.grid(row=0, column=1, sticky="nsew", padx=6, pady=12)
        self.center_panel.grid_rowconfigure(0, weight=1)
        self.center_panel.grid_columnconfigure(0, weight=1)

        self.right_panel = ctk.CTkFrame(self.main_frame, width=RIGHT_PANEL_WIDTH, fg_color=CARD_COLOR)
        self.right_panel.grid(row=0, column=2, sticky="nsew", padx=(6, 18), pady=12)
        self.right_panel.grid_propagate(False)
        self.right_panel.grid_rowconfigure(0, weight=1)
        self.right_panel.grid_columnconfigure(0, weight=1)

        self.bottom_bar = ctk.CTkFrame(self, height=94, fg_color=CARD_COLOR)
        self.bottom_bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        self.bottom_bar.grid_columnconfigure(0, weight=1)
        self.bottom_bar.grid_columnconfigure(1, weight=0)

        self.log_container = ctk.CTkFrame(self, fg_color=CARD_COLOR)
        self.log_container.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.log_container.grid_columnconfigure(0, weight=1)

        self._build_left_panel()
        self._build_center_panel()
        self._build_right_panel()
        self._build_bottom_bar()
        self._build_log_panel()

    def _build_left_panel(self) -> None:
        self.drop_zone = ctk.CTkFrame(self.left_panel, fg_color=DROP_ZONE_COLOR, corner_radius=12)
        self.drop_zone.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 10))
        self.drop_zone.grid_columnconfigure(0, weight=1)

        self.drop_canvas = tk.Canvas(
            self.drop_zone,
            height=140,
            bd=0,
            highlightthickness=0,
            relief="flat",
            background=DROP_ZONE_COLOR,
        )
        self.drop_canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self._redraw_drop_zone()

        self.file_list = ctk.CTkScrollableFrame(self.left_panel, label_text="Queue")
        self.file_list.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))
        self.file_list.grid_columnconfigure(0, weight=1)
        self.file_list.configure(fg_color="#f7f9fb")

        controls = ctk.CTkFrame(self.left_panel, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        controls.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkButton(controls, text="Add Files", command=self.add_files_dialog).grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 6))
        ctk.CTkButton(controls, text="Add Folder", command=self.add_folder_dialog).grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=(0, 6))
        ctk.CTkButton(controls, text="Remove Selected", command=self.remove_selected).grid(row=1, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(controls, text="Clear All", command=self.clear_all).grid(row=1, column=1, sticky="ew", padx=(6, 0))
        ctk.CTkCheckBox(controls, text="Include Subfolders", variable=self.include_subfolders_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _build_center_panel(self) -> None:
        self.preview_holder = ctk.CTkFrame(self.center_panel, fg_color="#f7f9fb")
        self.preview_holder.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self.preview_holder.grid_rowconfigure(0, weight=1)
        self.preview_holder.grid_columnconfigure(0, weight=1)

        self.preview_label = ctk.CTkLabel(self.preview_holder, text="", compound="center")
        self.preview_label.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        self.metadata_frame = ctk.CTkFrame(self.center_panel, fg_color="#f7f9fb")
        self.metadata_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.metadata_frame.grid_columnconfigure((0, 1), weight=1)

        self.meta_filename = ctk.CTkLabel(self.metadata_frame, text="Filename: -", anchor="w")
        self.meta_filename.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        self.meta_pages = ctk.CTkLabel(self.metadata_frame, text="Pages: -", anchor="w")
        self.meta_pages.grid(row=0, column=1, sticky="ew", padx=10, pady=(10, 4))
        self.meta_size = ctk.CTkLabel(self.metadata_frame, text="File size: -", anchor="w")
        self.meta_size.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.meta_estimated = ctk.CTkLabel(self.metadata_frame, text="Estimated output: -", anchor="w")
        self.meta_estimated.grid(row=1, column=1, sticky="ew", padx=10, pady=(0, 10))

    def _build_right_panel(self) -> None:
        self.settings_panel = ctk.CTkScrollableFrame(
            self.right_panel,
            width=RIGHT_PANEL_WIDTH,
            fg_color=CARD_COLOR,
        )
        self.settings_panel.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self.settings_panel.grid_columnconfigure(0, weight=1)

        panel = self.settings_panel
        row = 0
        ctk.CTkLabel(panel, text="Resolution", font=ctk.CTkFont(size=16, weight="bold")).grid(row=row, column=0, sticky="w", padx=14, pady=(14, 6))
        row += 1

        dpi_row = ctk.CTkFrame(panel, fg_color="transparent")
        dpi_row.grid(row=row, column=0, sticky="ew", padx=14)
        dpi_row.grid_columnconfigure(0, weight=1)
        self.dpi_value_label = ctk.CTkLabel(dpi_row, text=f"{self.dpi_var.get()} DPI")
        self.dpi_value_label.grid(row=0, column=1, sticky="e")
        row += 1

        self.dpi_slider = ctk.CTkSlider(panel, from_=72, to=300, number_of_steps=228, variable=self.dpi_var, command=self.on_dpi_changed)
        self.dpi_slider.grid(row=row, column=0, sticky="ew", padx=14, pady=(4, 8))
        row += 1

        preset_row = ctk.CTkFrame(panel, fg_color="transparent")
        preset_row.grid(row=row, column=0, sticky="ew", padx=14)
        preset_row.grid_columnconfigure((0, 1, 2, 3), weight=1)
        for column, value in enumerate(PRESET_DPI_VALUES):
            ctk.CTkButton(preset_row, text=str(value), width=44, command=lambda preset=value: self.set_dpi(preset)).grid(row=0, column=column, sticky="ew", padx=3)
        row += 1

        ctk.CTkLabel(panel, text="Format", font=ctk.CTkFont(size=16, weight="bold")).grid(row=row, column=0, sticky="w", padx=14, pady=(16, 6))
        row += 1

        self.format_selector = ctk.CTkSegmentedButton(panel, values=["JPEG", "PNG"], variable=self.format_var, command=self.on_format_changed)
        self.format_selector.grid(row=row, column=0, sticky="ew", padx=14)
        row += 1

        self.quality_frame = ctk.CTkFrame(panel, fg_color="transparent")
        self.quality_frame.grid(row=row, column=0, sticky="ew", padx=14, pady=(12, 0))
        self.quality_frame.grid_columnconfigure(0, weight=1)
        self.quality_label = ctk.CTkLabel(self.quality_frame, text=f"JPEG Quality: {self.quality_var.get()}")
        self.quality_label.grid(row=0, column=0, sticky="w")
        self.quality_slider = ctk.CTkSlider(self.quality_frame, from_=1, to=100, number_of_steps=99, variable=self.quality_var, command=self.on_quality_changed)
        self.quality_slider.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        row += 1

        ctk.CTkLabel(panel, text="Output Location", font=ctk.CTkFont(size=16, weight="bold")).grid(row=row, column=0, sticky="w", padx=14, pady=(16, 6))
        row += 1

        self.same_folder_radio = ctk.CTkRadioButton(panel, text="Same folder as source", variable=self.output_mode_var, value="same", command=self._update_output_mode_state)
        self.same_folder_radio.grid(row=row, column=0, sticky="w", padx=14)
        row += 1
        self.custom_folder_radio = ctk.CTkRadioButton(panel, text="Custom folder", variable=self.output_mode_var, value="custom", command=self._update_output_mode_state)
        self.custom_folder_radio.grid(row=row, column=0, sticky="w", padx=14, pady=(4, 0))
        row += 1

        self.custom_folder_frame = ctk.CTkFrame(panel, fg_color="transparent")
        self.custom_folder_frame.grid(row=row, column=0, sticky="ew", padx=14, pady=(6, 0))
        self.custom_folder_frame.grid_columnconfigure(0, weight=1)
        self.custom_folder_entry = ctk.CTkEntry(self.custom_folder_frame, textvariable=self.custom_output_var, state="readonly")
        self.custom_folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(self.custom_folder_frame, text="Browse", width=72, command=self.browse_output_folder).grid(row=0, column=1)
        row += 1

        ctk.CTkLabel(panel, text="File Conflict", font=ctk.CTkFont(size=16, weight="bold")).grid(row=row, column=0, sticky="w", padx=14, pady=(16, 6))
        row += 1
        ctk.CTkRadioButton(panel, text="Overwrite", variable=self.conflict_var, value="overwrite").grid(row=row, column=0, sticky="w", padx=14)
        row += 1
        ctk.CTkRadioButton(panel, text="Skip", variable=self.conflict_var, value="skip").grid(row=row, column=0, sticky="w", padx=14, pady=(4, 0))
        row += 1
        ctk.CTkRadioButton(panel, text="Auto-rename", variable=self.conflict_var, value="auto-rename").grid(row=row, column=0, sticky="w", padx=14, pady=(4, 0))
        row += 1

        ctk.CTkFrame(panel, height=2).grid(row=row, column=0, sticky="ew", padx=14, pady=(16, 16))

        self.start_button = ctk.CTkButton(
            self.right_panel,
            text="START",
            fg_color="#198754",
            hover_color="#157347",
            height=44,
            command=self.on_start_cancel,
        )
        self.start_button.grid(row=1, column=0, sticky="ew", padx=RIGHT_PANEL_INNER_PADX, pady=(0, 14))

    def _build_bottom_bar(self) -> None:
        progress_panel = ctk.CTkFrame(self.bottom_bar, fg_color=CARD_COLOR)
        progress_panel.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        progress_panel.grid_columnconfigure(0, weight=1)

        self.overall_label = ctk.CTkLabel(progress_panel, text="0 / 0 files", anchor="w")
        self.overall_label.grid(row=0, column=0, sticky="ew")
        self.overall_progress = ctk.CTkProgressBar(progress_panel)
        self.overall_progress.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        self.overall_progress.set(0)

        self.file_label = ctk.CTkLabel(progress_panel, text="Page 0 of 0", anchor="w")
        self.file_label.grid(row=2, column=0, sticky="ew")
        self.file_progress = ctk.CTkProgressBar(progress_panel)
        self.file_progress.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.file_progress.set(0)

        action_panel = ctk.CTkFrame(self.bottom_bar, fg_color=CARD_COLOR)
        action_panel.grid(row=0, column=1, sticky="e", padx=12, pady=12)

        self.status_label = ctk.CTkLabel(action_panel, text="Ready", anchor="e")
        self.status_label.grid(row=0, column=0, sticky="e", pady=(0, 8))
        self.open_output_button = ctk.CTkButton(action_panel, text="Open Output Folder", state="disabled", command=self.open_output_folder)
        self.open_output_button.grid(row=1, column=0, sticky="e")

    def _build_log_panel(self) -> None:
        self.log_toggle = ctk.CTkButton(self.log_container, text="▲ Show Log", width=120, command=self.toggle_log_panel)
        self.log_toggle.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 0))

        self.log_body = ctk.CTkFrame(self.log_container, fg_color="#f7f9fb")
        self.log_body.grid(row=1, column=0, sticky="ew", padx=12, pady=12)
        self.log_body.grid_columnconfigure(0, weight=1)

        self.log_text = ctk.CTkTextbox(self.log_body, height=120, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_text.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.log_text.configure(state="disabled")
        ctk.CTkButton(self.log_body, text="Copy Log", width=100, command=self.copy_log).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ctk.CTkButton(self.log_body, text="Clear Log", width=100, command=self.clear_log).grid(row=1, column=1, sticky="e", pady=(8, 0))
        self.log_body.grid_remove()

    def _bind_events(self) -> None:
        self.drop_zone.drop_target_register(DND_FILES)
        self.drop_zone.dnd_bind("<<Drop>>", self.on_drop_files)
        self.drop_canvas.drop_target_register(DND_FILES)
        self.drop_canvas.dnd_bind("<<Drop>>", self.on_drop_files)
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, _event: tk.Event) -> None:
        try:
            self.last_window_geometry = self.geometry()
        except tk.TclError:
            pass
        try:
            if self.winfo_exists():
                self._apply_responsive_layout()
        except tk.TclError:
            pass

    def _restore_geometry(self, geometry: str | None) -> None:
        target_geometry = self._normalize_geometry(geometry)
        try:
            self.geometry(target_geometry)
            self.last_window_geometry = target_geometry
        except tk.TclError:
            fallback_geometry = f"{self.default_window_width}x{self.default_window_height}"
            self.geometry(fallback_geometry)
            self.last_window_geometry = fallback_geometry

    def _apply_responsive_layout(self) -> None:
        try:
            width = self.winfo_width()
            height = self.winfo_height()
        except tk.TclError:
            return
        if width <= 1 or height <= 1:
            return

        left_panel_width_actual = max(SIDE_PANEL_MIN_ACTUAL, min(SIDE_PANEL_MAX_ACTUAL, int(width * 0.22)))
        right_panel_width_actual = max(RIGHT_PANEL_MIN_ACTUAL, min(RIGHT_PANEL_MAX_ACTUAL, int(width * 0.285)))
        left_panel_width = max(190, int(round(left_panel_width_actual / self.window_scaling)))
        right_panel_width = max(220, int(round(right_panel_width_actual / self.window_scaling)))

        self.left_panel.configure(width=left_panel_width)
        self.right_panel.configure(width=right_panel_width)
        self.settings_panel.configure(width=max(220, right_panel_width))
        self.main_frame.grid_columnconfigure(0, minsize=left_panel_width)
        self.main_frame.grid_columnconfigure(2, minsize=right_panel_width)

        metadata_font_size = 12 if width < 1280 else 13
        placeholder_font_size = 16 if width < 1280 else 20
        for label in (self.meta_filename, self.meta_pages, self.meta_size, self.meta_estimated):
            label.configure(font=ctk.CTkFont(size=metadata_font_size))

        self._redraw_drop_zone()
        if self.preview_source_image is not None:
            self._display_preview_image(self.preview_source_image)
        else:
            current_text = self.preview_label.cget("text") or "PDF Preview\n\nSelect a PDF to preview"
            self.preview_label.configure(font=ctk.CTkFont(size=placeholder_font_size, weight="bold"), text=current_text)

    def _redraw_drop_zone(self) -> None:
        canvas_width = max(180, self.drop_canvas.winfo_width() or 220)
        canvas_height = max(120, self.drop_canvas.winfo_height() or 140)
        self.drop_canvas.delete("all")
        self.drop_canvas.configure(width=canvas_width, height=canvas_height)

        margin_x = 14
        margin_y = 14
        self.drop_canvas.create_rectangle(
            margin_x,
            margin_y,
            canvas_width - margin_x,
            canvas_height - margin_y,
            outline="#7ca0d6",
            width=2,
            dash=(6, 4),
        )
        self.drop_canvas.create_text(
            canvas_width / 2,
            canvas_height * 0.38,
            text="PDF",
            fill="#1f4a8a",
            font=("Segoe UI", max(18, int(canvas_height * 0.16)), "bold"),
        )
        self.drop_canvas.create_text(
            canvas_width / 2,
            canvas_height * 0.62,
            text="Drop PDFs here",
            fill="#4e6e9d",
            font=("Segoe UI", max(11, int(canvas_height * 0.09))),
        )

    def _normalize_geometry(self, geometry: str | None) -> str:
        if not geometry:
            return f"{self.default_window_width}x{self.default_window_height}"

        size_part, _, offset_part = geometry.partition("+")
        try:
            width_str, height_str = size_part.split("x", maxsplit=1)
            width = max(self.default_window_width, int(width_str))
            height = max(self.default_window_height, int(height_str))
            width = min(self.screen_width - 80, width)
            height = min(self.screen_height - 80, height)
        except (ValueError, TypeError):
            return f"{self.default_window_width}x{self.default_window_height}"

        if offset_part:
            offsets = geometry[len(size_part):]
            return f"{width}x{height}{offsets}"
        return f"{width}x{height}"

    def _refresh_queue_list(self) -> None:
        for child in self.file_list.winfo_children():
            child.destroy()
        self.row_widgets.clear()

        for index, entry in enumerate(self.queue_entries.values()):
            selected = entry.path == self.selected_path
            row = ctk.CTkFrame(
                self.file_list,
                fg_color=("#cfe2ff", "#cfe2ff") if selected else STATUS_COLORS.get(entry.status, ("#edf3fb", "#edf3fb")),
            )
            row.grid(row=index, column=0, sticky="ew", padx=4, pady=4)
            row.grid_columnconfigure(1, weight=1)
            self.row_widgets[entry.path] = row

            icon_label = ctk.CTkLabel(row, text=STATUS_ICONS.get(entry.status, "⏳"), width=24)
            icon_label.grid(row=0, column=0, padx=(8, 6), pady=8)
            name_label = ctk.CTkLabel(row, text=self._truncate_filename(entry.path.name), anchor="w")
            name_label.grid(row=0, column=1, sticky="ew", pady=(6, 0), padx=(0, 6))
            page_text = f"{entry.page_count} pages" if entry.page_count > 0 else "-"
            page_label = ctk.CTkLabel(row, text=page_text, anchor="w", text_color="#5f718c")
            page_label.grid(row=1, column=1, sticky="ew", pady=(0, 6), padx=(0, 6))

            for widget in (row, icon_label, name_label, page_label):
                widget.bind("<Button-1>", lambda _event, path=entry.path: self.select_file(path))

    def _truncate_filename(self, name: str, max_length: int = 28) -> str:
        if len(name) <= max_length:
            return name
        return f"{name[:max_length - 3]}..."

    def _estimate_entry_output_size(self, entry: QueueFileEntry) -> int:
        scale = self.dpi_var.get() / 72.0
        width_px = max(1, int(round(entry.width_points * scale)))
        height_px = max(1, int(round(entry.height_points * scale)))
        compression_ratio = 10 if self.format_var.get() == "JPEG" else 3
        return int(entry.page_count * width_px * height_px * 3 / compression_ratio)

    def _recalculate_estimates(self) -> None:
        for entry in self.queue_entries.values():
            if entry.page_count > 0:
                entry.estimated_output_size = self._estimate_entry_output_size(entry)
        self._refresh_queue_list()
        self._update_metadata_strip()

    def _update_preview(self) -> None:
        if self.selected_path is None or self.selected_path not in self.queue_entries:
            self._show_preview_placeholder()
            self._update_metadata_strip()
            return

        entry = self.queue_entries[self.selected_path]
        if entry.status == "error" and entry.error_message:
            self.preview_source_image = None
            self._show_preview_placeholder(entry.error_message)
            self._update_metadata_strip()
            return

        try:
            preview_image = render_preview(entry.path, dpi=96)
            self.preview_source_image = preview_image
            self._display_preview_image(preview_image)
        except Exception as exc:
            self.preview_source_image = None
            self._show_preview_placeholder(str(exc))
        self._update_metadata_strip()

    def _show_preview_placeholder(self, message: str | None = None) -> None:
        placeholder = message or "Select a PDF to preview"
        width = max(self.winfo_width(), self.default_window_width)
        font_size = 16 if width < 1280 else 20
        self.preview_label.configure(
            image=None,
            text=f"PDF Preview\n\n{placeholder}",
            font=ctk.CTkFont(size=font_size, weight="bold"),
            text_color=ACCENT_TEXT_COLOR,
        )

    def _display_preview_image(self, source_image: Image.Image) -> None:
        preview_width = max(260, self.preview_holder.winfo_width() - 44)
        preview_height = max(260, self.preview_holder.winfo_height() - 44)
        fitted = ImageOps.contain(source_image, (preview_width, preview_height))
        self.preview_photo = ctk.CTkImage(light_image=fitted, dark_image=fitted, size=fitted.size)
        self.preview_label.configure(image=self.preview_photo, text="")

    def _update_metadata_strip(self) -> None:
        if self.selected_path is None or self.selected_path not in self.queue_entries:
            self.meta_filename.configure(text="Filename: -")
            self.meta_pages.configure(text="Pages: -")
            self.meta_size.configure(text="File size: -")
            self.meta_estimated.configure(text="Estimated output: -")
            return

        entry = self.queue_entries[self.selected_path]
        self.meta_filename.configure(text=f"Filename: {entry.path.name}")
        self.meta_pages.configure(text=f"Pages: {entry.page_count or '-'}")
        self.meta_size.configure(text=f"File size: {format_bytes(entry.file_size)}")
        self.meta_estimated.configure(text=f"Estimated output: {format_bytes(entry.estimated_output_size)}")

    def _update_quality_visibility(self) -> None:
        if self.format_var.get() == "JPEG":
            self.quality_frame.grid()
        else:
            self.quality_frame.grid_remove()

    def _update_output_mode_state(self) -> None:
        state = "normal" if self.output_mode_var.get() == "custom" else "disabled"
        self.custom_folder_entry.configure(state=state if state == "disabled" else "readonly")
        for child in self.custom_folder_frame.winfo_children():
            if isinstance(child, ctk.CTkButton):
                child.configure(state=state)

    def _set_processing_state(self, active: bool) -> None:
        self.processing_active = active
        if active:
            self.start_button.configure(text="CANCEL", fg_color="#b02a37", hover_color="#8f1d2b")
            self.status_label.configure(text="Processing...")
        else:
            self.start_button.configure(text="START", fg_color="#198754", hover_color="#157347")
            self.status_label.configure(text="Ready")

    def _refresh_progress_labels(self, done_files: int, total_files: int, page_index: int, total_pages: int) -> None:
        self.overall_label.configure(text=f"{done_files} / {total_files} files")
        self.file_label.configure(text=f"Page {page_index} of {total_pages}")
        self.overall_progress.set(0 if total_files == 0 else done_files / total_files)
        self.file_progress.set(0 if total_pages == 0 else page_index / total_pages)

    def add_files_dialog(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select PDF files",
            filetypes=[("PDF files", "*.pdf")],
            initialdir=self.desktop_path,
        )
        if paths:
            self.add_paths([Path(path) for path in paths])

    def add_folder_dialog(self) -> None:
        folder = filedialog.askdirectory(title="Select folder", initialdir=self.desktop_path)
        if folder:
            self.add_paths([Path(folder)])

    def add_paths(self, paths: list[Path]) -> None:
        candidates = collect_pdf_files(paths, include_subfolders=self.include_subfolders_var.get())
        if not candidates:
            self.append_log("[INFO] No PDF files found in the selected location.")
            return

        for path in candidates:
            if path in self.queue_entries:
                continue
            try:
                info = read_pdf_info(path, dpi=self.dpi_var.get(), image_format=self.format_var.get())
                self.queue_entries[path] = QueueFileEntry(
                    path=path,
                    page_count=info.page_count,
                    file_size=info.file_size,
                    width_points=info.width_points,
                    height_points=info.height_points,
                    estimated_output_size=info.estimated_output_size,
                )
            except Exception as exc:
                self.queue_entries[path] = QueueFileEntry(
                    path=path,
                    page_count=0,
                    file_size=path.stat().st_size if path.exists() else 0,
                    width_points=0,
                    height_points=0,
                    status="error",
                    error_message=str(exc),
                )
                self.append_log(f"[ERROR] {path.name}: {exc}")

        if self.selected_path is None and self.queue_entries:
            self.selected_path = next(iter(self.queue_entries))
        self._refresh_queue_list()
        self._update_preview()

    def remove_selected(self) -> None:
        if self.selected_path is None:
            return
        self.queue_entries.pop(self.selected_path, None)
        self.selected_path = next(iter(self.queue_entries), None)
        self._refresh_queue_list()
        self._update_preview()

    def clear_all(self) -> None:
        if self.processing_active:
            return
        self.queue_entries.clear()
        self.selected_path = None
        self.completed_output_dirs.clear()
        self.open_output_button.configure(state="disabled")
        self._refresh_queue_list()
        self._update_preview()
        self._refresh_progress_labels(0, 0, 0, 0)

    def select_file(self, path: Path) -> None:
        self.selected_path = path
        self._refresh_queue_list()
        self._update_preview()

    def on_drop_files(self, event: Any) -> str:
        paths = [Path(item) for item in self.tk.splitlist(event.data)]
        self.add_paths(paths)
        return "break"

    def set_dpi(self, dpi: int) -> None:
        self.dpi_var.set(dpi)
        self.on_dpi_changed(float(dpi))

    def on_dpi_changed(self, value: float) -> None:
        dpi = int(round(value))
        self.dpi_var.set(dpi)
        self.dpi_value_label.configure(text=f"{dpi} DPI")
        self._recalculate_estimates()

    def on_format_changed(self, _value: str) -> None:
        self._update_quality_visibility()
        self._recalculate_estimates()

    def on_quality_changed(self, value: float) -> None:
        quality = int(round(value))
        self.quality_var.set(quality)
        self.quality_label.configure(text=f"JPEG Quality: {quality}")

    def browse_output_folder(self) -> None:
        folder = filedialog.askdirectory(
            title="Select output folder",
            initialdir=self.custom_output_var.get() or str(self.desktop_path),
        )
        if folder:
            self.custom_output_var.set(folder)

    def toggle_log_panel(self) -> None:
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_body.grid()
            self.log_toggle.configure(text="▼ Hide Log")
        else:
            self.log_body.grid_remove()
            self.log_toggle.configure(text="▲ Show Log")

    def append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{line}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def copy_log(self) -> None:
        text = self.log_text.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def on_start_cancel(self) -> None:
        if self.processing_active:
            self.request_cancel()
            return
        self.start_processing()

    def _get_output_directory(self) -> Path | None:
        if self.output_mode_var.get() == "custom":
            folder = self.custom_output_var.get().strip()
            if not folder:
                messagebox.showerror("Output Folder", "Please choose a custom output folder.")
                return None
            return Path(folder)
        return None

    def _check_disk_space_warning(self, files: list[QueueFileEntry], output_dir: Path | None) -> bool:
        grouped: dict[Path, int] = {}
        for entry in files:
            destination = (output_dir or entry.path.parent).resolve()
            grouped[destination] = grouped.get(destination, 0) + entry.estimated_output_size

        warnings: list[str] = []
        for destination, estimate in grouped.items():
            try:
                free = shutil.disk_usage(destination).free
            except OSError:
                continue
            if estimate > int(free * 0.9):
                warnings.append(
                    f"{destination}\nEstimated output: {format_bytes(estimate)}\nFree space: {format_bytes(free)}"
                )

        if not warnings:
            return True
        return messagebox.askyesno(
            "Disk Space Warning",
            "Estimated output size exceeds 90% of available space for:\n\n" + "\n\n".join(warnings) + "\n\nContinue anyway?",
        )

    def start_processing(self) -> None:
        files = [entry for entry in self.queue_entries.values() if entry.page_count > 0]
        if not files:
            messagebox.showinfo("No Files", "Add at least one valid PDF before starting.")
            return

        output_dir = self._get_output_directory()
        if self.output_mode_var.get() == "custom" and output_dir is None:
            return
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)

        if not self._check_disk_space_warning(files, output_dir):
            return

        self.completed_output_dirs.clear()
        self.cancel_event = threading.Event()
        self._set_processing_state(True)
        self._refresh_progress_labels(0, len(files), 0, 0)
        self.append_log("[INFO] Batch processing started.")

        self.worker_thread = threading.Thread(
            target=self._worker_process_files,
            args=(files, output_dir),
            daemon=True,
        )
        self.worker_thread.start()

    def request_cancel(self) -> None:
        if self.processing_active:
            self.cancel_event.set()
            self.status_label.configure(text="Cancelling...")

    def _worker_process_files(self, files: list[QueueFileEntry], output_dir: Path | None) -> None:
        total_files = len(files)
        processed_count = 0
        for entry in files:
            if self.cancel_event.is_set():
                break

            result = process_pdf(
                source_path=entry.path,
                dpi=self.dpi_var.get(),
                image_format=self.format_var.get(),
                jpeg_quality=self.quality_var.get(),
                output_directory=output_dir,
                conflict_mode=self.conflict_var.get(),
                cancel_event=self.cancel_event,
                progress_callback=self.progress_queue.put,
            )
            processed_count += 1
            self.progress_queue.put(
                {
                    "event": "file_result",
                    "result": result,
                    "processed_count": processed_count,
                    "total_files": total_files,
                    "dpi": self.dpi_var.get(),
                    "image_format": self.format_var.get(),
                    "jpeg_quality": self.quality_var.get(),
                }
            )
            if self.cancel_event.is_set():
                break

        self.progress_queue.put(
            {
                "event": "batch_finished",
                "cancelled": self.cancel_event.is_set(),
                "processed_count": processed_count,
                "total_files": total_files,
            }
        )

    def poll_progress_queue(self) -> None:
        while True:
            try:
                event = self.progress_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_progress_event(event)
        self.after(100, self.poll_progress_queue)

    def _handle_progress_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("event")
        if event_type == "file_started":
            source_path = Path(event["source_path"])
            entry = self.queue_entries.get(source_path)
            if entry is not None:
                entry.status = "processing"
                self.selected_path = source_path
                self._refresh_queue_list()
                self._update_preview()
            self._refresh_progress_labels(0, len(self.queue_entries), 0, int(event.get("total_pages", 0)))
            return

        if event_type == "page_completed":
            page_index = int(event.get("page_index", 0))
            total_pages = int(event.get("total_pages", 0))
            self.file_label.configure(text=f"Page {page_index} of {total_pages}")
            self.file_progress.set(0 if total_pages == 0 else page_index / total_pages)
            return

        if event_type == "file_result":
            result = event["result"]
            entry = self.queue_entries.get(result.source_path)
            if entry is not None:
                entry.output_path = result.output_path
                if result.skipped:
                    entry.status = "skipped"
                elif result.success:
                    entry.status = "done"
                else:
                    entry.status = "error"
                    entry.error_message = result.message
            if result.success and result.output_path is not None:
                self.completed_output_dirs.add(result.output_path.parent)
                self.open_output_button.configure(state="normal")

            log_line = build_log_entry(
                source_path=result.source_path,
                output_path=result.output_path,
                dpi=int(event["dpi"]),
                image_format=event["image_format"],
                jpeg_quality=int(event["jpeg_quality"]),
                success=result.success or result.skipped,
                message=result.message if not result.skipped else "SKIPPED",
            )
            self.append_log(log_line)
            self.status_label.configure(text=result.message)
            self._refresh_queue_list()
            self._update_preview()
            self._refresh_progress_labels(
                int(event["processed_count"]),
                int(event["total_files"]),
                result.processed_pages if result.success else 0,
                result.total_pages,
            )
            return

        if event_type == "batch_finished":
            self._set_processing_state(False)
            processed_count = int(event.get("processed_count", 0))
            total_files = int(event.get("total_files", 0))
            self._refresh_progress_labels(processed_count, total_files, 0, 0)
            self.status_label.configure(text="Cancelled" if event.get("cancelled") else "Completed")
            self.append_log("[INFO] Batch processing cancelled." if event.get("cancelled") else "[INFO] Batch processing completed.")
            if self.exit_after_cancel:
                self.destroy()

    def open_output_folder(self) -> None:
        if not self.completed_output_dirs:
            return
        target = next(iter(self.completed_output_dirs))
        subprocess.Popen(["explorer", str(target)])

    def on_close(self) -> None:
        if self.processing_active:
            should_cancel = messagebox.askyesno("Exit", "Processing in progress. Cancel and exit?")
            if not should_cancel:
                return
            self.exit_after_cancel = True
            self.request_cancel()
            return
        self.destroy()
