import json
import math
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import customtkinter as ctk
import imageio_ffmpeg
import yt_dlp
from PIL import Image, ImageDraw, ImageOps
from tkinter import PhotoImage, filedialog, messagebox
from yt_dlp.utils import DownloadCancelled

APP_NAME = "Clipzy"
APP_VERSION = "2026.1.0"
APP_DIR = Path(__file__).parent.resolve()
DATA_DIR = Path(os.getenv("LOCALAPPDATA", str(APP_DIR))) / "Clipzy"
HISTORY_FILE = DATA_DIR / "history.json"
ICON_FILE = APP_DIR / "clipzy_icon.ico"
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "Clipzy"

LOGO_FILE_CANDIDATES = [
    "clipzy_logo_main.png",
    "clipzy_logo_alt.png",
    "clipzy_logo.png",
    "logo.png",
]
EXTERNAL_LOGO_PATHS = [
    Path(r"c:\Users\Lenovo\Downloads\Untitled - May 26, 2026 at 12.34.16.png"),
    Path(r"c:\Users\Lenovo\Downloads\ChatGPT Image May 26, 2026, 12_33_19 PM.png"),
]

PRESETS: dict[str, tuple[str, str]] = {
    "Best MP4": ("MP4 Video", "Best"),
    "MP3 Audio": ("MP3 Audio", "Best"),
    "720p Fast": ("MP4 Video", "720p"),
    "Small Size": ("MP4 Video", "480p"),
}


def short_error(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return exc.__class__.__name__
    text = text.splitlines()[0]
    return text[:260]


def resource_path(file_name: str) -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", APP_DIR)) / file_name
    return APP_DIR / file_name


class ClipzyApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1320x860")
        self.minsize(1180, 760)
        self.configure(fg_color="#060b14")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        self.ffmpeg_path: str | None = None
        self._ffmpeg_probe_started = False
        self.status_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

        self.download_queue: list[dict[str, Any]] = []
        self.download_history: list[dict[str, str]] = self._load_history()
        self.info_cache: dict[str, Any] | None = None

        self.worker_thread: threading.Thread | None = None
        self.analyze_thread: threading.Thread | None = None
        self.pause_event = threading.Event()
        self.cancel_current_event = threading.Event()
        self.cancel_all_event = threading.Event()
        self.processing = False
        self.analyzing = False
        self.queue_counter = 1
        self.last_downloaded_file: str = ""
        self.anim_tick = 0

        self.url_var = ctk.StringVar()
        self.output_var = ctk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        self.format_var = ctk.StringVar(value="MP4 Video")
        self.resolution_var = ctk.StringVar(value="Best")
        self.preset_var = ctk.StringVar(value="Best MP4")
        self.status_var = ctk.StringVar(value="Ready")
        self.progress_var = ctk.DoubleVar(value=0.0)
        self.live_var = ctk.StringVar(value="Idle")
        self.preview_title_var = ctk.StringVar(value="No video selected")
        self.preview_channel_var = ctk.StringVar(value="Channel: -")
        self.preview_duration_var = ctk.StringVar(value="Duration: -")
        self.preview_res_var = ctk.StringVar(value="Available: -")
        self.preview_size_var = ctk.StringVar(value="Approx size: -")
        self.history_choice_var = ctk.StringVar(value="No history yet")

        self.logo_path: Path | None = self._discover_logo_path()
        self.logo_header_img: ctk.CTkImage | None = None
        self.logo_preview_img: ctk.CTkImage | None = None
        self.tk_window_icon: PhotoImage | None = None

        self._ensure_icon_file()
        self._apply_window_icon()
        self._build_ui()
        self._refresh_queue_text()
        self._refresh_history()
        self._set_controls_enabled(True)

        # Defer heavier startup tasks until after first paint for faster app open.
        self.after(20, self._deferred_startup)

        self.after(130, self._drain_status_queue)
        self.after(130, self._animate_ui)

    def _deferred_startup(self) -> None:
        self._load_brand_images()
        self._start_ffmpeg_probe()

    def _start_ffmpeg_probe(self) -> None:
        if self._ffmpeg_probe_started:
            return
        self._ffmpeg_probe_started = True

        def worker() -> None:
            self.ffmpeg_path = self._resolve_ffmpeg_path()

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _resolve_ffmpeg_path() -> str | None:
        try:
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and Path(exe).exists():
                return exe
        except Exception:
            pass
        return shutil.which("ffmpeg")

    @staticmethod
    def _is_safe_url(url: str) -> bool:
        try:
            p = urlparse(url.strip())
            return p.scheme in {"http", "https"} and bool(p.netloc)
        except Exception:
            return False

    @staticmethod
    def _format_duration(seconds: Any) -> str:
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return "-"
        total = int(seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _fmt_mb(value_bytes: Any) -> str:
        if not isinstance(value_bytes, (int, float)) or value_bytes <= 0:
            return "-"
        return f"{value_bytes / (1024 * 1024):.1f} MB"

    def _discover_logo_path(self) -> Path | None:
        for name in LOGO_FILE_CANDIDATES:
            for base in (APP_DIR, resource_path(".")):
                p = (base / name).resolve()
                if p.exists():
                    return p

        for p in EXTERNAL_LOGO_PATHS:
            if p.exists():
                return p

        for p in APP_DIR.glob("*.png"):
            return p
        return None

    def _ensure_icon_file(self) -> None:
        if self.logo_path and self.logo_path.exists():
            try:
                regenerate = True
                if ICON_FILE.exists():
                    regenerate = ICON_FILE.stat().st_mtime < self.logo_path.stat().st_mtime
                if regenerate:
                    src = Image.open(self.logo_path).convert("RGBA")
                    sq = ImageOps.fit(src, (512, 512), method=Image.Resampling.LANCZOS)
                    sq.save(
                        ICON_FILE,
                        format="ICO",
                        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
                    )
            except Exception:
                pass

        if ICON_FILE.exists():
            return

        fallback = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(fallback)
        d.ellipse((12, 12, 244, 244), fill=(20, 122, 255, 255), outline=(39, 228, 255, 255), width=6)
        d.polygon([(178, 84), (240, 128), (178, 172)], fill=(255, 156, 41, 255))
        d.polygon([(108, 88), (186, 128), (108, 168)], fill=(255, 255, 255, 240))
        fallback.save(
            ICON_FILE,
            format="ICO",
            sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
        )

    def _apply_window_icon(self) -> None:
        try:
            if ICON_FILE.exists():
                self.iconbitmap(str(ICON_FILE))
        except Exception:
            pass

        try:
            if self.logo_path and self.logo_path.exists() and not ICON_FILE.exists():
                self.tk_window_icon = PhotoImage(file=str(self.logo_path))
                self.iconphoto(True, self.tk_window_icon)
        except Exception:
            pass

    def _load_brand_images(self) -> None:
        try:
            if self.logo_path and self.logo_path.exists():
                raw = Image.open(self.logo_path).convert("RGBA")
                head = ImageOps.contain(raw, (72, 72), method=Image.Resampling.LANCZOS)
                preview = ImageOps.contain(raw, (300, 220), method=Image.Resampling.LANCZOS)
                self.logo_header_img = ctk.CTkImage(light_image=head, dark_image=head, size=head.size)
                self.logo_preview_img = ctk.CTkImage(light_image=preview, dark_image=preview, size=preview.size)
                self.logo_badge.configure(text="", image=self.logo_header_img)
                self.preview_image.configure(text="", image=self.logo_preview_img)
                self.brand_subtitle.configure(text="Premium video downloader with queue intelligence")
            else:
                self.logo_badge.configure(text="C", image=None)
                self.preview_image.configure(text="No thumbnail", image=None)
        except Exception:
            self.logo_badge.configure(text="C", image=None)
            self.preview_image.configure(text="No thumbnail", image=None)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.header = ctk.CTkFrame(self, corner_radius=18, fg_color="#0c1c33", border_width=1, border_color="#17345f")
        self.header.grid(row=0, column=0, padx=18, pady=(14, 10), sticky="ew")
        self.header.grid_columnconfigure(1, weight=1)

        self.logo_badge = ctk.CTkLabel(
            self.header,
            width=78,
            height=78,
            text="",
            corner_radius=16,
            fg_color="#0d2746",
            font=ctk.CTkFont(size=34, weight="bold"),
        )
        self.logo_badge.grid(row=0, column=0, rowspan=2, padx=(16, 12), pady=12)

        self.brand_title = ctk.CTkLabel(
            self.header,
            text=APP_NAME,
            font=ctk.CTkFont(family="Segoe UI", size=44, weight="bold"),
            text_color="#6dd7ff",
        )
        self.brand_title.grid(row=0, column=1, padx=4, pady=(12, 0), sticky="w")

        self.brand_subtitle = ctk.CTkLabel(
            self.header,
            text="Fast. Safe. Beautiful.",
            font=ctk.CTkFont(size=15),
            text_color="#b8d6f8",
        )
        self.brand_subtitle.grid(row=1, column=1, padx=4, pady=(0, 14), sticky="w")

        self.live_label = ctk.CTkLabel(
            self.header,
            textvariable=self.live_var,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#71f3ff",
        )
        self.live_label.grid(row=0, column=2, padx=(0, 16), sticky="e")

        self.main = ctk.CTkFrame(self, corner_radius=16, fg_color="#0a1528", border_width=1, border_color="#123056")
        self.main.grid(row=1, column=0, padx=18, pady=(0, 10), sticky="nsew")
        self.main.grid_columnconfigure(0, weight=5)
        self.main.grid_columnconfigure(1, weight=7)
        self.main.grid_rowconfigure(0, weight=1)

        self.left = ctk.CTkScrollableFrame(
            self.main,
            corner_radius=16,
            fg_color="#111d36",
            scrollbar_button_color="#1a3a66",
            scrollbar_button_hover_color="#24508c",
        )
        self.left.grid(row=0, column=0, padx=(12, 8), pady=12, sticky="nsew")
        self.left.grid_columnconfigure(0, weight=1)

        self.right = ctk.CTkFrame(self.main, corner_radius=16, fg_color="#111d36")
        self.right.grid(row=0, column=1, padx=(8, 12), pady=12, sticky="nsew")
        self.right.grid_columnconfigure(0, weight=1)
        self.right.grid_rowconfigure(1, weight=1)

        self._build_left_controls()
        self._build_right_tabs()
        self._build_footer()

    def _build_left_controls(self) -> None:
        ctk.CTkLabel(self.left, text="Single URL", font=ctk.CTkFont(size=16, weight="bold"), text_color="#f0f7ff").grid(
            row=0, column=0, padx=14, pady=(14, 6), sticky="w"
        )

        self.url_entry = ctk.CTkEntry(
            self.left,
            textvariable=self.url_var,
            height=42,
            placeholder_text="Paste YouTube or supported video URL",
            border_color="#2c5687",
            fg_color="#0c1422",
            text_color="#dceeff",
        )
        self.url_entry.grid(row=1, column=0, padx=14, pady=(0, 8), sticky="ew")

        top_actions = ctk.CTkFrame(self.left, fg_color="transparent")
        top_actions.grid(row=2, column=0, padx=14, pady=(0, 10), sticky="ew")
        top_actions.grid_columnconfigure((0, 1, 2), weight=1)

        self.generate_btn = ctk.CTkButton(top_actions, text="Generate Preview", height=38, command=self._on_generate, fg_color="#0f7bd8", hover_color="#16a0ff")
        self.generate_btn.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.add_url_btn = ctk.CTkButton(top_actions, text="Add URL to Queue", height=38, command=self._add_current_url_to_queue, fg_color="#f1832e", hover_color="#ffa04d")
        self.add_url_btn.grid(row=0, column=1, padx=3, sticky="ew")
        self.paste_btn = ctk.CTkButton(top_actions, text="Paste", height=38, command=self._paste_clipboard, fg_color="#2e436a", hover_color="#3b5d95")
        self.paste_btn.grid(row=0, column=2, padx=(6, 0), sticky="ew")

        ctk.CTkLabel(self.left, text="Batch Links", font=ctk.CTkFont(size=16, weight="bold"), text_color="#f0f7ff").grid(
            row=3, column=0, padx=14, pady=(4, 6), sticky="w"
        )
        self.batch_box = ctk.CTkTextbox(self.left, height=130, fg_color="#0c1422", text_color="#dceeff", border_width=1, border_color="#2c5687")
        self.batch_box.grid(row=4, column=0, padx=14, pady=(0, 8), sticky="ew")
        self.batch_box.insert("1.0", "")

        batch_actions = ctk.CTkFrame(self.left, fg_color="transparent")
        batch_actions.grid(row=5, column=0, padx=14, pady=(0, 10), sticky="ew")
        batch_actions.grid_columnconfigure((0, 1), weight=1)
        self.add_batch_btn = ctk.CTkButton(batch_actions, text="Add Batch to Queue", height=36, command=self._add_batch_to_queue, fg_color="#0f7bd8", hover_color="#16a0ff")
        self.add_batch_btn.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.clear_batch_btn = ctk.CTkButton(batch_actions, text="Clear Batch", height=36, command=self._clear_batch_box, fg_color="#2e436a", hover_color="#3b5d95")
        self.clear_batch_btn.grid(row=0, column=1, padx=(6, 0), sticky="ew")

        ctk.CTkLabel(self.left, text="Output Folder", font=ctk.CTkFont(size=16, weight="bold"), text_color="#f0f7ff").grid(
            row=6, column=0, padx=14, pady=(4, 6), sticky="w"
        )
        self.output_entry = ctk.CTkEntry(self.left, textvariable=self.output_var, height=40, fg_color="#0c1422", border_color="#2c5687", text_color="#dceeff")
        self.output_entry.grid(row=7, column=0, padx=14, pady=(0, 8), sticky="ew")

        out_actions = ctk.CTkFrame(self.left, fg_color="transparent")
        out_actions.grid(row=8, column=0, padx=14, pady=(0, 10), sticky="ew")
        out_actions.grid_columnconfigure((0, 1), weight=1)
        self.choose_btn = ctk.CTkButton(out_actions, text="Choose Folder", height=34, command=self._choose_output, fg_color="#2e436a", hover_color="#3b5d95")
        self.choose_btn.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.open_output_btn = ctk.CTkButton(out_actions, text="Open Folder", height=34, command=self._open_output_folder, fg_color="#2e436a", hover_color="#3b5d95")
        self.open_output_btn.grid(row=0, column=1, padx=(6, 0), sticky="ew")

        ctk.CTkLabel(self.left, text="Smart Preset", font=ctk.CTkFont(size=16, weight="bold"), text_color="#f0f7ff").grid(
            row=9, column=0, padx=14, pady=(4, 6), sticky="w"
        )
        preset_row = ctk.CTkFrame(self.left, fg_color="transparent")
        preset_row.grid(row=10, column=0, padx=14, pady=(0, 8), sticky="ew")
        preset_row.grid_columnconfigure((0, 1), weight=1)
        self.preset_menu = ctk.CTkOptionMenu(preset_row, variable=self.preset_var, values=list(PRESETS.keys()), height=36, fg_color="#0f7bd8", button_color="#0f7bd8", button_hover_color="#16a0ff")
        self.preset_menu.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.apply_preset_btn = ctk.CTkButton(preset_row, text="Apply Preset", height=36, command=self._apply_preset, fg_color="#f1832e", hover_color="#ffa04d")
        self.apply_preset_btn.grid(row=0, column=1, padx=(6, 0), sticky="ew")

        format_row = ctk.CTkFrame(self.left, fg_color="transparent")
        format_row.grid(row=11, column=0, padx=14, pady=(0, 14), sticky="ew")
        format_row.grid_columnconfigure((0, 1), weight=1)
        self.format_menu = ctk.CTkOptionMenu(
            format_row,
            variable=self.format_var,
            values=["MP4 Video", "WEBM Video", "MP3 Audio", "M4A Audio"],
            height=36,
            fg_color="#0f7bd8",
            button_color="#0f7bd8",
            button_hover_color="#16a0ff",
        )
        self.format_menu.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.res_menu = ctk.CTkOptionMenu(
            format_row,
            variable=self.resolution_var,
            values=["Best"],
            height=36,
            fg_color="#0f7bd8",
            button_color="#0f7bd8",
            button_hover_color="#16a0ff",
        )
        self.res_menu.grid(row=0, column=1, padx=(6, 0), sticky="ew")

    def _build_right_tabs(self) -> None:
        ctk.CTkLabel(self.right, text="Workspace", font=ctk.CTkFont(size=18, weight="bold"), text_color="#f0f7ff").grid(
            row=0, column=0, padx=12, pady=(12, 8), sticky="w"
        )

        self.tabs = ctk.CTkTabview(self.right, corner_radius=14)
        self.tabs.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")
        self.preview_tab = self.tabs.add("Preview")
        self.queue_tab = self.tabs.add("Queue")
        self.history_tab = self.tabs.add("History")

        self._build_preview_tab()
        self._build_queue_tab()
        self._build_history_tab()

    def _build_preview_tab(self) -> None:
        self.preview_tab.grid_columnconfigure(1, weight=1)
        self.preview_tab.grid_rowconfigure(6, weight=1)
        image_wrap = ctk.CTkFrame(self.preview_tab, corner_radius=14, width=320, height=240, fg_color="#0c1422", border_width=1, border_color="#2c5687")
        image_wrap.grid(row=0, column=0, rowspan=6, padx=(12, 12), pady=12, sticky="ns")
        image_wrap.grid_propagate(False)
        self.preview_image = ctk.CTkLabel(image_wrap, text="No thumbnail", text_color="#9ab9dd")
        self.preview_image.pack(expand=True)

        self.preview_title_label = ctk.CTkLabel(
            self.preview_tab,
            textvariable=self.preview_title_var,
            anchor="w",
            justify="left",
            wraplength=470,
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#f5fbff",
        )
        self.preview_title_label.grid(row=0, column=1, padx=(0, 12), pady=(16, 8), sticky="ew")
        ctk.CTkLabel(self.preview_tab, textvariable=self.preview_channel_var, anchor="w", text_color="#d6e8ff").grid(row=1, column=1, padx=(0, 12), pady=4, sticky="ew")
        ctk.CTkLabel(self.preview_tab, textvariable=self.preview_duration_var, anchor="w", text_color="#d6e8ff").grid(row=2, column=1, padx=(0, 12), pady=4, sticky="ew")
        ctk.CTkLabel(self.preview_tab, textvariable=self.preview_size_var, anchor="w", text_color="#d6e8ff").grid(row=3, column=1, padx=(0, 12), pady=4, sticky="ew")
        ctk.CTkLabel(self.preview_tab, textvariable=self.preview_res_var, anchor="w", justify="left", wraplength=470, text_color="#d6e8ff").grid(
            row=4, column=1, padx=(0, 12), pady=4, sticky="ew"
        )
        self.preview_help = ctk.CTkLabel(
            self.preview_tab,
            text="Generate Preview to validate the link before queueing.",
            anchor="w",
            justify="left",
            wraplength=470,
            text_color="#91add3",
        )
        self.preview_help.grid(row=5, column=1, padx=(0, 12), pady=(2, 14), sticky="ew")

        self.download_preview_btn = ctk.CTkButton(
            self.preview_tab,
            text="Download This Preview",
            height=36,
            command=self._download_current_preview,
            fg_color="#16a34a",
            hover_color="#22c55e",
        )
        self.download_preview_btn.grid(row=6, column=1, padx=(0, 12), pady=(0, 14), sticky="ew")

    def _build_queue_tab(self) -> None:
        self.queue_tab.grid_columnconfigure(0, weight=1)
        self.queue_tab.grid_rowconfigure(1, weight=1)
        controls = ctk.CTkFrame(self.queue_tab, fg_color="transparent")
        controls.grid(row=0, column=0, padx=10, pady=(12, 8), sticky="ew")
        controls.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.start_btn = ctk.CTkButton(controls, text="Start Queue", height=34, command=self._start_queue, fg_color="#0f7bd8", hover_color="#16a0ff")
        self.start_btn.grid(row=0, column=0, padx=4, sticky="ew")
        self.pause_btn = ctk.CTkButton(controls, text="Pause", height=34, command=self._pause_or_resume, fg_color="#f1832e", hover_color="#ffa04d")
        self.pause_btn.grid(row=0, column=1, padx=4, sticky="ew")
        self.cancel_current_btn = ctk.CTkButton(controls, text="Cancel Current", height=34, command=self._cancel_current, fg_color="#aa3b47", hover_color="#c84e5a")
        self.cancel_current_btn.grid(row=0, column=2, padx=4, sticky="ew")
        self.cancel_all_btn = ctk.CTkButton(controls, text="Cancel All", height=34, command=self._cancel_all, fg_color="#7f2a33", hover_color="#a53745")
        self.cancel_all_btn.grid(row=0, column=3, padx=4, sticky="ew")

        self.queue_text = ctk.CTkTextbox(self.queue_tab, height=330, fg_color="#0c1422", border_width=1, border_color="#2c5687", text_color="#dceeff")
        self.queue_text.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="nsew")

        row2 = ctk.CTkFrame(self.queue_tab, fg_color="transparent")
        row2.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="ew")
        row2.grid_columnconfigure((0, 1), weight=1)
        self.clear_done_btn = ctk.CTkButton(row2, text="Clear Done/Failed", height=32, command=self._clear_done, fg_color="#2e436a", hover_color="#3b5d95")
        self.clear_done_btn.grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self.clear_queue_btn = ctk.CTkButton(row2, text="Clear Full Queue", height=32, command=self._clear_queue, fg_color="#2e436a", hover_color="#3b5d95")
        self.clear_queue_btn.grid(row=0, column=1, padx=(4, 0), sticky="ew")

    def _build_history_tab(self) -> None:
        self.history_tab.grid_columnconfigure(0, weight=1)
        self.history_tab.grid_rowconfigure(2, weight=1)
        self.history_menu = ctk.CTkOptionMenu(self.history_tab, variable=self.history_choice_var, values=["No history yet"], height=36, fg_color="#0f7bd8", button_color="#0f7bd8", button_hover_color="#16a0ff")
        self.history_menu.grid(row=0, column=0, padx=10, pady=(14, 8), sticky="ew")

        actions = ctk.CTkFrame(self.history_tab, fg_color="transparent")
        actions.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="ew")
        actions.grid_columnconfigure((0, 1, 2), weight=1)
        self.open_file_btn = ctk.CTkButton(actions, text="Open File", height=32, command=self._open_selected_history_file, fg_color="#2e436a", hover_color="#3b5d95")
        self.open_file_btn.grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self.open_folder_btn = ctk.CTkButton(actions, text="Open Folder", height=32, command=self._open_selected_history_folder, fg_color="#2e436a", hover_color="#3b5d95")
        self.open_folder_btn.grid(row=0, column=1, padx=4, sticky="ew")
        self.clear_history_btn = ctk.CTkButton(actions, text="Clear History", height=32, command=self._clear_history, fg_color="#7f2a33", hover_color="#a53745")
        self.clear_history_btn.grid(row=0, column=2, padx=(4, 0), sticky="ew")

        self.history_text = ctk.CTkTextbox(self.history_tab, height=330, fg_color="#0c1422", border_width=1, border_color="#2c5687", text_color="#dceeff")
        self.history_text.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="nsew")

    def _build_footer(self) -> None:
        self.footer = ctk.CTkFrame(self, corner_radius=14, fg_color="#0c1c33", border_width=1, border_color="#17345f")
        self.footer.grid(row=2, column=0, padx=18, pady=(0, 14), sticky="ew")
        self.footer.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(self.footer, variable=self.progress_var, progress_color="#1ca9ff")
        self.progress.grid(row=0, column=0, padx=12, pady=(10, 6), sticky="ew")
        self.progress.set(0.0)

        self.status_label = ctk.CTkLabel(self.footer, textvariable=self.status_var, anchor="w", wraplength=1260, text_color="#dceeff")
        self.status_label.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="ew")

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_var.get() or str(DEFAULT_OUTPUT_DIR))
        if selected:
            self.output_var.set(selected)

    def _open_output_folder(self) -> None:
        path = Path(self.output_var.get().strip() or str(DEFAULT_OUTPUT_DIR)).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{short_error(exc)}")

    def _paste_clipboard(self) -> None:
        try:
            txt = self.clipboard_get().strip()
        except Exception:
            txt = ""
        if txt:
            self.url_var.set(txt)
            self.status_var.set("Pasted URL from clipboard.")

    def _clear_batch_box(self) -> None:
        self.batch_box.delete("1.0", "end")

    def _apply_preset(self) -> None:
        preset = self.preset_var.get().strip()
        fmt, res = PRESETS.get(preset, ("MP4 Video", "Best"))
        self.format_var.set(fmt)
        vals = list(self.res_menu.cget("values"))
        if res not in vals:
            vals.append(res)
            self.res_menu.configure(values=vals)
        self.resolution_var.set(res)
        self.status_var.set(f"Preset applied: {preset}")

    def _extract_urls(self, text: str) -> list[str]:
        cleaned = text.replace("\r", "\n")
        urls = re.findall(r"https?://[^\s<>\"']+", cleaned)
        out: list[str] = []
        seen: set[str] = set()
        for u in urls:
            if self._is_safe_url(u) and u not in seen:
                out.append(u)
                seen.add(u)
        return out

    def _add_queue_items(self, urls: list[str]) -> None:
        if not urls:
            messagebox.showerror(APP_NAME, "No valid URL found. Please paste valid http/https links.")
            return
        fmt = self.format_var.get().strip()
        res = self.resolution_var.get().strip()
        preset = self.preset_var.get().strip()
        for url in urls:
            self.download_queue.append(
                {
                    "id": self.queue_counter,
                    "url": url,
                    "format": fmt,
                    "resolution": res,
                    "preset": preset,
                    "status": "Queued",
                    "title": "Pending metadata",
                    "message": "",
                    "saved_file": "",
                }
            )
            self.queue_counter += 1
        self._refresh_queue_text()
        self.status_var.set(f"Added {len(urls)} item(s) to queue.")

    def _add_current_url_to_queue(self) -> None:
        url = self.url_var.get().strip()
        self._add_queue_items([url] if url else [])

    def _download_current_preview(self) -> None:
        if self.processing:
            messagebox.showerror(APP_NAME, "A queue is already running. Wait for it to finish or cancel it first.")
            return
        if self.analyzing:
            messagebox.showerror(APP_NAME, "Preview is still loading. Please wait a moment.")
            return
        if not self.info_cache:
            messagebox.showerror(APP_NAME, "No preview loaded. Click Generate Preview first.")
            return

        preview_url = str(self.info_cache.get("url") or "").strip()
        if not preview_url:
            messagebox.showerror(APP_NAME, "Preview URL is missing. Please generate preview again.")
            return

        self._add_queue_items([preview_url])
        self.tabs.set("Queue")
        self._start_queue()

    def _add_batch_to_queue(self) -> None:
        text = self.batch_box.get("1.0", "end").strip()
        if text == "Paste one URL per line":
            text = ""
        self._add_queue_items(self._extract_urls(text))

    def _refresh_queue_text(self) -> None:
        self.queue_text.configure(state="normal")
        self.queue_text.delete("1.0", "end")
        if not self.download_queue:
            self.queue_text.insert("end", "Queue is empty. Add URL(s) to begin.\n")
            self.queue_text.configure(state="disabled")
            return
        for item in self.download_queue:
            mark = ">> " if item["status"] == "Downloading" else "   "
            short_url = item["url"] if len(item["url"]) < 92 else item["url"][:89] + "..."
            self.queue_text.insert(
                "end",
                f"{mark}#{item['id']:03d} | {item['status']:<10} | {item['preset']:<10} | {item['resolution']:<6} | {short_url}\n",
            )
            if item.get("message"):
                self.queue_text.insert("end", f"      note: {item['message']}\n")
        self.queue_text.configure(state="disabled")

    def _clear_done(self) -> None:
        self.download_queue = [q for q in self.download_queue if q["status"] in {"Queued", "Downloading"}]
        self._refresh_queue_text()

    def _clear_queue(self) -> None:
        if self.processing:
            messagebox.showerror(APP_NAME, "Queue is running. Stop it first.")
            return
        self.download_queue = []
        self._refresh_queue_text()
        self.status_var.set("Queue cleared.")

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (
            self.generate_btn,
            self.add_url_btn,
            self.add_batch_btn,
            self.clear_batch_btn,
            self.start_btn,
            self.paste_btn,
            self.choose_btn,
            self.open_output_btn,
            self.apply_preset_btn,
            self.clear_done_btn,
            self.clear_queue_btn,
            self.download_preview_btn,
        ):
            widget.configure(state=state)
        action_state = "normal" if self.processing else "disabled"
        self.pause_btn.configure(state=action_state)
        self.cancel_current_btn.configure(state=action_state)
        self.cancel_all_btn.configure(state=action_state)

    def _cancel_current(self) -> None:
        if not self.processing:
            return
        self.cancel_current_event.set()
        self.status_var.set("Cancel requested for current item...")

    def _cancel_all(self) -> None:
        if not self.processing:
            return
        self.cancel_all_event.set()
        self.cancel_current_event.set()
        self.status_var.set("Cancel requested for full queue...")

    def _pause_or_resume(self) -> None:
        if not self.processing:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text="Pause")
            self.status_var.set("Resuming...")
        else:
            self.pause_event.set()
            self.pause_btn.configure(text="Resume")
            self.status_var.set("Pause requested...")

    def _on_generate(self) -> None:
        if self.analyzing:
            return
        url = self.url_var.get().strip()
        if not self._is_safe_url(url):
            messagebox.showerror(APP_NAME, "Please enter a valid http/https URL.")
            return

        self.analyzing = True
        self.progress.set(0)
        self.status_var.set("Analyzing video URL...")
        self.preview_title_var.set("Loading metadata...")
        self.preview_channel_var.set("Channel: -")
        self.preview_duration_var.set("Duration: -")
        self.preview_res_var.set("Available: -")
        self.preview_size_var.set("Approx size: -")

        self.analyze_thread = threading.Thread(target=self._analyze_worker, args=(url,), daemon=True)
        self.analyze_thread.start()

    def _analyze_worker(self, url: str) -> None:
        try:
            opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                raise RuntimeError("Unable to fetch video metadata.")
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            formats = info.get("formats", []) or []
            heights = sorted({f.get("height") for f in formats if f.get("height")}, reverse=True)
            approximate = max(
                [int(f.get("filesize") or f.get("filesize_approx") or 0) for f in formats if isinstance(f, dict)] or [0]
            )

            video_ext = {str(f.get("ext")).lower() for f in formats if f.get("vcodec") not in (None, "none") and f.get("ext")}
            audio_ext = {
                str(f.get("ext")).lower()
                for f in formats
                if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none") and f.get("ext")
            }

            format_choices = []
            if "mp4" in video_ext:
                format_choices.append("MP4 Video")
            if "webm" in video_ext:
                format_choices.append("WEBM Video")
            if not format_choices:
                format_choices.append("Best Video")
            format_choices.append("MP3 Audio")
            if "m4a" in audio_ext:
                format_choices.append("M4A Audio")

            resolution_choices = ["Best"] + [f"{h}p" for h in heights]
            if len(resolution_choices) == 1:
                resolution_choices.extend(["1080p", "720p", "480p"])

            thumb = self._download_thumbnail_bytes(info)
            self.info_cache = {
                "url": url,
                "title": info.get("title") or "Untitled",
                "channel": info.get("uploader") or info.get("channel") or "-",
                "duration": info.get("duration"),
                "heights": heights,
                "approx_size": approximate,
                "format_choices": format_choices,
                "resolution_choices": resolution_choices,
            }
            self.status_queue.put(("analyze_ok", thumb))
        except Exception as exc:
            self.status_queue.put(("error", f"Generate failed: {short_error(exc)}"))

    def _download_thumbnail_bytes(self, info: dict[str, Any]) -> bytes | None:
        thumb = info.get("thumbnail")
        if not thumb:
            thumbs = info.get("thumbnails") or []
            if thumbs:
                thumb = thumbs[-1].get("url")
        if not thumb:
            return None
        try:
            req = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.read()
        except Exception:
            return None

    def _update_preview_image(self, raw_bytes: bytes | None) -> None:
        if not raw_bytes:
            self._load_brand_images()
            return
        try:
            img = Image.open(BytesIO(raw_bytes)).convert("RGBA")
            img = ImageOps.fit(img, (300, 220), method=Image.Resampling.LANCZOS)
            self.logo_preview_img = ctk.CTkImage(light_image=img, dark_image=img, size=(300, 220))
            self.preview_image.configure(image=self.logo_preview_img, text="")
        except Exception:
            self._load_brand_images()

    def _format_selector(self, selected_format: str, selected_res: str) -> tuple[str, str | None, list[dict[str, Any]] | None]:
        res_limit = None
        if selected_res.lower().endswith("p") and selected_res[:-1].isdigit():
            res_limit = int(selected_res[:-1])

        if selected_format == "MP3 Audio":
            return (
                "bestaudio/best",
                None,
                [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            )

        if selected_format == "M4A Audio":
            return "bestaudio[ext=m4a]/bestaudio/best", None, None

        ext = None
        if selected_format == "MP4 Video":
            ext = "mp4"
        elif selected_format == "WEBM Video":
            ext = "webm"

        if ext is None:
            if res_limit:
                return f"bestvideo[height<={res_limit}]+bestaudio/best[height<={res_limit}]/best", None, None
            return "bestvideo+bestaudio/best", None, None

        if res_limit:
            fmt = (
                f"bestvideo[ext={ext}][height<={res_limit}]+bestaudio/"
                f"best[ext={ext}][height<={res_limit}]/"
                f"bestvideo[height<={res_limit}]+bestaudio/best[height<={res_limit}]"
            )
        else:
            fmt = f"bestvideo[ext={ext}]+bestaudio/best[ext={ext}]/bestvideo+bestaudio/best"
        return fmt, ext, None

    def _start_queue(self) -> None:
        if self.processing:
            return
        if not any(item["status"] == "Queued" for item in self.download_queue):
            messagebox.showerror(APP_NAME, "Queue is empty. Add at least one item first.")
            return

        output_dir = Path(self.output_var.get().strip() or str(DEFAULT_OUTPUT_DIR)).expanduser()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Cannot use output folder:\n{short_error(exc)}")
            return

        self.processing = True
        self.cancel_current_event.clear()
        self.cancel_all_event.clear()
        self.pause_event.clear()
        self.pause_btn.configure(text="Pause")
        if self.ffmpeg_path is None:
            # If background probe has not finished yet, resolve once here.
            self.ffmpeg_path = self._resolve_ffmpeg_path()
        self.progress.set(0.0)
        self._set_controls_enabled(False)
        self.status_var.set("Queue started.")
        self.worker_thread = threading.Thread(target=self._queue_worker, args=(output_dir,), daemon=True)
        self.worker_thread.start()

    def _queue_worker(self, output_dir: Path) -> None:
        for item in self.download_queue:
            if self.cancel_all_event.is_set():
                break
            if item["status"] != "Queued":
                continue

            item["status"] = "Downloading"
            item["message"] = ""
            self.status_queue.put(("queue_refresh", None))

            ok, msg, saved_file, title = self._download_single(item, output_dir)
            if title:
                item["title"] = title
            item["saved_file"] = saved_file
            item["message"] = msg

            if "cancel" in msg.lower():
                item["status"] = "Cancelled"
            else:
                item["status"] = "Done" if ok else "Failed"

            self.status_queue.put(("queue_refresh", None))

            if ok and saved_file:
                self._append_history(
                    {
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "title": item.get("title") or "Untitled",
                        "url": item["url"],
                        "file": saved_file,
                        "folder": str(Path(saved_file).parent),
                    }
                )
                self.status_queue.put(("history_refresh", None))

            self.cancel_current_event.clear()

        self.processing = False
        self.pause_event.clear()
        self.status_queue.put(("queue_finished", None))

    def _download_single(self, item: dict[str, Any], output_dir: Path) -> tuple[bool, str, str, str]:
        fmt, merge_ext, postprocessors = self._format_selector(str(item["format"]), str(item["resolution"]))
        title_holder = ""

        def hook(data: dict[str, Any]) -> None:
            status = data.get("status")
            if self.cancel_current_event.is_set() or self.cancel_all_event.is_set():
                raise DownloadCancelled("Cancelled by user")
            while self.pause_event.is_set():
                time.sleep(0.2)
                if self.cancel_current_event.is_set() or self.cancel_all_event.is_set():
                    raise DownloadCancelled("Cancelled by user")

            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                downloaded = data.get("downloaded_bytes") or 0
                pct = int(max(0.0, min(1.0, downloaded / total)) * 100) if total else 0
                speed = data.get("_speed_str", "")
                eta = data.get("_eta_str", "")
                msg = f"Downloading #{item['id']:03d}... {pct}%"
                if speed:
                    msg += f" | {speed}"
                if eta:
                    msg += f" | ETA {eta}"
                self.status_queue.put(("status", msg))
                if total:
                    self.status_queue.put(("progress", max(0.0, min(1.0, downloaded / total))))
            elif status == "finished":
                self.last_downloaded_file = str(data.get("filename") or "")
                self.status_queue.put(("status", f"Finalizing #{item['id']:03d}..."))
                self.status_queue.put(("progress", 0.98))

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": fmt,
            "outtmpl": str(output_dir / "%(title).180B [%(id)s].%(ext)s"),
            "concurrent_fragment_downloads": 8,
            "overwrites": False,
            "progress_hooks": [hook],
        }
        if self.ffmpeg_path:
            opts["ffmpeg_location"] = self.ffmpeg_path
        if merge_ext:
            opts["merge_output_format"] = merge_ext
        if postprocessors:
            opts["postprocessors"] = postprocessors

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(item["url"], download=True)
                if isinstance(info, dict):
                    title_holder = str(info.get("title") or "")
                final = self._extract_saved_path(info)

            if not final and self.last_downloaded_file:
                final = self.last_downloaded_file
            self.status_queue.put(("progress", 1.0))
            return True, "Completed", final or "", title_holder
        except DownloadCancelled:
            return False, "Cancelled by user", "", title_holder
        except Exception as exc:
            return False, short_error(exc), "", title_holder

    @staticmethod
    def _extract_saved_path(info: Any) -> str:
        if not isinstance(info, dict):
            return ""
        req = info.get("requested_downloads")
        if isinstance(req, list):
            for i in req:
                if isinstance(i, dict):
                    p = i.get("filepath")
                    if isinstance(p, str) and Path(p).exists():
                        return p
        p = info.get("filepath") or info.get("_filename")
        if isinstance(p, str) and Path(p).exists():
            return p
        return ""

    def _append_history(self, row: dict[str, str]) -> None:
        self.download_history.insert(0, row)
        self.download_history = self.download_history[:120]
        self._save_history()

    def _load_history(self) -> list[dict[str, str]]:
        if not HISTORY_FILE.exists():
            return []
        try:
            raw = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [x for x in raw if isinstance(x, dict)]
        except Exception:
            pass
        return []

    def _save_history(self) -> None:
        try:
            HISTORY_FILE.write_text(json.dumps(self.download_history, ensure_ascii=True, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _refresh_history(self) -> None:
        if not self.download_history:
            self.history_menu.configure(values=["No history yet"])
            self.history_choice_var.set("No history yet")
            self.history_text.configure(state="normal")
            self.history_text.delete("1.0", "end")
            self.history_text.insert("end", "No downloads yet.\n")
            self.history_text.configure(state="disabled")
            return

        choices: list[str] = []
        for i, item in enumerate(self.download_history[:70]):
            title = (item.get("title") or "Untitled").strip()
            if len(title) > 48:
                title = title[:45] + "..."
            choices.append(f"{i + 1:02d}. {item.get('timestamp', '-')} | {title}")
        self.history_menu.configure(values=choices)
        if self.history_choice_var.get() not in choices:
            self.history_choice_var.set(choices[0])

        self.history_text.configure(state="normal")
        self.history_text.delete("1.0", "end")
        for i, item in enumerate(self.download_history[:50], start=1):
            self.history_text.insert(
                "end",
                f"{i:02d}. {item.get('timestamp', '-')}\n"
                f"    title: {item.get('title', 'Untitled')}\n"
                f"    url: {item.get('url', '')}\n"
                f"    file: {item.get('file', '')}\n\n",
            )
        self.history_text.configure(state="disabled")

    def _selected_history(self) -> dict[str, str] | None:
        if not self.download_history:
            return None
        selected = self.history_choice_var.get()
        if selected == "No history yet":
            return None
        try:
            idx = int(selected.split(".", 1)[0]) - 1
            if 0 <= idx < len(self.download_history):
                return self.download_history[idx]
        except Exception:
            pass
        return None

    def _open_selected_history_file(self) -> None:
        item = self._selected_history()
        if not item:
            return
        fp = item.get("file") or ""
        if not fp or not Path(fp).exists():
            messagebox.showerror(APP_NAME, "Selected file does not exist.")
            return
        try:
            os.startfile(fp)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open file:\n{short_error(exc)}")

    def _open_selected_history_folder(self) -> None:
        item = self._selected_history()
        if not item:
            return
        folder = item.get("folder") or ""
        if not folder or not Path(folder).exists():
            messagebox.showerror(APP_NAME, "Selected folder does not exist.")
            return
        try:
            os.startfile(folder)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{short_error(exc)}")

    def _clear_history(self) -> None:
        self.download_history = []
        self._save_history()
        self._refresh_history()
        self.status_var.set("History cleared.")

    def _drain_status_queue(self) -> None:
        try:
            while True:
                kind, payload = self.status_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    try:
                        self.progress.set(float(payload))
                    except Exception:
                        pass
                elif kind == "queue_refresh":
                    self._refresh_queue_text()
                elif kind == "history_refresh":
                    self._refresh_history()
                elif kind == "analyze_ok":
                    self.analyzing = False
                    if self.info_cache:
                        self.preview_title_var.set(self.info_cache.get("title", "Untitled"))
                        self.preview_channel_var.set(f"Channel: {self.info_cache.get('channel', '-')}")
                        self.preview_duration_var.set(f"Duration: {self._format_duration(self.info_cache.get('duration'))}")
                        self.preview_size_var.set(f"Approx size: {self._fmt_mb(self.info_cache.get('approx_size'))}")
                        heights = self.info_cache.get("heights") or []
                        self.preview_res_var.set(
                            "Available: " + (", ".join(f"{h}p" for h in heights[:10]) if heights else "-")
                        )
                        self.format_menu.configure(values=self.info_cache.get("format_choices", ["MP4 Video"]))
                        self.format_var.set(self.info_cache.get("format_choices", ["MP4 Video"])[0])
                        self.res_menu.configure(values=self.info_cache.get("resolution_choices", ["Best"]))
                        self.resolution_var.set(self.info_cache.get("resolution_choices", ["Best"])[0])
                        self._update_preview_image(payload if isinstance(payload, (bytes, bytearray)) else None)
                        self.status_var.set("Preview generated successfully.")
                elif kind == "queue_finished":
                    self.pause_btn.configure(text="Pause")
                    self._set_controls_enabled(True)
                    self._refresh_queue_text()
                    self._refresh_history()
                    self.status_var.set("Queue finished.")
                elif kind == "error":
                    self.analyzing = False
                    self.processing = False
                    self._set_controls_enabled(True)
                    self.progress.set(0.0)
                    self.pause_btn.configure(text="Pause")
                    self.status_var.set(str(payload))
                    messagebox.showerror(APP_NAME, str(payload))
        except queue.Empty:
            pass
        finally:
            self.after(130, self._drain_status_queue)

    def _animate_ui(self) -> None:
        self.anim_tick += 1
        phase = (math.sin(self.anim_tick / 10.0) + 1.0) / 2.0
        r = int(90 + 80 * phase)
        g = int(195 + 35 * (1 - phase))
        b = int(255 - 30 * phase)
        self.brand_title.configure(text_color=f"#{r:02x}{g:02x}{b:02x}")

        if self.processing:
            dots = "." * ((self.anim_tick // 4) % 4)
            if self.pause_event.is_set():
                self.live_var.set("Paused")
            else:
                self.live_var.set(f"Downloading{dots}")
        elif self.analyzing:
            dots = "." * ((self.anim_tick // 4) % 4)
            self.live_var.set(f"Analyzing{dots}")
        else:
            self.live_var.set("Idle")

        self.after(130, self._animate_ui)


def main() -> None:
    app = ClipzyApp()
    app.status_var.set(
        "Use Clipzy only for content you own or have permission to download. "
        "No account credentials are collected by this app."
    )
    try:
        app.mainloop()
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
