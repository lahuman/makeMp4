"""Tkinter timeline editor for one song, image cues, and independent text clips."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
import wave

from PIL import ImageTk

import editor_core as core
from studio_theme import install_studio_controls


UI_COLORS = {
    "window": "#F2F3F7", "surface": "#FFFFFF", "surface_alt": "#F2F3F7",
    "border": "#E1E4EC", "text": "#202434", "muted": "#656D80",
    "primary": "#5856D6", "primary_hover": "#4745BD", "primary_tint": "#ECEBFF",
    "preview": "#F2F3F7", "timeline": "#FFFFFF", "track_header": "#F8F9FC",
    "ruler": "#F2F3F7", "track_text": "#FFFFFF", "track_image": "#F8F9FC",
    "track_audio": "#FFFFFF", "timeline_text": "#202434", "timeline_muted": "#656D80",
    "grid": "#E9ECF2", "track_divider": "#E1E4EC", "outside": "#F2F3F7",
    "gap_image": "#FFF4EB", "gap_audio": "#F3F4F6", "clip_border": "#BFC8D3",
    "image_clip": "#E3EDF8", "image_clip_top": "#648CB5",
    "text_clip": "#EEE8F8", "text_clip_top": "#9A80BA",
    "audio_clip": "#E4F2EB", "audio_clip_top": "#549887", "waveform": "#247D6C",
    "video_clip": "#EDEBFB", "video_clip_top": "#7661BD",
    "playhead": "#5856D6", "dark_text": "#E7EAF1", "dark_muted": "#AEB7C7",
    "success": "#10B981", "warning": "#F59E0B",
    "error": "#EF4444",
}


def clock(seconds):
    minutes, remaining = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):02d}:{remaining:06.3f}"


def frame_of(seconds, duration):
    return max(0, min(core.total_frames(duration) - 1, round(float(seconds) * core.FPS)))


class EditorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("선율담")
        self._default_window_geometry()
        self.project = core.fresh()
        self.project_file = None
        self.dirty = False
        self.undo = []
        self.redo = []
        self.selection = None
        self.duration = 0.0
        self.position = 0.0
        self.bins = []
        self.pcm = None
        self.audio_cancel = threading.Event()
        self.export_cancel = threading.Event()
        self.events = queue.Queue()
        self.exporting = False
        self.last_output = None
        self.export_dialog = None
        self.play_thread = None
        self.audio_thread = None
        self.export_thread = None
        self.play_stop = threading.Event()
        self.playing = False
        self.play_frames = 0
        self.play_epoch = 0
        self.play_latency = 0.0
        self.zoom = 55
        self.snap = tk.BooleanVar(value=True)
        self.project_search = tk.StringVar()
        self.library_view = tk.StringVar(value="썸네일")
        self.library_filter = tk.StringVar(value="전체")
        self.property_groups = {"text": True, "time": True, "position": False, "appearance": False,
                                "image_advanced": False}
        self.thumbnail_cache = {}
        self.timeline_thumbnail_cache = {}
        self.library_markers = {}
        self._library_signature = None
        settings_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        self.ui_settings_file = settings_root / "Seonyuldam" / "ui.json"
        self.legacy_ui_settings_file = settings_root / "MusicToVideo" / "ui.json"
        self._panes_initialized = False
        self.drag = None
        self.preview_drag = None
        self.thumb_refs = []
        self.library_selection = None
        self.library_drag_id = None
        self.audio_generation = 0
        self.scene_cache_key = None
        self.preview_ref = None
        self.preview_rect = (0, 0, 1, 1)
        self.warned_fonts = set()
        self.status = tk.StringVar(value="음악 파일을 선택하세요.")
        self.time_label = tk.StringVar(value="00:00.000 / 00:00.000")
        self.progress = tk.IntVar()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-z>", lambda e: self._shortcut(e, self.undo_action))
        self.root.bind("<Control-y>", lambda e: self._shortcut(e, self.redo_action))
        self.root.bind("<Delete>", lambda e: self._shortcut(e, self.delete_selected))
        self.root.bind("<Control-d>", lambda e: self._shortcut(e, self.duplicate_selected))
        self.root.bind("<Control-s>", lambda e: self._shortcut(e, self.save_project))
        self.root.bind("<Control-o>", lambda e: self._shortcut(e, self.open_project))
        self.root.bind("<Control-n>", lambda e: self._shortcut(e, self.new_project))
        self.root.bind("<space>", lambda e: self._shortcut(e, self.toggle_play))
        self.root.bind("<Left>", lambda e: self._shortcut(e, lambda: self.seek(self.position-1/core.FPS)))
        self.root.bind("<Right>", lambda e: self._shortcut(e, lambda: self.seek(self.position+1/core.FPS)))
        self.root.bind("<Home>", lambda e: self._shortcut(e, lambda: self.seek(0)))
        self.root.bind("<plus>", lambda e: self._shortcut(e, lambda: self.set_zoom(self.zoom*1.5)))
        self.root.bind("<minus>", lambda e: self._shortcut(e, lambda: self.set_zoom(self.zoom/1.5)))
        self.root.bind("s", lambda e: self._shortcut(e, lambda: self.snap.set(not self.snap.get())))
        self.root.bind("<Escape>", self._escape)
        self.root.bind_all("<ButtonRelease-1>", self._library_drop_anywhere, add="+")
        self.root.bind_all("<MouseWheel>", self._panel_wheel, add="+")
        self.project_search.trace_add("write", lambda *_: self._refresh_library())
        self.library_view.trace_add("write", lambda *_: self._refresh_library())
        self.library_filter.trace_add("write", lambda *_: self._refresh_library())
        self._restore_ui_settings()
        self.root.after_idle(self._set_initial_panes)
        self.root.after(80, self._poll)
        self.root.after(80, self._tick)

    def _shortcut(self, event, action):
        if isinstance(event.widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox, ttk.Spinbox)):
            return None
        action()
        return "break"

    def _escape(self, event):
        if self.drag and self.drag[0] != "seek":
            if self.drag[5] and self.undo:
                self.project = self.undo.pop()
                self.dirty = self.drag[6]
            self.drag = None
            self._refresh()
            return "break"
        return None

    def _default_window_geometry(self):
        # Leave room for the taskbar and window decorations on smaller displays.
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1420, max(1, screen_width - 80))
        height = min(850, max(1, screen_height - 120))
        self.root.minsize(min(1040, width), min(680, height))
        top = max(0, (screen_height - height - 120) // 2)
        self.root.geometry(f"{width}x{height}+{(screen_width-width)//2}+{top}")

    def _restore_ui_settings(self):
        self.saved_panes = None
        self.last_export_folder = None
        try:
            settings_file = self.ui_settings_file if self.ui_settings_file.is_file() else self.legacy_ui_settings_file
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            folder = data.get("export_folder")
            if isinstance(folder, str) and folder:
                self.last_export_folder = folder
            self.saved_panes = data.get("panes", None)
            geometry = data.get("geometry", "")
            size = re.fullmatch(r"(\d+)x(\d+)(?:[+-]\d+[+-]\d+)?", geometry) if isinstance(geometry, str) else None
            if size:
                width, height = map(int, size.groups())
                if (0 < width <= self.root.winfo_screenwidth() - 80
                        and 0 < height <= self.root.winfo_screenheight() - 120):
                    self.root.geometry(geometry)
                else:
                    self.saved_panes = None
        except (OSError, ValueError, tk.TclError):
            self.saved_panes = None

    def _save_ui_settings(self):
        if not self._panes_initialized:
            return
        try:
            self.ui_settings_file.parent.mkdir(parents=True, exist_ok=True)
            positions = [self.body.sashpos(0), self.body.sashpos(1), self.workspace.sashpos(0)] if len(self.body.panes()) == 3 else None
            self.ui_settings_file.write_text(json.dumps({"geometry": self.root.geometry(), "panes": positions,
                                                        "export_folder": self.last_export_folder}), encoding="utf-8")
        except OSError:
            pass

    def _set_initial_panes(self):
        if not self.root.winfo_exists():
            return
        # A frozen application's first map can arrive after startup timers fire.
        # Wait for mapping, then flush geometry work before measuring the panes.
        if not self.root.winfo_ismapped():
            self.root.after(50, self._set_initial_panes)
            return
        self.root.update_idletasks()
        width = self.body.winfo_width()
        height = self.workspace.winfo_height()
        if width <= 1 or height <= 1:
            self.root.after(50, self._set_initial_panes)
            return
        positions = self.saved_panes
        if not (isinstance(positions, list) and len(positions) == 3
                and all(type(value) is int for value in positions)
                and 140 <= positions[0] <= positions[1] - 320
                and positions[1] <= width - 180
                and 160 <= positions[2] <= height - 140):
            positions = [min(270, max(210, int(width*.19))),
                         max(650, width-300), int(height*.64)]
        self.body.sashpos(0, positions[0])
        self.body.sashpos(1, positions[1])
        self.workspace.sashpos(0, positions[2])
        self._panes_initialized = True
        self.root.update_idletasks()

    def tool(self, name):
        from seonyuldam import bundled_tool
        result = bundled_tool(name)
        if not result:
            raise core.EditorError(f"{name}를 찾지 못했습니다. 배포 폴더의 ffmpeg/bin을 확인하세요.")
        return result

    def _build(self):
        self._apply_theme()
        top = ttk.Frame(self.root, style="Header.TFrame", padding=(18, 12)); top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        brand = ttk.Frame(top, style="Header.TFrame"); brand.grid(row=0, column=0, sticky="w")
        ttk.Label(brand, text="♫", style="BrandMark.TLabel").pack(side="left", padx=(0, 9))
        ttk.Label(brand, text="선율담", style="Header.TLabel").pack(side="left")
        menus = ttk.Frame(brand, padding=(14, 0)); menus.pack(side="left")
        self._build_menu(menus)
        project_heading = ttk.Frame(top, style="Header.TFrame")
        project_heading.grid(row=0, column=1, padx=20, sticky="e")
        self.project_name_label = ttk.Label(project_heading, text="새 프로젝트", style="Project.TLabel")
        self.project_name_label.pack(side="left")
        self.project_state_label = ttk.Label(project_heading, text="저장 전", style="HeaderMuted.TLabel")
        self.project_state_label.pack(side="left", padx=(12, 0))
        ttk.Button(top, text="MP4 내보내기", style="Primary.TButton", command=self.start_export).grid(row=0, column=2)
        ttk.Separator(self.root).pack(fill="x")
        self.workspace = ttk.PanedWindow(self.root, orient="vertical")
        self.workspace.pack(fill="both", expand=True)
        body = ttk.PanedWindow(self.workspace, orient="horizontal")
        self.workspace.add(body, weight=3)
        self.body = body
        self.left = ttk.Frame(body, width=250, style="Panel.TFrame")
        self.left.pack_propagate(False)
        body.add(self.left, weight=1)
        left = self.left
        self._panel_heading(left, "미디어")
        tools = ttk.Frame(left, style="Panel.TFrame", padding=(12, 10)); tools.pack(fill="x")
        self.import_button = ttk.Menubutton(tools, text="＋  미디어 가져오기", direction="below")
        self.import_button.pack(fill="x")
        self.import_menu = tk.Menu(self.import_button, tearoff=False, bg=UI_COLORS["surface"],
                                   fg=UI_COLORS["text"], activebackground=UI_COLORS["primary_tint"])
        self.import_menu.add_command(label="음악 가져오기", command=self.choose_audio)
        self.import_menu.add_command(label="이미지 가져오기", command=self.add_assets)
        if hasattr(self, "choose_video"):
            self.import_menu.add_command(label="영상 가져오기", command=self.choose_video)
        self.import_button.configure(menu=self.import_menu)
        self.audio_info = ttk.Label(left, text="음악을 가져오세요", style="Muted.TLabel", padding=(10, 8))
        search = ttk.Frame(left, style="Panel.TFrame", padding=(12, 2)); search.pack(fill="x")
        ttk.Label(search, text="미디어 검색", style="Muted.TLabel").pack(anchor="w", pady=(0, 5))
        search_row = ttk.Frame(search, style="Panel.TFrame"); search_row.pack(fill="x")
        ttk.Entry(search_row, textvariable=self.project_search).pack(side="left", fill="x", expand=True)
        ttk.Combobox(search_row, textvariable=self.library_view, state="readonly", width=7,
                     values=("썸네일", "목록")).pack(side="right", padx=(5, 0))
        filters = ttk.Frame(left, padding=(12, 10)); filters.pack(fill="x")
        for name in ("전체", "영상", "사진"):
            ttk.Radiobutton(filters, text=name, value=name, variable=self.library_filter,
                            style="Chip.TRadiobutton").pack(side="left", padx=(0, 4))
        library_wrap = ttk.Frame(left, style="Panel.TFrame"); library_wrap.pack(fill="both", expand=True)
        self.library_canvas = tk.Canvas(library_wrap, highlightthickness=0, bg=UI_COLORS["surface"])
        self.library_canvas.pack(side="left", fill="both", expand=True)
        library_scroll = ttk.Scrollbar(library_wrap, orient="vertical", command=self.library_canvas.yview)
        library_scroll.pack(side="right", fill="y")
        self.library_canvas.configure(yscrollcommand=library_scroll.set)
        self.library_frame = ttk.Frame(self.library_canvas, style="Panel.TFrame")
        self.library_window = self.library_canvas.create_window((0, 0), window=self.library_frame, anchor="nw")
        self.library_frame.bind("<Configure>", lambda e: self.library_canvas.configure(scrollregion=self.library_canvas.bbox("all")))
        self.library_canvas.bind("<Configure>", lambda e: self.library_canvas.itemconfigure(self.library_window, width=e.width))
        ttk.Separator(left).pack(fill="x", padx=14)
        self.audio_info.pack(fill="x")
        ttk.Button(left, text="선택 미디어를 재생 위치에 추가", style="Tint.TButton",
                   command=self.add_selected_asset).pack(fill="x", padx=12, pady=12)
        center = ttk.Frame(body, style="Workspace.TFrame")
        center.pack_propagate(False)
        body.add(center, weight=5)
        preview_bar = ttk.Frame(center, style="Workspace.TFrame", padding=(18, 12)); preview_bar.pack(fill="x")
        ttk.Label(preview_bar, text="미리보기", style="PreviewTitle.TLabel").pack(side="left")
        self.preview_context = ttk.Label(preview_bar, style="PreviewMuted.TLabel")
        self.preview_context.pack(side="right")
        self.preview = tk.Canvas(center, bg=UI_COLORS["preview"], highlightthickness=0)
        self.preview.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        self.preview.bind("<Configure>", lambda e: self.render_preview())
        self.preview.bind("<ButtonPress-1>", self._preview_down)
        self.preview.bind("<B1-Motion>", self._preview_move)
        self.preview.bind("<ButtonRelease-1>", self._preview_up)
        transport = ttk.Frame(center, style="Workspace.TFrame", padding=(14, 10)); transport.pack(fill="x")
        transport.columnconfigure(1, weight=1)
        playback = ttk.Frame(transport, style="Workspace.TFrame"); playback.grid(row=0, column=1)
        ttk.Button(playback, text="|◀", style="Quiet.TButton", width=3, command=lambda: self.seek(0)).pack(side="left")
        ttk.Button(playback, text="◀", style="Quiet.TButton", width=3, command=lambda: self.seek(self.position - 1/core.FPS)).pack(side="left")
        self.pause_icon = tk.PhotoImage(master=self.root, width=14, height=14)
        for x in (2, 9):
            self.pause_icon.put(UI_COLORS["text"], to=(x, 2, x+3, 12))
        self.play_button = ttk.Button(playback, text="▶ 재생", style="Tint.TButton", command=self.toggle_play)
        self.play_button.pack(side="left", padx=4)
        ttk.Button(playback, text="▶|", style="Quiet.TButton", width=3, command=lambda: self.seek(self.position + 1/core.FPS)).pack(side="left")
        time_display = ttk.Button(transport, textvariable=self.time_label, style="Time.TButton",
                                  command=self._prompt_time, cursor="hand2")
        time_display.grid(row=0, column=0, sticky="w")
        ttk.Label(transport, text="16:9 · 30fps", style="PreviewMuted.TLabel").grid(row=0, column=2, sticky="e")
        ttk.Label(center, text="사진을 끌어 위치 조절 · 모서리에서 크기 조절", style="PreviewMuted.TLabel").pack(
            anchor="w", padx=18, pady=(0, 14))
        self.right = ttk.Frame(body, width=290, style="Panel.TFrame")
        self.right.pack_propagate(False)
        body.add(self.right, weight=1)
        prop_heading = self._panel_heading(self.right, "클립 속성")
        self.property_scroll_hint = ttk.Label(prop_heading, text="", style="ScrollHint.TLabel")
        self.property_scroll_hint.pack(side="right")
        prop_wrap = ttk.Frame(self.right, style="Panel.TFrame"); prop_wrap.pack(fill="both", expand=True)
        prop_wrap.columnconfigure(0, weight=1)
        prop_wrap.rowconfigure(0, weight=1)
        self.property_canvas = tk.Canvas(prop_wrap, width=1, bg=UI_COLORS["surface"],
                                        highlightthickness=0, yscrollincrement=24)
        self.property_canvas.grid(row=0, column=0, sticky="nsew")
        self.property_scrollbar = ttk.Scrollbar(prop_wrap, orient="vertical", command=self.property_canvas.yview)
        self.property_scrollbar.grid(row=0, column=1, sticky="ns")
        self.property_canvas.configure(yscrollcommand=self._update_property_scroll)
        self.properties = ttk.Frame(self.property_canvas, style="Panel.TFrame", padding=(14, 12))
        self.property_window = self.property_canvas.create_window((0, 0), window=self.properties, anchor="nw")
        self.properties.bind("<Configure>", self._resize_property_content)
        self.property_canvas.bind("<Configure>", self._resize_property_view)
        actions = ttk.Frame(self.right, style="Panel.TFrame", padding=(14, 10)); actions.pack(fill="x")
        ttk.Button(actions, text="복제", command=self.duplicate_selected).pack(side="left")
        ttk.Button(actions, text="삭제", style="Danger.TButton", command=self.delete_selected).pack(side="left", padx=4)
        timeline_area = ttk.Frame(self.workspace, style="Panel.TFrame")
        self.workspace.add(timeline_area, weight=2)
        timeline_bar = ttk.Frame(timeline_area, style="Panel.TFrame", padding=(14, 10)); timeline_bar.pack(fill="x")
        self.timeline_bar = timeline_bar
        ttk.Label(timeline_bar, text="타임라인", style="PanelTitle.TLabel").pack(side="left", padx=(0, 10))
        ttk.Button(timeline_bar, text="＋ 문구", style="Ghost.TButton", command=self.add_text).pack(side="left")
        if hasattr(self, "show_composition"):
            ttk.Button(timeline_bar, text="구성 보기", style="Ghost.TButton", command=self.show_composition).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(timeline_bar, text="스냅", variable=self.snap).pack(side="left", padx=8)
        ttk.Button(timeline_bar, text="+", style="Ghost.TButton", width=3, command=lambda: self.set_zoom(self.zoom * 1.5)).pack(side="right")
        ttk.Button(timeline_bar, text="−", style="Ghost.TButton", width=3, command=lambda: self.set_zoom(self.zoom / 1.5)).pack(side="right")
        ttk.Button(timeline_bar, text="전체 맞춤", style="Ghost.TButton", command=self.fit_zoom).pack(side="right", padx=6)
        ttk.Separator(timeline_area).pack(fill="x")
        timeline_frame = ttk.Frame(timeline_area, style="Panel.TFrame"); timeline_frame.pack(fill="both", expand=True)
        self.track_header_width = 104
        self.track_header = tk.Canvas(timeline_frame, width=self.track_header_width,
                                      bg=UI_COLORS["track_header"], highlightthickness=0)
        self.track_header.pack(side="left", fill="y")
        timeline_body = ttk.Frame(timeline_frame, style="Panel.TFrame"); timeline_body.pack(side="left", fill="both", expand=True)
        self.timeline = tk.Canvas(timeline_body, bg=UI_COLORS["timeline"], highlightthickness=0)
        self.timeline.pack(side="left", fill="both", expand=True)
        self.timeline.bind("<Configure>", lambda e: self.draw_timeline())
        self.timeline.bind("<ButtonPress-1>", self._timeline_down)
        self.timeline.bind("<B1-Motion>", self._timeline_move)
        self.timeline.bind("<ButtonRelease-1>", self._timeline_up)
        self.timeline.bind("<MouseWheel>", self._timeline_wheel)
        vscroll = ttk.Scrollbar(timeline_body, orient="vertical", command=self._scroll_timeline_y)
        vscroll.pack(side="right", fill="y")
        self.timeline.configure(yscrollcommand=vscroll.set)
        scroll = ttk.Scrollbar(timeline_area, orient="horizontal", command=self._scroll_timeline)
        scroll.pack(fill="x")
        self.timeline.configure(xscrollcommand=scroll.set)
        footer = ttk.Frame(self.root, style="Header.TFrame", padding=(14, 7)); footer.pack(fill="x")
        self.duration_label = ttk.Label(footer, text="영상 00:00.000", style="HeaderMuted.TLabel")
        self.duration_label.pack(side="right", padx=(12, 0))
        self.progressbar = ttk.Progressbar(footer, variable=self.progress, maximum=100, length=125)
        self.progressbar.pack(side="left"); self.progressbar.pack_forget()
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel").pack(side="left", padx=8)
        self.cancel_button = ttk.Button(footer, text="작업 취소", command=self.cancel_job, state="disabled")
        self.cancel_button.pack(side="right"); self.cancel_button.pack_forget()
        self.open_file = ttk.Button(footer, text="결과 열기", command=self.open_result, state="disabled")
        self.open_folder = ttk.Button(footer, text="폴더 열기", command=self.open_result_folder, state="disabled")
        self._show_properties()

    def _apply_theme(self):
        c = UI_COLORS
        self.root.configure(bg=c["window"])
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=c["surface"], foreground=c["text"], font=("Malgun Gothic", 9))
        style.configure("TFrame", background=c["surface"])
        style.configure("Panel.TFrame", background=c["surface"])
        style.configure("Header.TFrame", background=c["surface"])
        style.configure("Workspace.TFrame", background=c["surface_alt"])
        style.configure("TLabel", background=c["surface"], foreground=c["text"])
        style.configure("Header.TLabel", background=c["surface"], foreground=c["text"], font=("Malgun Gothic", 12, "bold"))
        style.configure("BrandMark.TLabel", background=c["primary"], foreground="white",
                        padding=(7, 1), font=("Malgun Gothic", 15, "bold"))
        style.configure("Project.TLabel", background=c["surface"], foreground=c["text"], font=("Malgun Gothic", 10))
        style.configure("HeaderMuted.TLabel", background=c["surface"], foreground=c["muted"])
        style.configure("PanelTitle.TLabel", background=c["surface"], foreground=c["text"], font=("Malgun Gothic", 10, "bold"))
        style.configure("Muted.TLabel", background=c["surface"], foreground=c["muted"])
        style.configure("Time.TLabel", background=c["surface_alt"], foreground=c["text"], font=("Consolas", 10))
        style.configure("PreviewTitle.TLabel", background=c["surface_alt"], foreground=c["text"], font=("Malgun Gothic", 10, "bold"))
        style.configure("PreviewMuted.TLabel", background=c["surface_alt"], foreground=c["muted"], font=("Malgun Gothic", 8))
        style.configure("Library.TFrame", background=c["surface"], borderwidth=0,
                        bordercolor=c["border"], relief="solid")
        style.configure("Selected.TFrame", background=c["primary_tint"], borderwidth=2,
                        bordercolor=c["primary"], relief="solid")
        style.configure("Selected.TLabel", background=c["primary_tint"], foreground=c["text"])
        style.configure("Source.TLabel", background=c["primary_tint"], foreground=c["primary"],
                        padding=(8, 5), font=("Malgun Gothic", 9, "bold"))
        style.configure("Result.TFrame", background=c["surface_alt"], borderwidth=1,
                        bordercolor=c["border"], relief="solid")
        style.configure("ResultTitle.TLabel", background=c["surface_alt"], foreground=c["text"],
                        font=("Malgun Gothic", 10, "bold"))
        style.configure("ResultMuted.TLabel", background=c["surface_alt"], foreground=c["muted"])
        style.configure("Success.TLabel", background=c["surface"], foreground="#047857")
        style.configure("TButton", background=c["surface"], foreground=c["text"],
                        borderwidth=1, bordercolor=c["border"], lightcolor=c["surface"],
                        darkcolor=c["surface"], relief="flat", padding=(9, 6))
        style.map("TButton", background=[("disabled", c["surface_alt"]), ("active", c["primary_tint"])],
                  foreground=[("disabled", c["muted"])], bordercolor=[("focus", c["primary"]), ("active", c["primary"])])
        style.configure("Primary.TButton", background=c["primary"], foreground="white",
                        bordercolor=c["primary"], lightcolor=c["primary"], darkcolor=c["primary"], padding=(14, 9))
        style.map("Primary.TButton", background=[("active", c["primary_hover"]), ("disabled", c["border"])],
                  foreground=[("active", "white"), ("disabled", c["muted"])],
                  bordercolor=[("active", c["primary_hover"]), ("disabled", c["border"])])
        style.configure("Tint.TButton", background=c["primary_tint"], foreground=c["primary"],
                        bordercolor=c["primary_tint"], lightcolor=c["primary_tint"], darkcolor=c["primary_tint"])
        style.configure("Quiet.TButton", background=c["surface_alt"], bordercolor=c["surface_alt"],
                        lightcolor=c["surface_alt"], darkcolor=c["surface_alt"], padding=(5, 6))
        style.configure("Section.TButton", anchor="w", font=("Malgun Gothic", 9, "bold"),
                        background=c["surface_alt"], bordercolor=c["surface_alt"])
        style.configure("TMenubutton", background=c["surface"], foreground=c["text"],
                        bordercolor=c["border"], lightcolor=c["surface"], darkcolor=c["surface"],
                        relief="flat", padding=(9, 6))
        style.map("TMenubutton", background=[("active", c["primary_tint"])])
        style.configure("Danger.TButton", foreground="#B91C1C", bordercolor="#FECACA")
        style.map("Danger.TButton", foreground=[("active", "#B91C1C")])
        style.configure("TEntry", fieldbackground=c["surface_alt"], foreground=c["text"],
                        insertcolor=c["text"], bordercolor=c["border"], padding=5)
        style.map("TEntry", bordercolor=[("focus", c["primary"])])
        style.configure("TCombobox", fieldbackground=c["surface"], background=c["surface"],
                        foreground=c["text"], bordercolor=c["border"], padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", c["surface"])],
                  foreground=[("readonly", c["text"])], selectbackground=[("readonly", c["surface"])],
                  selectforeground=[("readonly", c["text"])], bordercolor=[("focus", c["primary"])])
        style.configure("TCheckbutton", background=c["surface"], foreground=c["text"])
        style.map("TCheckbutton", background=[("active", c["surface"])])
        style.configure("TScrollbar", background=c["surface_alt"], troughcolor=c["surface"],
                        arrowcolor=c["muted"], bordercolor=c["border"], lightcolor=c["surface_alt"],
                        darkcolor=c["surface_alt"], relief="flat")
        style.configure("TSeparator", background=c["border"])
        style.configure("TPanedwindow", background=c["border"], sashwidth=4, sashrelief="flat")
        style.configure("Treeview", background=c["surface"], fieldbackground=c["surface"],
                        foreground=c["text"], bordercolor=c["border"], rowheight=28)
        style.map("Treeview", background=[("selected", c["primary_tint"])],
                  foreground=[("selected", c["text"])])
        style.configure("Treeview.Heading", background=c["surface_alt"], foreground=c["muted"],
                        relief="flat", padding=(8, 7), font=("Malgun Gothic", 9, "bold"))
        style.map("Treeview.Heading", background=[("active", c["primary_tint"])])
        style.configure("TProgressbar", troughcolor=c["surface_alt"], background=c["primary"],
                        bordercolor=c["border"], lightcolor=c["primary"], darkcolor=c["primary"])
        self._studio_theme_images = install_studio_controls(self.root, style, c)

    def _panel_heading(self, parent, title):
        bar = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 12)); bar.pack(fill="x")
        ttk.Label(bar, text=title, style="PanelTitle.TLabel").pack(side="left")
        ttk.Separator(parent).pack(fill="x")
        return bar

    def _property_heading(self, title):
        ttk.Separator(self.properties).pack(fill="x", pady=(10, 7))
        ttk.Label(self.properties, text=title, style="PanelTitle.TLabel").pack(anchor="w", pady=(0, 6))

    def _update_property_scroll(self, first, last):
        self.property_scrollbar.set(first, last)
        above, below = float(first) > .001, float(last) < .999
        hint = "↑ 위·아래로 스크롤 ↓" if above and below else "아래로 스크롤 ↓" if below else "↑ 위로 스크롤" if above else ""
        self.property_scroll_hint.configure(text=hint)

    def _resize_property_content(self, event=None):
        bounds = self.property_canvas.bbox("all")
        self.property_canvas.configure(scrollregion=bounds)
        if bounds and bounds[3] - bounds[1] <= self.property_canvas.winfo_height():
            self.property_canvas.yview_moveto(0)

    def _resize_property_view(self, event):
        self.property_canvas.itemconfigure(self.property_window, width=event.width)
        self.root.after_idle(self._resize_property_content)

    def _refresh_project_heading(self):
        name = self.project_file.stem if self.project_file else "새 프로젝트"
        self.project_name_label.configure(text=name if len(name) <= 28 else name[:27] + "…")
        self.project_state_label.configure(text="● 수정됨" if self.dirty else "저장됨" if self.project_file else "저장 전")

    def _prompt_time(self, event=None):
        value = simpledialog.askstring("시각으로 이동", "분:초.밀리초 또는 초를 입력하세요.",
                                       initialvalue=clock(self.position), parent=self.root)
        if value is None: return
        try:
            parts = value.strip().split(":")
            if len(parts) == 2: seconds = int(parts[0])*60 + float(parts[1])
            elif len(parts) == 1: seconds = float(parts[0])
            else: raise ValueError
            if not math.isfinite(seconds) or seconds < 0: raise ValueError
            self.seek(round(seconds*core.FPS)/core.FPS)
        except ValueError:
            messagebox.showerror("시각 입력", "예: 01:23.500 또는 83.5를 입력하세요.")

    def _build_menu(self, parent):
        for title, entries in (
            ("파일", (("새 프로젝트", self.new_project), ("열기", self.open_project),
                    ("저장", self.save_project), ("다른 이름으로 저장", self.save_as),
                    ("음악 가져오기", self.choose_audio), ("이미지 가져오기", self.add_assets),
                    *((("영상 가져오기", self.choose_video),) if hasattr(self, "choose_video") else ()),
                    ("MP4 내보내기", self.start_export))),
            ("편집", (("실행 취소", self.undo_action), ("다시 실행", self.redo_action),
                    ("복제", self.duplicate_selected), ("삭제", self.delete_selected))),
            ("보기", (("프로젝트 패널", lambda: self.toggle_panel("left")),
                    ("속성 패널", lambda: self.toggle_panel("right")),
                    ("전체 길이 맞춤", self.fit_zoom), ("기본 배치 복원", self.reset_layout)))):
            sub = tk.Menu(parent, tearoff=False, bg=UI_COLORS["surface"], fg=UI_COLORS["text"],
                          activebackground=UI_COLORS["primary_tint"], activeforeground=UI_COLORS["text"])
            for label, action in entries: sub.add_command(label=label, command=action)
            ttk.Menubutton(parent, text=title, menu=sub, style="Chrome.TMenubutton").pack(side="left", padx=(0, 4))

    def toggle_panel(self, side):
        panel = self.left if side == "left" else self.right
        if str(panel) in self.body.panes(): self.body.forget(panel)
        else: self.body.insert(0 if side == "left" else "end", panel, weight=1)

    def reset_layout(self):
        for side in ("left", "right"):
            panel = getattr(self, side)
            if str(panel) not in self.body.panes(): self.toggle_panel(side)
        self.root.state("normal")
        self._default_window_geometry()
        self.saved_panes = None
        self._panes_initialized = False
        self.root.after_idle(self._set_initial_panes)

    def _scroll_timeline_y(self, *args):
        self.timeline.yview(*args)
        self.track_header.yview(*args)

    def _panel_wheel(self, event):
        if isinstance(event.widget, ttk.Combobox): return None
        for canvas in (self.library_canvas, self.property_canvas):
            if (canvas.winfo_rootx() <= event.x_root < canvas.winfo_rootx()+canvas.winfo_width()
                    and canvas.winfo_rooty() <= event.y_root < canvas.winfo_rooty()+canvas.winfo_height()):
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
        return None

    def _editable(self):
        if self.exporting:
            self.status.set("MP4 내보내기 중에는 편집할 수 없습니다.")
            return False
        return True

    def _change(self):
        self.undo.append(copy.deepcopy(self.project))
        self.undo = self.undo[-50:]
        self.redo.clear()
        self.dirty = True
        self._refresh_project_heading()
        self.scene_cache_key = None

    def _refresh(self, properties=True):
        signature = (tuple((a["id"], a["path"]) for a in self.project["assets"]),
                     self.project_search.get(), self.library_view.get())
        if signature != self._library_signature: self._refresh_library()
        self.draw_timeline()
        self.render_preview()
        if properties: self._show_properties()
        self.duration_label.configure(text="음악 " + clock(self.duration))
        self.audio_info.configure(text=(Path(self.project["audio"]).name + "  ·  " + clock(self.duration)) if self.project["audio"] else "음악을 가져오세요")
        self.time_label.set(clock(self.position) + " / " + clock(self.duration))
        self.root.title(("● " if self.dirty else "") + "선율담")
        self._refresh_project_heading()

    def _refresh_library(self):
        videos = self.project.get("video_assets", [])
        cache = getattr(self, "video_cache", {})
        self._library_signature = (tuple((a["id"], a["path"], a["id"] in cache)
                                        for a in self.project["assets"] + videos),
                                   self.project_search.get(), self.library_view.get())
        for child in self.library_frame.winfo_children(): child.destroy()
        self.thumb_refs.clear()
        self.library_markers.clear()
        self.library_video_progress = {}
        query = self.project_search.get().casefold().strip()
        icon_view = self.library_view.get() == "썸네일"
        shown = 0
        for a in self.project["assets"] + videos:
            if self.library_filter.get() == "사진" and a in videos: continue
            if self.library_filter.get() == "영상" and a not in videos: continue
            if query and query not in Path(a["path"]).name.casefold(): continue
            selected = a["id"] == self.library_selection
            row = ttk.Frame(self.library_frame, padding=8,
                            style="Selected.TFrame" if selected else "Library.TFrame")
            if icon_view:
                row.grid(row=shown//2, column=shown%2, sticky="nsew", padx=4, pady=4)
            else:
                row.grid(row=shown, column=0, columnspan=2, sticky="ew", padx=4, pady=2)
            shown += 1
            try:
                from PIL import Image, ImageOps
                path = Path(cache[a["id"]]["thumbnail"] if a in videos else a["path"])
                key = (str(path), path.stat().st_mtime_ns, icon_view)
                photo = self.thumbnail_cache.get(key)
                if photo is None:
                    with Image.open(path) as src:
                        im = ImageOps.exif_transpose(src).convert("RGBA")
                        im.thumbnail((96, 64) if icon_view else (42, 32))
                        bg = Image.new("RGBA", im.size, "black"); bg.alpha_composite(im)
                        photo = ImageTk.PhotoImage(bg, master=self.root)
                    if len(self.thumbnail_cache) > 180: self.thumbnail_cache.clear()
                    self.thumbnail_cache[key] = photo
                self.thumb_refs.append(photo)
                thumb = ttk.Label(row, image=photo,
                                  style="Selected.TLabel" if selected else "TLabel")
            except Exception:
                thumb = ttk.Label(row, text="영상" if a in videos else "이미지 없음", width=10,
                                  style="Selected.TLabel" if selected else "TLabel")
            thumb.pack(side="top" if icon_view else "left")
            label = ttk.Label(row, text=(("▷ " + Path(a["path"]).name + "\n" +
                                         (clock(a.get("frames", 0)/core.FPS) if a["id"] in cache else self._video_import_caption(a["id"])))
                                        if a in videos else Path(a["path"]).name), wraplength=108 if icon_view else 165,
                              style="Selected.TLabel" if selected else "TLabel")
            label.pack(side="top" if icon_view else "left", padx=5)
            if a in videos and a["id"] not in cache:
                self.library_video_progress[a["id"]] = (label, Path(a["path"]).name)
            for widget in (row, thumb, label):
                widget.bind("<ButtonPress-1>", lambda e, ident=a["id"]: self._library_down(ident))
            marker = ttk.Label(row, text="●" if selected else "",
                               background=UI_COLORS["primary_tint"] if selected else UI_COLORS["surface"],
                               foreground=UI_COLORS["primary"])
            marker.pack(side="right")
            self.library_markers[a["id"]] = marker
        self.library_frame.columnconfigure(0, weight=1)
        self.library_frame.columnconfigure(1, weight=1)
        if not shown and query:
            ttk.Label(self.library_frame, text="일치하는 미디어가 없습니다.",
                      style="Muted.TLabel").grid(row=0, column=0, columnspan=2, padx=10, pady=10)

    def _video_import_caption(self, ident):
        percent, stage = getattr(self, "video_progress", {}).get(ident, (None, "준비 필요"))
        return f"{stage} · {percent}%" if percent is not None else stage

    def _confirm_dirty(self):
        if not self.dirty: return True
        answer = messagebox.askyesnocancel("저장 확인", "변경 사항을 저장할까요?")
        if answer is None: return False
        return self.save_project() if answer else True

    def new_project(self):
        if not self._editable() or not self._confirm_dirty(): return
        self._stop_audio(); self._dispose_pcm(); self.audio_cancel.set()
        self.audio_generation += 1
        self.project = core.fresh(); self.project_file = None; self.duration = 0; self.position = 0
        self.bins = []; self.undo.clear(); self.redo.clear(); self.selection = None; self.library_selection = None; self.dirty = False
        self.scene_cache_key = None
        self.status.set("음악 파일을 선택하세요."); self._refresh()

    def open_project(self):
        if not self._editable() or not self._confirm_dirty(): return
        path = filedialog.askopenfilename(filetypes=[("프로젝트 JSON", "*.json")])
        if not path: return
        try:
            data = core.load_project(path)
            missing = core.missing_media(data)
            for kind, asset_id, old in missing:
                messagebox.showinfo("파일 다시 연결", f"파일을 찾을 수 없습니다:\n{old}\n\n다시 선택하세요.")
                types = [("음악", "*.flac *.wav *.mp3")] if kind == "audio" else [("이미지", "*.jpg *.jpeg *.png")]
                replacement = filedialog.askopenfilename(filetypes=types)
                if not replacement: return
                if kind == "audio": data["audio"] = replacement
                else: next(a for a in data["assets"] if a["id"] == asset_id)["path"] = replacement
            self._stop_audio(); self._dispose_pcm(); self.audio_cancel.set()
            self.audio_generation += 1
            self.project = data; self.project_file = Path(path); self.selection = None
            self.library_selection = None
            self.undo.clear(); self.redo.clear(); self.dirty = bool(missing)
            self.position = 0; self.bins = []; self.duration = 0
            self.scene_cache_key = None
            if data["audio"]: self._load_audio(data["audio"])
            self._refresh()
        except core.EditorError as e: messagebox.showerror("열기 오류", str(e))

    def save_project(self):
        if self.project_file is None: return self.save_as()
        try:
            core.save_project(self.project, self.project_file)
            self.dirty = False; self.status.set("프로젝트를 저장했습니다."); self._refresh(False)
            return True
        except core.EditorError as e:
            messagebox.showerror("저장 오류", str(e)); return False

    def save_as(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("프로젝트 JSON", "*.json")])
        if not path: return False
        previous = self.project_file; self.project_file = Path(path)
        if self.save_project(): return True
        self.project_file = previous; return False

    def choose_audio(self):
        if not self._editable(): return
        path = filedialog.askopenfilename(filetypes=[("음악", "*.flac *.wav *.mp3")])
        if not path: return
        self._change(); self.project["audio"] = path; self._load_audio(path)

    def _dispose_pcm(self):
        if self.play_thread and self.play_thread.is_alive():
            self.play_stop.set(); self.play_thread.join(timeout=2)
        if self.pcm:
            try: Path(self.pcm).unlink(missing_ok=True)
            except OSError: pass
            self.pcm = None

    def _load_audio(self, path):
        self._stop_audio(); self.audio_cancel.set(); self._dispose_pcm()
        self.audio_cancel = threading.Event()
        self.audio_generation += 1
        generation = self.audio_generation
        cancel = self.audio_cancel
        self.status.set("음악 길이와 파형을 분석합니다…")
        self.cancel_button.configure(state="normal")
        self.progressbar.configure(mode="indeterminate")
        self.progressbar.start(12)
        self._show_job_controls(running=True)
        def worker():
            try:
                estimate = core.probe_audio(path, self.tool("ffprobe"))
                self.events.put(("estimate", generation, estimate))
                pcm, bins, duration = core.analyze_audio(path, self.tool("ffmpeg"), cancel)
                if cancel.is_set(): Path(pcm).unlink(missing_ok=True)
                else: self.events.put(("audio", generation, path, pcm, bins, duration))
            except Exception as e:
                if not cancel.is_set(): self.events.put(("error", generation, str(e)))
        self.audio_thread = threading.Thread(target=worker, daemon=True)
        self.audio_thread.start()

    def add_assets(self):
        if not self._editable(): return
        paths = filedialog.askopenfilenames(filetypes=[("이미지", "*.jpg *.jpeg *.png")])
        if not paths: return
        valid = []
        for p in paths:
            try:
                from PIL import Image
                with Image.open(p) as im: im.verify()
                valid.append(p)
            except Exception:
                messagebox.showwarning("이미지 제외", f"읽을 수 없는 이미지: {p}")
        if not valid: return
        self._change()
        known = {str(Path(a["path"]).resolve()).lower() for a in self.project["assets"]}
        for p in valid:
            if str(Path(p).resolve()).lower() not in known:
                self.project["assets"].append({"id": core.uid(), "path": p})
                known.add(str(Path(p).resolve()).lower())
        if not self.project["images"] and self.project["assets"]:
            self.project["images"].append({"id": core.uid(), "asset": self.project["assets"][0]["id"], "start": 0})
        self.status.set(f"이미지 {len(valid)}개를 등록했습니다.")
        self._refresh()

    def _library_down(self, ident):
        old = self.library_selection
        self.library_selection = ident
        self.library_drag_id = ident
        self.selection = None
        if old in self.library_markers: self.library_markers[old].configure(text="")
        if ident in self.library_markers: self.library_markers[ident].configure(text="●")
        self._show_properties()

    def _library_drop_anywhere(self, e):
        ident = self.library_drag_id
        self.library_drag_id = None
        if not self._editable() or not self.duration or not self.project["assets"]: return
        if not ident: return
        x, y = e.x_root, e.y_root
        t = self.timeline
        if t.winfo_rootx() <= x <= t.winfo_rootx() + t.winfo_width() and t.winfo_rooty() <= y <= t.winfo_rooty() + t.winfo_height():
            self.place_asset(ident, self._x_to_frame(t.canvasx(x - t.winfo_rootx())))

    def add_selected_asset(self):
        if self.library_selection and self.duration:
            self.place_asset(self.library_selection, frame_of(self.position, self.duration))

    def place_asset(self, asset_id, frame):
        if not self._editable(): return
        frame = max(0, min(core.total_frames(self.duration) - 1, frame))
        existing = next((c for c in self.project["images"] if c["start"] == frame), None)
        if existing and not messagebox.askyesno("이미지 교체", "이 시각의 이미지를 교체할까요?"):
            return
        self._change()
        if existing: existing["asset"] = asset_id; cue = existing
        else:
            cue = {"id": core.uid(), "asset": asset_id, "start": frame}
            self.project["images"].append(cue)
        self.selection = ("image", cue["id"]); self.seek(frame / core.FPS); self._refresh()

    def add_text(self):
        if not self._editable() or not self.duration: return
        self._change()
        start = frame_of(self.position, self.duration)
        end = min(core.total_frames(self.duration), start + 3 * core.FPS)
        if end <= start: start = max(0, end - 1)
        item = {"id": core.uid(), "start": start, "end": end, "text": "텍스트",
                "x": 960, "y": 840, "width": 1200, "size": 64, "font": "malgun.ttf",
                "color": "#ffffff", "background": "#000000", "background_alpha": 160,
                "outline": 2, "align": "center",
                "order": max((t.get("order", 0) for t in self.project["texts"]), default=0) + 1}
        self.project["texts"].append(item); self.selection = ("text", item["id"]); self._refresh()

    def _selected(self):
        if not self.selection: return None
        kind, ident = self.selection
        collection = self.project["images"] if kind == "image" else self.project["texts"]
        return next((i for i in collection if i["id"] == ident), None)

    def delete_selected(self):
        item = self._selected()
        if not item or not self._editable(): return
        self._change()
        kind = self.selection[0]
        collection = self.project["images"] if kind == "image" else self.project["texts"]
        collection.remove(item); self.selection = None; self._refresh()

    def duplicate_selected(self):
        item = self._selected()
        if not item or not self._editable() or not self.duration: return
        new = copy.deepcopy(item); new["id"] = core.uid()
        if self.selection[0] == "image":
            used = {i["start"] for i in self.project["images"]}
            start = min(core.total_frames(self.duration)-1, item["start"] + core.FPS)
            while start in used and start < core.total_frames(self.duration)-1: start += 1
            if start in used:
                messagebox.showwarning("복제 불가", "이미지를 놓을 빈 시작 프레임이 없습니다."); return
            new["start"] = start
            self._change(); self.project["images"].append(new)
        else:
            shift = min(core.FPS, max(0, core.total_frames(self.duration) - item["end"]))
            new["start"] += shift; new["end"] += shift
            new["order"] = max((t.get("order",0) for t in self.project["texts"]), default=0)+1
            self._change(); self.project["texts"].append(new)
        self.selection = (self.selection[0], new["id"]); self._refresh()

    def undo_action(self):
        if not self.undo or not self._editable(): return
        self.redo.append(copy.deepcopy(self.project))
        self.project = self.undo.pop(); self.dirty = True
        self.selection = None; self._refresh()
        self.scene_cache_key = None
        if self.project["audio"] and (not self.pcm or self.project["audio"] != getattr(self, "_loaded_audio", None)):
            self._load_audio(self.project["audio"])

    def redo_action(self):
        if not self.redo or not self._editable(): return
        self.undo.append(copy.deepcopy(self.project))
        self.project = self.redo.pop(); self.dirty = True
        self.selection = None; self._refresh()
        self.scene_cache_key = None
        if self.project["audio"] and (not self.pcm or self.project["audio"] != getattr(self, "_loaded_audio", None)):
            self._load_audio(self.project["audio"])

    def _show_properties(self):
        self._populate_properties()
        # An offscreen canvas window may not receive Configure after its contents shrink.
        self.root.after_idle(self._resize_property_content)

    def _populate_properties(self):
        for child in self.properties.winfo_children(): child.destroy()
        item = self._selected()
        if not item:
            if self.library_selection:
                asset = next((a for a in self.project["assets"] if a["id"] == self.library_selection), None)
                if asset:
                    ttk.Label(self.properties, text=Path(asset["path"]).name, wraplength=250,
                              style="PanelTitle.TLabel").pack(anchor="w", pady=8)
                    ttk.Button(self.properties, text="재생 위치에 추가", command=self.add_selected_asset).pack(fill="x")
                    return
            ttk.Label(self.properties, text="타임라인에서 클립을 선택하세요.", wraplength=250,
                      style="Muted.TLabel").pack(anchor="w", pady=8)
            ttk.Label(self.properties, text="영상 1920×1080 · 30fps", style="Muted.TLabel").pack(anchor="w")
            return
        kind = self.selection[0]
        if kind == "image":
            asset = next((a for a in self.project["assets"] if a["id"] == item["asset"]), None)
            ttk.Label(self.properties, text=Path(asset["path"]).name if asset else "이미지 없음", wraplength=250,
                      style="PanelTitle.TLabel").pack(anchor="w", pady=5)
            self._prop_entry("시작 시각 (초)", f"{item['start']/core.FPS:.3f}", "start_seconds")
            cues = sorted(self.project["images"], key=lambda x: x["start"])
            idx = next((i for i, c in enumerate(cues) if c["id"] == item["id"]), -1)
            end = cues[idx+1]["start"] / core.FPS if idx >= 0 and idx+1 < len(cues) else self.duration
            ttk.Label(self.properties, text=f"끝: {clock(end)}  ·  다음 이미지까지 유지", style="Muted.TLabel",
                      wraplength=250).pack(anchor="w", pady=8)
            return
        self._property_group("text", "텍스트", lambda box: self._build_text_props(box, item))
        self._property_group("time", "표시 시간", lambda box: (
            self._prop_entry("시작 (초)", f"{item['start']/core.FPS:.3f}", "start_seconds", box),
            self._prop_entry("끝 (초)", f"{item['end']/core.FPS:.3f}", "end_seconds", box)))
        self._property_group("position", "위치와 정렬", lambda box: self._build_position_props(box, item))
        self._property_group("appearance", "배경과 외곽선", lambda box: self._build_appearance_props(box, item))

    def _property_group(self, key, title, build):
        opened = self.property_groups[key]
        ttk.Button(self.properties, text=("▾  " if opened else "▸  ") + title, style="Section.TButton",
                   command=lambda: self._toggle_property_group(key)).pack(fill="x", pady=(8, 3))
        if opened:
            box = ttk.Frame(self.properties, style="Panel.TFrame")
            box.pack(fill="x")
            build(box)

    def _toggle_property_group(self, key):
        self.property_groups[key] = not self.property_groups[key]
        self._show_properties()

    def _build_text_props(self, box, item):
        self._prop_text(item, box)
        self._prop_entry("글자 크기", str(item["size"]), "size", box)
        fonts = {"malgun.ttf": "맑은 고딕", "malgunbd.ttf": "맑은 고딕 굵게", "arial.ttf": "Arial", "batang.ttc": "바탕"}
        ttk.Label(box, text="글꼴").pack(anchor="w", pady=(6, 2))
        font_var = tk.StringVar(value=fonts.get(item.get("font"), item.get("font", "malgun.ttf")))
        combo = ttk.Combobox(box, textvariable=font_var, values=list(fonts.values()))
        combo.pack(fill="x")
        combo.bind("<<ComboboxSelected>>", lambda e: self._commit("font", next((k for k, v in fonts.items() if v == font_var.get()), font_var.get()), item["id"]))
        ttk.Button(box, text="글자 색  " + item["color"], command=lambda: self._choose_color("color")).pack(fill="x", pady=6)

    def _build_position_props(self, box, item):
        for label, key in (("가로 중심 (px)", "x"), ("위쪽 위치 (px)", "y"), ("줄바꿈 폭 (px)", "width")):
            self._prop_entry(label, str(item[key]), key, box)
        ttk.Label(box, text="좌표 기준: 1920×1080", style="Muted.TLabel").pack(anchor="w", pady=4)
        ttk.Label(box, text="정렬").pack(anchor="w")
        names = {"left": "왼쪽", "center": "가운데", "right": "오른쪽"}
        align = ttk.Combobox(box, values=list(names.values()), state="readonly")
        align.set(names.get(item.get("align"), "가운데")); align.pack(fill="x")
        align.bind("<<ComboboxSelected>>", lambda e: self._commit("align", next(k for k,v in names.items() if v == align.get()), item["id"]))
        order_bar = ttk.Frame(box, style="Panel.TFrame"); order_bar.pack(fill="x", pady=5)
        ttk.Button(order_bar, text="앞으로", command=lambda: self._shift_order(1)).pack(side="left")
        ttk.Button(order_bar, text="뒤로", command=lambda: self._shift_order(-1)).pack(side="left", padx=4)

    def _build_appearance_props(self, box, item):
        ttk.Button(box, text="배경 색  " + item["background"], command=lambda: self._choose_color("background")).pack(fill="x")
        self._prop_entry("배경 불투명도 (%)", str(round(item["background_alpha"] * 100 / 255)), "background_percent", box)
        self._prop_entry("외곽선 두께", str(item["outline"]), "outline", box)

    def _shift_order(self, direction):
        item = self._selected()
        if not item or not self.selection or self.selection[0] != "text": return
        ordered = sorted(self.project["texts"], key=lambda t: t.get("order", 0))
        index = ordered.index(item)
        other_index = index + direction
        if other_index < 0 or other_index >= len(ordered): return
        self._change()
        ordered[index], ordered[other_index] = ordered[other_index], ordered[index]
        for number, clip in enumerate(ordered): clip["order"] = number
        self._refresh()

    def _prop_text(self, item, parent=None):
        parent = parent or self.properties
        ttk.Label(parent, text="문구 (여러 줄)").pack(anchor="w")
        box = tk.Text(parent, height=4, wrap="word", font=("Malgun Gothic", 10),
                      bg=UI_COLORS["surface"], fg=UI_COLORS["text"],
                      insertbackground=UI_COLORS["text"], relief="solid", bd=1,
                      highlightthickness=1, highlightbackground=UI_COLORS["border"],
                      highlightcolor=UI_COLORS["primary"])
        box.insert("1.0", item["text"]); box.pack(fill="x")
        box.bind("<FocusOut>", lambda e: self._commit("text", box.get("1.0", "end-1c"), item["id"]))
        box.bind("<Control-Return>", lambda e: (self._commit("text", box.get("1.0", "end-1c"), item["id"]), "break"))

    def _prop_entry(self, label, value, key, parent=None):
        parent = parent or self.properties
        row = ttk.Frame(parent, style="Panel.TFrame"); row.pack(fill="x", pady=4)
        row.columnconfigure(0, weight=1)
        ttk.Label(row, text=label, style="Muted.TLabel", wraplength=145).grid(row=0, column=0, sticky="w", padx=(0, 8))
        var = tk.StringVar(value=value)
        entry = ttk.Entry(row, textvariable=var, width=10, justify="right"); entry.grid(row=0, column=1, sticky="e")
        selected_id = self.selection[1] if self.selection else None
        entry.bind("<Return>", lambda e: self._commit(key, var.get(), selected_id))
        entry.bind("<FocusOut>", lambda e: self._commit(key, var.get(), selected_id))

    def _prop_fields(self, fields, parent=None):
        row = ttk.Frame(parent or self.properties)
        row.pack(fill="x", pady=(2, 5))
        selected_id = self.selection[1] if self.selection else None
        for column, (label, value, key) in enumerate(fields):
            row.columnconfigure(column, weight=1, uniform="fields")
            field = ttk.Frame(row)
            field.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 5 if column < len(fields)-1 else 0))
            ttk.Label(field, text=label, style="Muted.TLabel").pack(anchor="w", pady=(0, 5))
            entry = ttk.Entry(field, width=6)
            entry.insert(0, value)
            entry.pack(fill="x")
            commit = lambda e, widget=entry, name=key: self._commit(name, widget.get(), selected_id)
            entry.bind("<Return>", commit)
            entry.bind("<FocusOut>", commit)

    def _choose_color(self, key):
        item = self._selected()
        if not item: return
        color = colorchooser.askcolor(color=item[key])[1]
        if color: self._commit(key, color)

    def _commit(self, key, raw, selected_id=None):
        item = self._selected()
        if not item or (selected_id and item["id"] != selected_id) or not self._editable(): return
        try:
            if key == "background_percent":
                percent = max(0, min(100, int(float(raw))))
                key, raw = "background_alpha", str(round(percent * 255 / 100))
            if key in ("start_seconds", "end_seconds"):
                value = round(float(raw) * core.FPS)
                value = max(0, min(core.total_frames(self.duration) - (1 if key == "start_seconds" else 0), value))
                field = "start" if key == "start_seconds" else "end"
                if self.selection[0] == "image" and any(c["id"] != item["id"] and c["start"] == value for c in self.project["images"]):
                    messagebox.showwarning("시간 충돌", "그 시각에 이미지가 이미 있습니다."); return
                if self.selection[0] == "text" and ((field == "start" and value >= item["end"]) or (field == "end" and value <= item["start"])):
                    raise ValueError("끝 시각은 시작 시각보다 뒤여야 합니다.")
                key = field
            elif key in ("x", "y", "size", "width", "background_alpha", "outline", "order"):
                value = int(float(raw))
                limits = {"x": (0,1920), "y": (0,1080), "size":(8,240), "width":(80,1900),
                          "background_alpha":(0,255), "outline":(0,20), "order":(0,10000)}
                value = max(limits[key][0], min(limits[key][1], value))
            else: value = raw
            if item.get(key) == value: return
            self._change(); item[key] = value; self._refresh(False)
        except ValueError as e:
            messagebox.showerror("입력 오류", f"올바른 값을 입력하세요.\n{e}")
        self._show_properties()

    def _scene_warn(self, name):
        if name not in self.warned_fonts:
            self.warned_fonts.add(name)
            self.status.set(f"글꼴 {name}을 찾지 못해 대체 글꼴을 사용합니다.")

    def render_preview(self):
        if not self.preview.winfo_exists(): return
        w, h = self.preview.winfo_width(), self.preview.winfo_height()
        if w < 20 or h < 20: return
        scale = min(w / 1920, h / 1080)
        width, height = max(1, int(1920 * scale)), max(1, int(1080 * scale))
        x, y = (w-width)//2, (h-height)//2
        try:
            frame = min(core.total_frames(self.duration)-1, round(self.position * core.FPS))
            cue = core.active_image(self.project, frame)
            active = tuple((t["id"], t["start"], t["end"]) for t in self.project["texts"] if t["start"] <= frame < t["end"])
            cache_key = (cue["id"] if cue else None, active, width, height, len(self.undo))
            if self.scene_cache_key != cache_key or self.preview_ref is None:
                image = core.render_scene(self.project, frame, self._scene_warn)
                image = image.resize((width, height))
                self.preview_ref = ImageTk.PhotoImage(image, master=self.root)
                self.scene_cache_key = cache_key
            self.preview.delete("all"); self.preview.create_image(x, y, image=self.preview_ref, anchor="nw")
            self.preview_rect = (x, y, width, height)
            if not self.project["audio"] and not self.project["images"]:
                self.preview.create_text(w/2, h/2, text="음악과 이미지를 가져와 시작하세요",
                                         fill="#afb5bf", font=("Malgun Gothic", 13))
            item = self._selected()
            if item and self.selection[0] == "text" and item["start"] <= frame < item["end"]:
                l, t, r, b = core.text_box(item)
                self.preview.create_rectangle(x+l*scale, y+t*scale, x+r*scale, y+b*scale,
                                              outline="#00d6ff", width=2)
        except core.EditorError as e:
            self.status.set(str(e).splitlines()[0])

    def _preview_hit(self, px, py):
        x, y, w, h = self.preview_rect
        if not (x <= px <= x+w and y <= py <= y+h): return None
        fx, fy = (px-x)*1920/w, (py-y)*1080/h
        frame = min(core.total_frames(self.duration)-1, round(self.position*core.FPS))
        for t in sorted(self.project["texts"], key=lambda t: t.get("order",0), reverse=True):
            if not t["start"] <= frame < t["end"]: continue
            l, top, r, bottom = core.text_box(t)
            if l <= fx <= r and top <= fy <= bottom:
                return t
        return None

    def _preview_down(self, e):
        item = self._preview_hit(e.x, e.y)
        if item:
            self.selection = ("text", item["id"])
            self.preview_drag = (e.x, e.y, item["x"], item["y"], False)
            self._show_properties(); self.render_preview()
        else:
            self.selection = None
            self.library_selection = None
            self._show_properties(); self.render_preview(); self.draw_timeline()

    def _preview_move(self, e):
        if not self.preview_drag or not self._editable(): return
        item = self._selected()
        if not item: return
        sx, sy, ox, oy, recorded = self.preview_drag
        if not recorded:
            self._change(); recorded = True
        _, _, w, h = self.preview_rect
        item["x"] = max(0, min(1920, round(ox+(e.x-sx)*1920/w)))
        item["y"] = max(0, min(1080, round(oy+(e.y-sy)*1080/h)))
        self.scene_cache_key = None
        self.preview_drag = (sx, sy, ox, oy, recorded)
        self.render_preview()

    def _preview_up(self, e):
        if self.preview_drag and self.preview_drag[4]: self._refresh()
        self.preview_drag = None

    def _x_to_frame(self, x):
        return max(0, min(core.total_frames(self.duration)-1, round(max(0,x)/self.zoom*core.FPS)))

    def _frame_x(self, frame):
        return frame/core.FPS*self.zoom

    def set_zoom(self, value):
        center = self.timeline.canvasx(self.timeline.winfo_width() / 2)
        anchor = center / self.zoom
        self.zoom = max(.05, min(600, value)); self.draw_timeline()
        full = max(self.timeline.winfo_width(), self.duration * self.zoom)
        self.timeline.xview_moveto(max(0, anchor * self.zoom - self.timeline.winfo_width()/2) / max(1, full))
        self.draw_timeline()

    def fit_zoom(self):
        if self.duration:
            self.set_zoom(max(.05, (self.timeline.winfo_width()-8)/self.duration))
            self.timeline.xview_moveto(0); self.draw_timeline()

    def _scroll_timeline(self, *args):
        self.timeline.xview(*args); self.draw_timeline()

    def _timeline_wheel(self, e):
        if e.state & 0x4:
            self.set_zoom(self.zoom * (1.2 if e.delta > 0 else 1/1.2)); return
        if e.state & 0x1: self.timeline.xview_scroll(-1 if e.delta > 0 else 1, "units")
        else: self._scroll_timeline_y("scroll", -1 if e.delta > 0 else 1, "units")
        self.draw_timeline()

    def draw_timeline(self):
        c = self.timeline
        if not c.winfo_exists(): return
        c.delete("all")
        full_w = max(c.winfo_width(), 8+self.duration*self.zoom)
        lanes = self._text_lanes()
        text_rows = max(1, len(lanes))
        image_y = 24 + 42 * text_rows
        audio_y = image_y + 58
        height = max(c.winfo_height(), audio_y + 58)
        c.configure(scrollregion=(0,0,full_w,height))
        c.create_rectangle(0,0,full_w,24,fill="#29313b",outline="")
        c.create_rectangle(0,24,full_w,image_y,fill="#24262d",outline="")
        c.create_rectangle(0,image_y,full_w,audio_y,fill="#272a32",outline="")
        c.create_rectangle(0,audio_y,full_w,height,fill="#242a30",outline="")
        self.track_header.delete("all")
        self.track_header.configure(scrollregion=(0,0,self.track_header_width,height))
        for y,label in ((image_y+29,"이미지"),(audio_y+29,"음악")):
            self.track_header.create_text(8,y,text=label,anchor="w",fill="#d6d6d6",font=("Malgun Gothic",9))
        for lane in range(text_rows):
            self.track_header.create_text(8,45+lane*42,text=f"텍스트 {lane+1}",anchor="w",
                                          fill="#d6d6d6",font=("Malgun Gothic",9))
        self.track_header.yview_moveto(c.yview()[0])
        start = max(0, c.canvasx(0))
        end = min(full_w,c.canvasx(c.winfo_width()))
        step = next((s for s in (1, 2, 5, 10, 30, 60, 300, 600) if s*self.zoom >= 70), 1800)
        begin_s = max(0,int(start/self.zoom/step)*step)
        for sec in range(begin_s,min(math.ceil(max(0,end/self.zoom)), math.ceil(self.duration))+1,step):
            x = sec*self.zoom
            c.create_line(x,16,x,height,fill="#41464d")
            c.create_text(x+3,9,text=clock(sec)[:5],anchor="w",fill="#b8bdc5",font=("Segoe UI",8))
        if self.zoom >= 300:
            first_frame = max(0, int(start/self.zoom*core.FPS))
            last_frame = min(core.total_frames(self.duration), int(end/self.zoom*core.FPS)+1)
            for frame in range(first_frame, last_frame):
                if frame % core.FPS:
                    tick_x = self._frame_x(frame)
                    c.create_line(tick_x, 18, tick_x, 23, fill="#7d858f")
        if self.bins:
            left = max(0,int(start)); right = int(end)
            for px in range(left,right,2):
                b0 = max(0,int(px/self.zoom*100))
                b1 = min(len(self.bins),int((px+2)/self.zoom*100)+1)
                if b0 < b1:
                    peak = max(self.bins[b0:b1])
                    c.create_line(px,audio_y+29-peak*24,px,audio_y+29+peak*24,fill="#4bd5ad")
        cues = sorted(self.project["images"],key=lambda x:x["start"])
        last = core.total_frames(self.duration)
        if cues and cues[0]["start"] > 0:
            gap_end = self._frame_x(cues[0]["start"])
            c.create_rectangle(0,image_y+6,gap_end,image_y+52,fill="#15171b",outline="#4b5058")
            if gap_end > 86:
                c.create_text(6,image_y+29,text="검은 화면",anchor="w",fill="#afb5bf")
        for index, cue in enumerate(cues):
            next_frame = cues[index+1]["start"] if index+1<len(cues) else last
            x0,x1=self._frame_x(cue["start"]),self._frame_x(next_frame)
            if x1 < start or x0 > end: continue
            asset = next((a for a in self.project["assets"] if a["id"]==cue["asset"]),None)
            color="#4387ae" if self.selection == ("image",cue["id"]) else "#345d81"
            c.create_rectangle(x0,image_y+6,x1,image_y+52,fill=color,outline="#a9eaff" if self.selection==("image",cue["id"]) else "#6b8baa",tags=("image",cue["id"]))
            if x1-x0 > 26:
                name = Path(asset["path"]).name if asset else "누락"
                label_x = x0+6
                if asset and x1-x0 > 85:
                    try:
                        from PIL import Image, ImageOps
                        path = Path(asset["path"])
                        key = (str(path), path.stat().st_mtime_ns)
                        thumb = self.timeline_thumbnail_cache.get(key)
                        if thumb is None:
                            with Image.open(path) as source:
                                frame_image = ImageOps.exif_transpose(source).convert("RGB")
                                frame_image.thumbnail((48, 34))
                                thumb = ImageTk.PhotoImage(frame_image, master=self.root)
                            if len(self.timeline_thumbnail_cache)>180: self.timeline_thumbnail_cache.clear()
                            self.timeline_thumbnail_cache[key] = thumb
                        c.create_image(x0+5,image_y+29,anchor="w",image=thumb,tags=("image",cue["id"]))
                        label_x = x0+57
                    except OSError: pass
                chars = max(1, int((x1-label_x-6)/7))
                c.create_text(label_x,image_y+29,anchor="w",text=name[:chars-1]+"…" if len(name)>chars else name,
                              fill="white",tags=("image",cue["id"]))
        for lane, items in enumerate(lanes):
            for item in items:
                x0,x1=self._frame_x(item["start"]),self._frame_x(item["end"])
                if x1 < start or x0 > end: continue
                y0=27+lane*42
                c.create_rectangle(x0,y0,x1,y0+34,fill="#805680" if self.selection==("text",item["id"]) else "#604866",
                                   outline="#f2b9f3",tags=("text",item["id"]))
                if x1-x0 > 24:
                    name = item["text"].replace("\n", " ")
                    chars = max(1, int((x1-x0-10)/7))
                    c.create_text(x0+5,y0+17,anchor="w",text=name[:chars-1]+"…" if len(name)>chars else name,
                                  fill="white",tags=("text",item["id"]))
        x=self._frame_x(round(self.position*core.FPS))
        self.playhead_height = height
        self.playhead_line = c.create_line(x,16,x,height,fill="#ffb34d",width=2,tags=("playhead",))
        self.playhead_marker = c.create_polygon(x-6,16,x+6,16,x,27,fill="#ffb34d",tags=("playhead",))

    def _move_playhead_only(self):
        if not hasattr(self, "playhead_line"): return
        x = self._frame_x(round(self.position*core.FPS))
        self.timeline.coords(self.playhead_line, x, 16, x, self.playhead_height)
        self.timeline.coords(self.playhead_marker, x-6,16,x+6,16,x,27)
        left = self.timeline.canvasx(0)
        right = self.timeline.canvasx(self.timeline.winfo_width())
        if self.playing and x > right - 20:
            total = max(self.timeline.winfo_width(), self.duration*self.zoom+8)
            self.timeline.xview_moveto(max(0, x-self.timeline.winfo_width()*.25)/max(1,total))
            self.draw_timeline()

    def _text_lanes(self):
        lanes=[]
        for item in sorted(self.project["texts"],key=lambda t:(t["start"],t["end"])):
            for lane in lanes:
                if lane[-1]["end"] <= item["start"]:
                    lane.append(item); break
            else: lanes.append([item])
        return lanes

    def _timeline_down(self,e):
        c=self.timeline; x=c.canvasx(e.x); y=c.canvasy(e.y)
        hits=c.find_overlapping(x-1,y-1,x+1,y+1)
        chosen=None
        for ident in reversed(hits):
            tags=c.gettags(ident)
            if tags and tags[0] in ("image","text") and len(tags)>1:
                chosen=(tags[0],tags[1]); break
        if chosen and self._editable():
            self.selection=chosen; item=self._selected()
            edge="body"
            if chosen[0]=="text":
                if abs(x-self._frame_x(item["start"]))<8: edge="left"
                elif abs(x-self._frame_x(item["end"]))<8: edge="right"
            self.drag=(chosen,edge,item["start"],item.get("end"),x,False,self.dirty,None)
            self._show_properties(); self.render_preview(); self.draw_timeline()
        else:
            self.drag=("seek",None,None,None,x,False)
            self.seek(self._x_to_frame(x)/core.FPS)

    def _timeline_move(self,e):
        if not self.drag: return
        c=self.timeline; x=c.canvasx(e.x)
        if self.drag[0]=="seek":
            self.seek(self._x_to_frame(x)/core.FPS); return
        chosen,edge,original,end,begin,recorded=self.drag
        item=self._selected()
        if not item: return
        if not recorded and abs(x-begin) < 4: return
        delta=round((x-begin)/self.zoom*core.FPS)
        if self.snap and delta and self.zoom >= 1:
            candidate = original + delta
            edges = [round(self.position*core.FPS)]
            edges.extend(cue["start"] for cue in self.project["images"] if cue["id"] != chosen[1])
            for text in self.project["texts"]:
                if text["id"] != chosen[1]: edges.extend((text["start"], text["end"]))
            closest = min(edges, key=lambda edge_frame: abs(edge_frame-candidate), default=None)
            if closest is not None and abs(closest-candidate)*self.zoom/core.FPS <= 8:
                delta = closest-original
        if not recorded and delta:
            self._change(); recorded=True
        if not recorded: return
        total=core.total_frames(self.duration)
        if chosen[0]=="image":
            occupied={i["start"] for i in self.project["images"] if i["id"]!=item["id"]}
            candidate=max(0,min(total-1,original+delta))
            collision = candidate if candidate in occupied else None
            if collision is None: item["start"]=candidate
        elif edge=="left":
            item["start"]=max(0,min(item["end"]-1,original+delta))
        elif edge=="right":
            item["end"]=max(item["start"]+1,min(total,end+delta))
        else:
            length=end-original; candidate=max(0,min(total-length,original+delta))
            item["start"]=candidate; item["end"]=candidate+length
        self.drag=(chosen,edge,original,end,begin,recorded,self.drag[6],collision if chosen[0]=="image" else None)
        self.scene_cache_key = None
        self.draw_timeline(); self.render_preview()

    def _timeline_up(self,e):
        if self.drag and self.drag[0]!="seek" and self.drag[5]:
            chosen, _, _, _, _, _, was_dirty, collision = self.drag
            if chosen[0] == "image" and collision is not None:
                if messagebox.askyesno("이미지 교체", "이 시각의 이미지를 교체할까요?"):
                    item = self._selected()
                    other = next(c for c in self.project["images"] if c["start"] == collision and c["id"] != item["id"])
                    self.project["images"].remove(other)
                    item["start"] = collision
                elif self.undo:
                    self.project = self.undo.pop()
                    self.dirty = was_dirty
            self._refresh()
        self.drag=None

    def seek(self,seconds):
        self.position=max(0,min(self.duration,seconds))
        if self.playing:
            self._stop_audio()
            if self.play_thread: self.play_thread.join(timeout=1)
            self._start_audio()
        self.time_label.set(clock(self.position)+" / "+clock(self.duration))
        self.draw_timeline(); self.render_preview()

    def toggle_play(self):
        if self.playing: self._stop_audio()
        else: self._start_audio()

    def _set_play_button(self, playing):
        self.play_button.configure(text="일시정지" if playing else "▶ 재생",
                                   image=self.pause_icon if playing else "", compound="left")

    def _start_audio(self):
        if not self.pcm:
            self.status.set("음악 분석이 끝나면 재생할 수 있습니다."); return
        self.play_stop=threading.Event(); cancel=self.play_stop
        self.playing=True; self._set_play_button(True)
        start=min(self.duration,self.position)
        self.play_epoch=start
        def worker():
            try:
                import sounddevice as sd
                with wave.open(self.pcm,"rb") as w:
                    rate=w.getframerate(); w.setpos(min(w.getnframes(),int(start*rate)))
                    self.play_frames=0
                    with sd.RawOutputStream(samplerate=rate,channels=2,dtype="int16",
                                            blocksize=2048) as stream:
                        self.play_latency=stream.latency
                        while not cancel.is_set():
                            raw=w.readframes(2048)
                            if not raw: break
                            stream.write(raw)
                            self.play_frames+=len(raw)//4
                if not cancel.is_set(): self.events.put(("play_end",))
            except Exception as e:
                if not cancel.is_set(): self.events.put(("play_error","오디오 재생 실패: 장치와 연결을 확인하세요.\n"+str(e)))
        self.play_thread=threading.Thread(target=worker,daemon=True); self.play_thread.start()

    def _stop_audio(self):
        if self.playing:
            self.play_stop.set()
            self.playing=False
            self._set_play_button(False)

    def _tick(self):
        if self.playing:
            self.position=min(self.duration,self.play_epoch+max(0,self.play_frames/48000-self.play_latency))
            self.time_label.set(clock(self.position)+" / "+clock(self.duration))
            self._move_playhead_only(); self.render_preview()
        self.root.after(80,self._tick)

    def _show_job_controls(self, running=False, complete=False):
        if running:
            if not self.progressbar.winfo_manager(): self.progressbar.pack(side="left")
            if not self.cancel_button.winfo_manager(): self.cancel_button.pack(side="right")
        else:
            self.progressbar.pack_forget(); self.cancel_button.pack_forget()
        if complete:
            if not self.open_folder.winfo_manager(): self.open_folder.pack(side="right", padx=3)
            if not self.open_file.winfo_manager(): self.open_file.pack(side="right", padx=3)
        else:
            self.open_file.pack_forget(); self.open_folder.pack_forget()

    def start_export(self):
        if self.exporting or not self.duration:
            messagebox.showwarning("내보내기", "먼저 음악을 선택하고 분석이 끝날 때까지 기다리세요."); return
        if not self.project["images"] and not self.project.get("videos"):
            if not messagebox.askyesno("검은 화면", "이미지가 없습니다. 검은 화면과 텍스트로 영상을 만들까요?"): return
        if self.export_dialog and self.export_dialog.winfo_exists():
            self.export_dialog.lift(); return
        dialog = tk.Toplevel(self.root)
        dialog.title("MP4 내보내기")
        dialog.configure(bg=UI_COLORS["surface"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        self.export_dialog = dialog
        body = ttk.Frame(dialog, style="Panel.TFrame", padding=18)
        body.pack(fill="both", expand=True)
        self.export_heading = ttk.Label(body, text="MP4 내보내기", style="PanelTitle.TLabel")
        self.export_heading.pack(anchor="w", pady=(0, 12))
        default_folder = self.project_file.parent if self.project_file else Path.home() / "Videos"
        if self.last_export_folder and Path(self.last_export_folder).is_dir():
            default_folder = Path(self.last_export_folder)
        self.export_folder_var = tk.StringVar(value=str(default_folder))
        self.export_name_var = tk.StringVar(value=(self.project_file.stem if self.project_file else "음악 영상") + ".mp4")
        self.export_form = ttk.Frame(body, style="Panel.TFrame")
        self.export_form.pack(fill="x")
        ttk.Label(self.export_form, text="파일 이름").pack(anchor="w")
        name_entry = ttk.Entry(self.export_form, textvariable=self.export_name_var, width=48)
        name_entry.pack(fill="x", pady=(3, 9))
        ttk.Label(self.export_form, text="저장 폴더").pack(anchor="w")
        folder_row = ttk.Frame(self.export_form, style="Panel.TFrame"); folder_row.pack(fill="x", pady=(3, 10))
        ttk.Entry(folder_row, textvariable=self.export_folder_var).pack(side="left", fill="x", expand=True)
        ttk.Button(folder_row, text="찾기…", command=self._browse_export_folder).pack(side="right", padx=(6, 0))
        ttk.Label(self.export_form, text=f"{clock(self.duration)}  ·  1920×1080  ·  30fps  ·  H.264 / AAC",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 12))
        self.export_result_frame = ttk.Frame(body, style="Result.TFrame", padding=12)
        self.export_result_name = ttk.Label(self.export_result_frame, style="ResultTitle.TLabel", wraplength=320)
        self.export_result_name.pack(anchor="w", pady=(0, 8))
        self.export_result_details = ttk.Label(self.export_result_frame, style="ResultMuted.TLabel", wraplength=320)
        self.export_result_details.pack(anchor="w", pady=(0, 8))
        self.export_result_path = ttk.Label(self.export_result_frame, style="ResultMuted.TLabel", wraplength=320)
        self.export_result_path.pack(anchor="w", pady=(0, 8))
        self.export_status_label = ttk.Label(body, text="저장 위치를 확인하세요.", style="Muted.TLabel")
        self.export_status_label.pack(anchor="w", pady=(0, 4))
        self.export_dialog_progress = ttk.Progressbar(body, maximum=100, variable=self.progress)
        self.export_dialog_progress.pack(fill="x", pady=4)
        buttons = ttk.Frame(body, style="Panel.TFrame"); buttons.pack(fill="x", pady=(10, 0))
        self.export_start_button = ttk.Button(buttons, text="영상 만들기", style="Primary.TButton",
                                              command=self._export_from_dialog)
        self.export_start_button.pack(side="right")
        self.export_cancel_button = ttk.Button(buttons, text="닫기", command=self._close_export_dialog)
        self.export_cancel_button.pack(side="right", padx=7)
        self.export_result_button = ttk.Button(buttons, text="결과 열기", style="Primary.TButton",
                                               command=self.open_result)
        self.export_folder_button = ttk.Button(buttons, text="폴더 열기", command=self.open_result_folder)
        self.export_copy_button = ttk.Button(buttons, text="경로 복사", command=self._copy_export_path)
        dialog.protocol("WM_DELETE_WINDOW", self._close_export_dialog)
        dialog.grab_set()
        name_entry.focus_set()

    def _browse_export_folder(self):
        path = filedialog.askdirectory(parent=self.export_dialog, initialdir=self.export_folder_var.get())
        if path:
            self.export_folder_var.set(path)
            self._remember_export_folder(path)

    def _remember_export_folder(self, folder):
        if Path(folder).is_dir():
            self.last_export_folder = str(Path(folder).resolve())
            self._save_ui_settings()

    def _close_export_dialog(self):
        if self.exporting:
            self.export_cancel.set()
            self.export_status_label.configure(text="취소 중…")
            return
        if self.export_dialog and self.export_dialog.winfo_exists(): self.export_dialog.destroy()
        self.export_dialog = None

    def _show_export_result(self, path):
        result = Path(path)
        if not self.export_dialog or not self.export_dialog.winfo_exists(): return
        size = result.stat().st_size if result.is_file() else 0
        self.export_heading.configure(text="MP4 생성 완료")
        self.export_form.pack_forget()
        self.export_dialog_progress.pack_forget()
        self.export_result_name.configure(text=result.name)
        self.export_result_details.configure(
            text=f"길이 {clock(self.duration)}  ·  파일 크기 {size / 1048576:.1f} MB\n1920×1080  ·  30fps")
        self.export_result_path.configure(text=str(result))
        self.export_result_frame.pack(fill="x", pady=(0, 10), before=self.export_status_label)
        self.export_status_label.configure(text="영상이 저장되었습니다.", style="Success.TLabel")
        self.export_start_button.pack_forget()
        self.export_cancel_button.configure(text="닫기")
        self.export_folder_button.pack(side="left", padx=(7, 0))
        self.export_result_button.pack(side="left", padx=(7, 0))
        self.export_copy_button.pack(side="left", padx=(7, 0))

    def _copy_export_path(self):
        if not self.last_output: return
        self.root.clipboard_clear()
        self.root.clipboard_append(str(self.last_output))
        self.export_status_label.configure(text="저장 경로를 복사했습니다.")

    def _export_from_dialog(self):
        name = self.export_name_var.get().strip()
        if not name or Path(name).name != name or name in (".", ".."):
            self.export_status_label.configure(text="파일 이름만 입력하세요."); return
        if not name.lower().endswith(".mp4"): name += ".mp4"
        path = str(Path(self.export_folder_var.get()) / name)
        if not Path(self.export_folder_var.get()).is_dir():
            self.export_status_label.configure(text="저장 폴더를 찾을 수 없습니다."); return
        self._remember_export_folder(self.export_folder_var.get())
        self._begin_export(path)

    def _begin_export(self, path):
        if Path(path).exists():
            if self.export_dialog and self.export_dialog.winfo_exists():
                self.export_status_label.configure(text="같은 이름의 파일이 있습니다. 다른 이름을 입력하세요.")
            else: messagebox.showerror("파일 충돌","같은 이름의 파일이 이미 있습니다. 새 이름을 입력하세요.")
            return
        try: ffmpeg=self.tool("ffmpeg")
        except core.EditorError as e: messagebox.showerror("내보내기",str(e)); return
        self._stop_audio()
        self.exporting=True; self.export_cancel=threading.Event(); self.progress.set(0)
        self.progressbar.stop(); self.progressbar.configure(mode="determinate")
        self.cancel_button.configure(state="normal")
        self._show_job_controls(running=True)
        self.status.set("MP4 내보내는 중…")
        if self.export_dialog and self.export_dialog.winfo_exists():
            self.export_status_label.configure(text="MP4 만드는 중…")
            self.export_start_button.configure(state="disabled")
            self.export_cancel_button.configure(text="작업 취소")
        snapshot=copy.deepcopy(self.project); duration=self.duration; cancel=self.export_cancel
        def worker():
            try:
                result=core.export_video(snapshot,duration,path,ffmpeg,cancel,
                                         lambda n:self.events.put(("progress",n)))
                self.events.put(("export_done",result))
            except Exception as e: self.events.put(("export_error",str(e)))
        self.export_thread = threading.Thread(target=worker,daemon=True)
        self.export_thread.start()

    def cancel_job(self):
        if self.exporting: self.export_cancel.set()
        else: self.audio_cancel.set()
        self.status.set("작업 취소 중…")
        self.cancel_button.configure(state="disabled")

    def open_result(self):
        if self.last_output and Path(self.last_output).is_file(): os.startfile(str(self.last_output))

    def open_result_folder(self):
        if self.last_output: os.startfile(str(Path(self.last_output).parent))

    def _poll(self):
        try:
            while True:
                event=self.events.get_nowait(); kind=event[0]
                if kind=="estimate" and event[1] == self.audio_generation:
                    self.duration=event[2]; self._refresh(False)
                elif kind=="audio":
                    _,generation,path,pcm,bins,duration=event
                    if generation==self.audio_generation and path==self.project["audio"]:
                        self._dispose_pcm(); self.pcm=pcm; self.bins=bins; self.duration=duration
                        self._loaded_audio=path
                        self.status.set("파형 분석 완료. 이미지와 텍스트를 배치하세요.")
                        self.progressbar.stop(); self.cancel_button.configure(state="disabled"); self._show_job_controls(); self._refresh()
                    else: Path(pcm).unlink(missing_ok=True)
                elif kind=="error" and event[1]==self.audio_generation:
                    self._stop_audio(); self.progressbar.stop(); self.cancel_button.configure(state="disabled")
                    self._show_job_controls()
                    self.status.set(event[2].splitlines()[0]); messagebox.showerror("작업 오류",event[2])
                elif kind=="play_error":
                    self._stop_audio(); self.status.set(event[1].splitlines()[0])
                    messagebox.showerror("재생 오류",event[1])
                elif kind=="progress":
                    self.progress.set(event[1]); self.status.set(f"MP4 내보내는 중… {event[1]}%")
                    if self.export_dialog and self.export_dialog.winfo_exists():
                        self.export_status_label.configure(text=f"MP4 만드는 중… {event[1]}%")
                elif kind=="export_done":
                    self.exporting=False; self.last_output=event[1]; self.progress.set(100)
                    self.cancel_button.configure(state="disabled")
                    self.open_file.configure(state="normal"); self.open_folder.configure(state="normal")
                    self._show_job_controls(complete=True)
                    self.status.set("완료: "+str(event[1]))
                    if self.export_dialog and self.export_dialog.winfo_exists():
                        self._show_export_result(event[1])
                    else: messagebox.showinfo("완료","MP4를 저장했습니다.\n"+str(event[1]))
                elif kind=="export_error":
                    self.exporting=False; self.cancel_button.configure(state="disabled")
                    self._show_job_controls()
                    self.status.set(event[1].splitlines()[0])
                    if self.export_dialog and self.export_dialog.winfo_exists():
                        self.export_status_label.configure(text=event[1].splitlines()[0])
                        self.export_start_button.configure(state="normal")
                        self.export_cancel_button.configure(text="닫기")
                    elif "취소" not in event[1]: messagebox.showerror("변환 오류",event[1])
                elif kind=="play_end":
                    self._stop_audio(); self.position=self.duration; self._refresh(False)
        except queue.Empty: pass
        self.root.after(80,self._poll)

    def close(self):
        if self.exporting:
            self.export_cancel.set()
            self.status.set("변환 프로세스를 종료한 뒤 창을 닫습니다…")
            self.root.after(100,self.close)
            return
        if not self._confirm_dirty(): return
        self._save_ui_settings()
        self._stop_audio(); self.audio_cancel.set()
        if self.audio_thread: self.audio_thread.join(timeout=3)
        if self.play_thread: self.play_thread.join(timeout=2)
        self._dispose_pcm(); self.root.destroy()
