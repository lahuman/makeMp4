"""Clip-based Tk editor. All UI changes stay on Tk's thread."""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import ImageTk

import editor_core as core
from editor_ui import EditorApp as BaseEditor, clock


class EditorApp(BaseEditor):
    def __init__(self, root):
        self.audio_cache = {}
        self.audio_generation_by_id = {}
        self.audio_token = 0
        self.library_drag = None
        self.text_draft = None
        self.revision = 0
        self.image_mode = tk.BooleanVar(value=False)
        self.motion_key = tk.StringVar(value="from")
        super().__init__(root)
        self.drag = None
        self.pcm = None
        self.bins = []
        self.audio_list = ttk.Frame(self.left, style="Panel.TFrame")
        self.audio_list.pack(fill="x", after=self.audio_info, padx=8, pady=(0, 5))
        self.audio_rows = {}
        self.text_source = ttk.Label(self.timeline.master.master.master.winfo_children()[0],
                                     text="▣ 새 텍스트  ↘", cursor="hand2")
        self.text_source.pack(side="left", padx=6)
        self.text_source.bind("<ButtonPress-1>", lambda e: self._library_start("text", None, e))
        self.preview.bind("<Alt-MouseWheel>", self._image_wheel)
        self.timeline.bind("<Motion>", self._timeline_hover)
        self.root.bind_all("<B1-Motion>", self._library_motion_anywhere, add="+")
        self.root.bind("<FocusOut>", self._window_focus_out, add="+")
        self.status.set("음원과 이미지를 등록하세요. 목록에서 타임라인으로 끌어 놓을 수 있습니다.")
        self._refresh()

    def _window_focus_out(self, event):
        if event.widget is self.root:
            self.root.after_idle(self._cancel_on_focus_loss)

    def _cancel_on_focus_loss(self):
        if self.root.winfo_exists() and self.root.focus_displayof() is None and (self.drag or self.library_drag):
            self.drag = None; self.library_drag = None; self.draw_timeline()

    def _editable(self):
        return super()._editable()

    def _change(self):
        super()._change()
        self.revision += 1

    def _duration_refresh(self):
        self.duration = core.duration_seconds(self.project)
        self.position = min(self.position, self.duration)

    def _refresh(self, properties=True):
        self._duration_refresh()
        signature = (tuple((a["id"], a["path"]) for a in self.project["assets"]),
                     self.project_search.get(), self.library_view.get())
        if signature != self._library_signature: self._refresh_library()
        self._refresh_audio_list()
        self.draw_timeline(); self.render_preview()
        if properties: self._show_properties()
        self.duration_label.configure(text="영상 " + clock(self.duration))
        self.audio_info.configure(text=f"음원 {len(self.project['audio_assets'])}개 · 클립 {len(self.project['audio_clips'])}개")
        self.time_label.set(clock(self.position) + " / " + clock(self.duration))
        self.root.title(("● " if self.dirty else "") + "음악 파형 슬라이드 편집기")

    def _refresh_audio_list(self):
        for child in self.audio_list.winfo_children(): child.destroy()
        self.audio_rows.clear()
        for asset in self.project["audio_assets"]:
            ready = asset["id"] in self.audio_cache
            state = "✓" if ready else "…"
            label = ttk.Label(self.audio_list, text=f"♫ {Path(asset['path']).name}  {state}",
                              cursor="hand2", wraplength=220)
            label.pack(fill="x", pady=2)
            label.bind("<ButtonPress-1>", lambda e, ident=asset["id"]: self._library_start("audio", ident, e))
            self.audio_rows[asset["id"]] = label

    def _commit_text_draft(self):
        draft = self.text_draft
        if not draft: return
        ident, widget = draft
        try: value = widget.get("1.0", "end-1c")
        except tk.TclError: self.text_draft = None; return
        item = next((t for t in self.project["texts"] if t["id"] == ident), None)
        if item and item["text"] != value and self._editable():
            self._change(); item["text"] = value
            self.render_preview(); self.draw_timeline()

    def _prop_text(self, item, parent=None):
        parent = parent or self.properties
        ttk.Label(parent, text="문구 (여러 줄)").pack(anchor="w")
        box = tk.Text(parent, height=4, wrap="word", font=("Malgun Gothic", 10),
                      bg="#30333a", fg="#e8eaed", insertbackground="white", relief="flat")
        box.insert("1.0", item["text"]); box.pack(fill="x")
        self.text_draft = (item["id"], box)
        box.bind("<FocusOut>", lambda e: self.root.after_idle(self._commit_text_draft))
        box.bind("<Control-Return>", lambda e: (self._commit_text_draft(), "break"))

    def _show_properties(self):
        self._commit_text_draft()
        item = self._selected()
        if not item or self.selection[0] == "text":
            return super()._show_properties()
        for child in self.properties.winfo_children(): child.destroy()
        kind = self.selection[0]
        if kind == "audio":
            asset = next((a for a in self.project["audio_assets"] if a["id"] == item["asset"]), None)
            ttk.Label(self.properties, text=Path(asset["path"]).name if asset else "음원 없음",
                      style="PanelTitle.TLabel", wraplength=250).pack(anchor="w")
            self._prop_entry("타임라인 시작 (초)", f"{item['start_sample']/core.RATE:.3f}", "start_seconds")
            self._prop_entry("원본 시작 (초)", f"{item['source_in']/core.RATE:.3f}", "source_in_seconds")
            self._prop_entry("원본 끝 (초)", f"{item['source_out']/core.RATE:.3f}", "source_out_seconds")
            return
        asset = next((a for a in self.project["assets"] if a["id"] == item["asset"]), None)
        ttk.Label(self.properties, text=Path(asset["path"]).name if asset else "이미지 없음",
                  style="PanelTitle.TLabel", wraplength=250).pack(anchor="w")
        self._prop_entry("시작 (초)", f"{item['start']/core.FPS:.3f}", "start_seconds")
        self._prop_entry("끝 (초)", f"{item['end']/core.FPS:.3f}", "end_seconds")
        ttk.Checkbutton(self.properties, text="미리보기에서 이미지 이동", variable=self.image_mode).pack(anchor="w", pady=5)
        ttk.Button(self.properties, text="화면 맞춤", command=lambda: self._image_preset("fit")).pack(fill="x")
        ttk.Button(self.properties, text="화면 채움", command=lambda: self._image_preset("fill")).pack(fill="x")
        ttk.Button(self.properties, text="가운데 정렬", command=lambda: self._image_preset("center")).pack(fill="x")
        ttk.Button(self.properties, text="초기화", command=lambda: self._image_preset("reset")).pack(fill="x")
        motion = tk.BooleanVar(value=item.get("motion", False))
        ttk.Checkbutton(self.properties, text="시작→끝 움직임", variable=motion,
                        command=lambda: self._commit("motion", motion.get(), item["id"])).pack(anchor="w", pady=5)
        ttk.Label(self.properties, text="편집할 상태").pack(anchor="w")
        state_combo = ttk.Combobox(self.properties, values=("from", "to"), textvariable=self.motion_key,
                                   state="readonly")
        state_combo.pack(fill="x")
        state_combo.bind("<<ComboboxSelected>>", lambda e: self._show_properties())
        state = item[self.motion_key.get()]
        for label, key in (("가로 중심", "image_x"), ("세로 중심", "image_y"), ("배율 %", "image_zoom")):
            self._prop_entry(label, str(round(state[key[6:]])), key)
        ttk.Label(self.properties, text="움직임 속도").pack(anchor="w", pady=(6, 0))
        ease = tk.StringVar(value=item.get("easing", "smooth"))
        combo = ttk.Combobox(self.properties, values=("smooth", "linear"), textvariable=ease, state="readonly")
        combo.pack(fill="x")
        combo.bind("<<ComboboxSelected>>", lambda e: self._commit("easing", ease.get(), item["id"]))
        ttk.Label(self.properties, text="이미지 전환").pack(anchor="w", pady=(6, 0))
        effects = {"cut":"즉시 전환", "dissolve":"크로스 디졸브", "fade_black":"검정 페이드", "slide":"좌우 슬라이드"}
        current = tk.StringVar(value=effects.get(item.get("transition", {}).get("type", "cut"), "즉시 전환"))
        combo = ttk.Combobox(self.properties, values=list(effects.values()), textvariable=current, state="readonly")
        combo.pack(fill="x")
        combo.bind("<<ComboboxSelected>>", lambda e: self._commit("transition_type",
                    next(k for k,v in effects.items() if v == current.get()), item["id"]))
        self._prop_entry("전환 길이 (초)", f"{item['transition']['frames']/core.FPS:.3f}", "transition_seconds")
        effect, length, previous = core.transition_info(self.project, item)
        if item["transition"]["type"] != "cut" and effect == "cut":
            ttk.Label(self.properties, text="앞 이미지와 맞닿을 때 전환이 적용됩니다.",
                      style="Muted.TLabel", wraplength=245).pack(anchor="w")

    def _toggle_property_group(self, key):
        self._commit_text_draft()
        super()._toggle_property_group(key)

    def _commit(self, key, raw, selected_id=None):
        self._commit_text_draft()
        if key == "text": return
        item = self._selected()
        if not item or (selected_id and item["id"] != selected_id) or not self._editable(): return
        kind = self.selection[0]
        if kind == "text":
            if key in ("start_seconds", "end_seconds"):
                try:
                    field="start" if key=="start_seconds" else "end"
                    value=max(0,round(float(raw)*core.FPS))
                    if (field=="start" and value>=item["end"]) or (field=="end" and value<=item["start"]):
                        raise ValueError("끝 시각은 시작 시각보다 뒤여야 합니다.")
                    if item[field]!=value:
                        self._change(); item[field]=value; self._refresh()
                except ValueError as e: messagebox.showerror("입력 오류",str(e))
                return
            return super()._commit(key, raw, selected_id)
        trial = copy.deepcopy(item)
        try:
            if kind == "audio":
                asset = next(a for a in self.project["audio_assets"] if a["id"] == item["asset"])
                value = round(float(raw) * core.RATE)
                if key == "start_seconds": trial["start_sample"] = max(0, value)
                elif key == "source_in_seconds": trial["source_in"] = max(0, value)
                elif key == "source_out_seconds": trial["source_out"] = min(asset.get("samples", 0), value)
                else: return
                if not core.valid_audio(self.project, trial, item["id"]): raise ValueError("음원끼리 겹치거나 사용 구간이 잘못되었습니다.")
            else:
                if key in ("start_seconds", "end_seconds"):
                    trial["start" if key == "start_seconds" else "end"] = max(0, round(float(raw) * core.FPS))
                    if not core.valid_interval(self.project["images"], trial["start"], trial["end"], item["id"]):
                        raise ValueError("이미지 클립이 겹치거나 길이가 0입니다.")
                elif key in ("image_x", "image_y", "image_zoom"):
                    field = key[6:]
                    trial[self.motion_key.get()][field] = max(10, min(500, float(raw))) if field == "zoom" else float(raw)
                elif key == "transition_type": trial["transition"]["type"] = raw
                elif key == "transition_seconds": trial["transition"]["frames"] = max(1, round(float(raw) * core.FPS))
                elif key in ("motion", "easing"): trial[key] = raw
                else: return
            if trial != item:
                self._change(); item.update(trial); self._refresh()
        except (ValueError, StopIteration) as e:
            messagebox.showerror("입력 오류", "시간과 겹침을 확인하세요.\n" + str(e))

    def _selected(self):
        if not self.selection: return None
        kind, ident = self.selection
        table = {"image": self.project["images"], "audio": self.project["audio_clips"],
                 "text": self.project["texts"]}
        return next((item for item in table[kind] if item["id"] == ident), None)

    def _image_preset(self, preset):
        item = self._selected()
        if not item or self.selection[0] != "image": return
        trial = copy.deepcopy(item)
        state = trial[self.motion_key.get()]
        if preset in ("fit", "reset"):
            state.update(x=960, y=540, zoom=100)
            trial.pop("legacy_native", None)
        elif preset == "center": state.update(x=960, y=540)
        else:
            asset = next(a for a in self.project["assets"] if a["id"] == item["asset"])
            from PIL import Image, ImageOps
            with Image.open(asset["path"]) as source:
                image = ImageOps.exif_transpose(source)
                ratio = max(core.SIZE[0] / image.width, core.SIZE[1] / image.height)
                fit = min(core.SIZE[0] / image.width, core.SIZE[1] / image.height)
                state.update(x=960, y=540, zoom=min(500, ratio / fit * 100))
                trial.pop("legacy_native", None)
        if preset == "reset":
            trial["from"] = dict(state); trial["to"] = dict(state); trial["motion"] = False
        if trial != item:
            self._change(); item.update(trial); self._refresh()

    def _image_wheel(self, event):
        item = self._selected()
        if not item or self.selection[0] != "image" or not self.image_mode.get(): return
        key = self.motion_key.get()
        value = item[key]["zoom"] * (1.08 if event.delta > 0 else 1/1.08)
        self._commit("image_zoom", value, item["id"])
        return "break"

    def _dispose_pcm(self):
        if self.play_thread and self.play_thread.is_alive():
            self.play_stop.set(); self.play_thread.join(timeout=2)
        self.audio_cache.clear()

    def choose_audio(self):
        if not self._editable(): return
        paths = filedialog.askopenfilenames(filetypes=[("음악", "*.flac *.wav *.mp3")])
        if not paths: return
        self._commit_text_draft()
        known = {str(Path(a["path"]).resolve()).casefold() for a in self.project["audio_assets"]}
        added = []
        for path in paths:
            if str(Path(path).resolve()).casefold() in known: continue
            if Path(path).suffix.lower() not in core.AUDIO_EXTS or not Path(path).is_file():
                messagebox.showerror("음원 오류", "FLAC/WAV/MP3 파일을 선택하세요."); continue
            added.append({"id": core.uid(), "path": path, "samples": 0})
            known.add(str(Path(path).resolve()).casefold())
        if not added: return
        self._change(); self.project["audio_assets"].extend(added)
        for asset in added: self._analyze_asset(asset)
        self._refresh()

    def _analyze_asset(self, asset):
        if self.audio_cancel.is_set(): self.audio_cancel = threading.Event()
        self.audio_token += 1
        token = self.audio_token
        self.audio_generation_by_id[asset["id"]] = token
        cancel = self.audio_cancel
        self.cancel_button.configure(state="normal")
        self.progressbar.configure(mode="indeterminate"); self.progressbar.start(12)
        self._show_job_controls(running=True)
        self.status.set("음원을 분석하는 중… " + Path(asset["path"]).name)
        def worker():
            try:
                core.probe_audio(asset["path"], self.tool("ffprobe"))
                pcm, bins, samples = core.analyze_audio_asset(asset["path"], self.tool("ffmpeg"), cancel)
                self.events.put(("audio_ready_v2", asset["id"], token, pcm, bins, samples))
            except Exception as e:
                self.events.put(("audio_cancel_v2" if cancel.is_set() else "audio_error_v2",
                                 asset["id"], token, str(e)))
        self.audio_thread = threading.Thread(target=worker, daemon=True)
        self.audio_thread.start()

    def _recheck_audio(self):
        for asset in self.project["audio_assets"]:
            if asset["id"] not in self.audio_cache:
                self._analyze_asset(asset)

    def add_assets(self):
        if not self._editable(): return
        paths = filedialog.askopenfilenames(filetypes=[("이미지", "*.jpg *.jpeg *.png")])
        if not paths: return
        valid = []
        from PIL import Image
        for path in paths:
            try:
                with Image.open(path) as image: image.verify()
                valid.append(path)
            except Exception: messagebox.showwarning("이미지 제외", "읽을 수 없는 이미지: " + path)
        known = {str(Path(a["path"]).resolve()).casefold() for a in self.project["assets"]}
        fresh = [p for p in valid if str(Path(p).resolve()).casefold() not in known]
        if not fresh: return
        self._change()
        self.project["assets"].extend({"id": core.uid(), "path": p} for p in fresh)
        self.status.set(f"이미지 {len(fresh)}개를 등록했습니다.")
        self._refresh()

    def _library_down(self, ident):
        self._commit_text_draft()
        self.library_selection = ident
        self._library_start("image", ident, None)
        self._refresh_library()
        self._show_properties()

    def _library_start(self, kind, ident, event):
        self._commit_text_draft()
        self._stop_audio()
        self.library_drag = {"kind": kind, "id": ident,
                             "x": event.x_root if event else self.root.winfo_pointerx(),
                             "y": event.y_root if event else self.root.winfo_pointery(),
                             "active": False, "valid": False}

    def _library_motion_anywhere(self, event):
        drag = self.library_drag
        if not drag: return
        dx = event.x_root - drag["x"]; dy = event.y_root - drag["y"]
        if not drag["active"] and max(abs(dx), abs(dy)) < 4: return
        drag["active"] = True
        t = self.timeline
        x = event.x_root - t.winfo_rootx(); y = event.y_root - t.winfo_rooty()
        if 0 <= x < t.winfo_width() and 0 <= y < t.winfo_height():
            if x < 30: t.xview_scroll(-1,"units")
            elif x > t.winfo_width()-30: t.xview_scroll(1,"units")
            if y < 30: t.yview_scroll(-1,"units")
            elif y > t.winfo_height()-30: t.yview_scroll(1,"units")
            drag["frame"] = self._x_to_frame(t.canvasx(x))
            drag["valid"] = self._track_at(t.canvasy(y)) == drag["kind"] and (
                drag["kind"] == "audio" or self.duration > 0)
            frame = drag["frame"]
            if drag["kind"] == "audio":
                asset = next((a for a in self.project["audio_assets"] if a["id"] == drag["id"]), None)
                if asset and drag["id"] in self.audio_cache:
                    drag["trial"] = {"start_sample": round(frame*core.RATE/core.FPS),
                                     "source_in": 0, "source_out": asset["samples"]}
                    drag["valid"] = drag["valid"] and core.valid_audio(self.project, drag["trial"] | {"id":""})
                else: drag["valid"] = False
            elif drag["kind"] == "image":
                next_start=min((c["start"] for c in self.project["images"] if c["start"]>frame),
                               default=core.total_frames(self.duration))
                drag["trial"] = {"start": frame, "end": min(core.total_frames(self.duration),
                                                             frame+150,next_start)}
                drag["valid"] = drag["valid"] and core.valid_interval(self.project["images"],
                                      drag["trial"]["start"],drag["trial"]["end"])
            else:
                drag["trial"] = {"start":frame,"end":min(core.total_frames(self.duration),frame+90)}
                drag["valid"] = drag["valid"] and drag["trial"]["end"]>frame
        else: drag["valid"] = False
        self.draw_timeline()

    def _library_drop_anywhere(self, event):
        drag = self.library_drag
        self.library_drag = None
        if not drag or not drag["active"] or not drag["valid"]: self.draw_timeline(); return
        frame = drag["frame"]
        if drag["kind"] == "image": self.place_asset(drag["id"], frame)
        elif drag["kind"] == "audio": self.place_audio(drag["id"], frame)
        else: self.add_text(frame)

    def add_selected_asset(self):
        if self.library_selection and self.duration:
            self.place_asset(self.library_selection, round(self.position * core.FPS))

    def place_asset(self, asset_id, frame):
        if not self._editable() or not self.duration: return
        start = max(0, int(frame)); end = min(core.total_frames(self.duration), start + 5 * core.FPS)
        next_start = min((c["start"] for c in self.project["images"] if c["start"] > start), default=end)
        end = min(end, next_start)
        if not core.valid_interval(self.project["images"], start, end):
            self.status.set("이미지 클립이 겹칩니다. 빈 구간에 놓으세요."); return
        self._commit_text_draft(); self._change()
        clip = core.image_defaults(asset_id, start, end)
        self.project["images"].append(clip)
        self.selection = ("image", clip["id"]); self.seek(start / core.FPS); self._refresh()

    def place_audio(self, asset_id, frame):
        if not self._editable(): return
        asset = next((a for a in self.project["audio_assets"] if a["id"] == asset_id), None)
        if not asset or asset_id not in self.audio_cache:
            self.status.set("음원 분석이 끝난 뒤 배치할 수 있습니다."); return
        clip = {"id": core.uid(), "asset": asset_id,
                "start_sample": max(0, int(frame * core.RATE / core.FPS)),
                "source_in": 0, "source_out": int(asset["samples"])}
        if not core.valid_audio(self.project, clip):
            self.status.set("음원 클립이 겹칩니다. 빈 구간에 놓으세요."); return
        self._commit_text_draft(); self._change()
        self.project["audio_clips"].append(clip)
        self.selection = ("audio", clip["id"]); self._refresh()

    def add_text(self, frame=None):
        if not self._editable() or not self.duration: return
        self._commit_text_draft()
        start = max(0, round(self.position * core.FPS) if frame is None else int(frame))
        end = min(core.total_frames(self.duration), start + 3 * core.FPS)
        if end <= start: return
        self._change()
        item = {"id": core.uid(), "start": start, "end": end, "text": "텍스트",
                "x": 960, "y": 840, "width": 1200, "size": 64, "font": "malgun.ttf",
                "color": "#ffffff", "background": "#000000", "background_alpha": 160,
                "outline": 2, "align": "center",
                "order": max((t.get("order", 0) for t in self.project["texts"]), default=0) + 1}
        self.project["texts"].append(item); self.selection = ("text", item["id"]); self._refresh()

    def delete_selected(self):
        self._commit_text_draft()
        item = self._selected()
        if not item or not self._editable(): return
        collection = {"audio": self.project["audio_clips"], "image": self.project["images"],
                      "text": self.project["texts"]}[self.selection[0]]
        self._change(); collection.remove(item); self.selection = None; self._refresh()

    def duplicate_selected(self):
        self._commit_text_draft()
        item = self._selected()
        if not item or not self._editable(): return
        kind = self.selection[0]
        trial = copy.deepcopy(item); trial["id"] = core.uid()
        if kind == "audio":
            trial["start_sample"] = core.audio_end(item)
            valid = core.valid_audio(self.project, trial)
            collection = self.project["audio_clips"]
        else:
            duration = item["end"] - item["start"]
            trial["start"] = item["end"]; trial["end"] = item["end"] + duration
            collection = self.project["images"] if kind == "image" else self.project["texts"]
            valid = kind == "text" or core.valid_interval(collection, trial["start"], trial["end"])
            if kind == "text": trial["order"] = max((t.get("order",0) for t in collection), default=0)+1
        if not valid:
            self.status.set("복제할 빈 구간이 없습니다."); return
        self._change(); collection.append(trial); self.selection = (kind, trial["id"]); self._refresh()

    def undo_action(self):
        self._commit_text_draft()
        if not self.undo or not self._editable(): return
        self.redo.append(copy.deepcopy(self.project)); self.project = self.undo.pop()
        self.dirty = True; self.selection = None; self.revision += 1; self._refresh(); self._recheck_audio()

    def redo_action(self):
        self._commit_text_draft()
        if not self.redo or not self._editable(): return
        self.undo.append(copy.deepcopy(self.project)); self.project = self.redo.pop()
        self.dirty = True; self.selection = None; self.revision += 1; self._refresh(); self._recheck_audio()

    def save_project(self):
        self._commit_text_draft()
        if self.project.get("_migrated") and self.project_file == getattr(self,"_legacy_project_file",None):
            return self.save_as()
        return super().save_project()

    def save_as(self):
        self._commit_text_draft()
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("프로젝트 JSON", "*.json")],
                 initialfile=(self.project_file.stem + "_v2.json" if self.project_file and self.project.get("_migrated") else None))
        if not path: return False
        previous = self.project_file; self.project_file = Path(path)
        if self.save_project(): self.project.pop("_migrated", None); return True
        self.project_file = previous; return False

    def new_project(self):
        self._commit_text_draft()
        if not self._editable() or not self._confirm_dirty(): return
        self._stop_audio(); self.audio_cancel.set(); self._dispose_pcm()
        self.audio_cancel = threading.Event()
        self.project = core.fresh(); self.project_file = None; self.duration = 0; self.position = 0
        self.undo.clear(); self.redo.clear(); self.selection = None; self.library_selection = None
        self.dirty = False; self.revision += 1; self.scene_cache_key = None
        self._refresh()

    def open_project(self):
        self._commit_text_draft()
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
                collection = data["audio_assets"] if kind == "audio" else data["assets"]
                next(a for a in collection if a["id"] == asset_id)["path"] = replacement
            self._stop_audio(); self.audio_cancel.set(); self._dispose_pcm()
            self.audio_cancel = threading.Event()
            self.project = data; self.project_file = Path(path)
            self._legacy_project_file = Path(path) if data.get("_migrated") else None
            self.selection = None; self.library_selection = None
            self.undo.clear(); self.redo.clear(); self.dirty = bool(missing or data.get("_migrated"))
            self.position = 0; self.revision += 1; self.scene_cache_key = None
            if data.get("_migrated") and data["audio_assets"]:
                asset = data["audio_assets"][0]
                duration = core.probe_audio(asset["path"], self.tool("ffprobe"))
                asset["samples"] = round(duration * core.RATE)
                data["audio_clips"][0]["source_out"] = asset["samples"]
                end = core.total_frames(duration)
                cues = sorted(data["images"], key=lambda c: c["start"])
                if cues: cues[-1]["end"] = max(cues[-1]["start"] + 1, end)
            self._refresh(); self._recheck_audio()
            if data.get("_migrated"): self.status.set("v1 프로젝트를 변환했습니다. 저장 시 _v2 이름을 사용하세요.")
        except core.EditorError as e: messagebox.showerror("열기 오류", str(e))

    def _x_to_frame(self, x):
        return max(0, round(max(0, x) / self.zoom * core.FPS))

    def _track_at(self, y):
        if y < 24: return "seek"
        if y < self.image_y: return "text"
        if y < self.audio_y: return "image"
        if y < self.audio_y + 64: return "audio"
        return None

    def draw_timeline(self):
        c = self.timeline
        if not c.winfo_exists(): return
        c.delete("all")
        lanes = self._text_lanes()
        rows = max(1, len(lanes))
        self.image_y = 24 + rows * 42
        self.audio_y = self.image_y + 58
        height = max(c.winfo_height(), self.audio_y + 64)
        latest = max([self.duration] + [i["end"] / core.FPS for i in self.project["images"]]
                     + [t["end"] / core.FPS for t in self.project["texts"]])
        for drag in (self.drag,self.library_drag):
            if drag and drag.get("trial"):
                trial=drag["trial"]
                latest=max(latest,(core.audio_end(trial)/core.RATE if drag["kind"]=="audio"
                                   else trial["end"]/core.FPS))
        full_w = max(c.winfo_width(), 8 + (latest + 10) * self.zoom)
        c.configure(scrollregion=(0, 0, full_w, height))
        for y0,y1,color in ((0,24,"#29313b"),(24,self.image_y,"#24262d"),
                            (self.image_y,self.audio_y,"#272a32"),(self.audio_y,height,"#242a30")):
            c.create_rectangle(0,y0,full_w,y1,fill=color,outline="")
        self.track_header.delete("all"); self.track_header.configure(scrollregion=(0,0,76,height))
        for lane in range(rows):
            self.track_header.create_text(8,45+lane*42,text=f"텍스트 {lane+1}",anchor="w",fill="#d6d6d6")
        for y,label in ((self.image_y+29,"이미지"),(self.audio_y+31,"음악")):
            self.track_header.create_text(8,y,text=label,anchor="w",fill="#d6d6d6")
        self.track_header.yview_moveto(c.yview()[0])
        left,right = max(0,c.canvasx(0)),c.canvasx(c.winfo_width())
        step = next((s for s in (1,2,5,10,30,60,300,600) if s*self.zoom >= 70),1800)
        for sec in range(max(0,int(left/self.zoom/step)*step), int(right/self.zoom)+step,step):
            x = sec*self.zoom
            c.create_line(x,16,x,height,fill="#41464d")
            c.create_text(x+3,9,text=clock(sec)[:5],anchor="w",fill="#b8bdc5")
        if self.duration:
            x = self.duration*self.zoom
            c.create_line(x,24,x,height,fill="#ed6868",dash=(3,3))
            c.create_text(x+4,25,text="영상 끝",anchor="nw",fill="#ed6868")
        for lane, items in enumerate(lanes):
            for item in items:
                self._draw_clip(item,"text",27+lane*42,34,"#604866",item["text"].replace("\n"," "))
        for clip in self.project["images"]:
            asset = next((a for a in self.project["assets"] if a["id"] == clip["asset"]), None)
            name = Path(asset["path"]).name if asset else "누락"
            self._draw_clip(clip,"image",self.image_y+6,46,"#345d81",name)
            effect,length,previous = core.transition_info(self.project,clip)
            if clip.get("transition",{}).get("type") != "cut":
                x0 = self._frame_x(clip["start"])
                x1 = self._frame_x(clip["start"] + min(clip["transition"]["frames"],clip["end"]-clip["start"]))
                c.create_rectangle(x0,self.image_y+6,x1,self.image_y+19,
                                   fill="#a67832" if effect != "cut" else "#654b37",outline="",
                                   tags=("transition",clip["id"]))
                if x1-x0 > 26:
                    c.create_text(x0+3,self.image_y+12,text=clip["transition"]["type"],
                                  anchor="w",fill="white",tags=("transition",clip["id"]))
        for clip in self.project["audio_clips"]:
            asset = next((a for a in self.project["audio_assets"] if a["id"] == clip["asset"]), None)
            label = Path(asset["path"]).name if asset else "누락"
            self._draw_clip(clip,"audio",self.audio_y+6,50,"#206b60",label)
            cache = self.audio_cache.get(clip["asset"])
            if cache:
                bins = cache["bins"]
                x0 = core.audio_start(clip)/core.RATE*self.zoom
                x1 = core.audio_end(clip)/core.RATE*self.zoom
                for px in range(max(int(left),int(x0)), min(int(right),int(x1)), 2):
                    source_s = clip["source_in"]/core.RATE+(px-x0)/self.zoom
                    idx = int(source_s*100)
                    if 0 <= idx < len(bins):
                        peak = min(1,bins[idx])
                        c.create_line(px,self.audio_y+31-peak*15,px,self.audio_y+31+peak*15,
                                      fill="#69d7b5",tags=("audio",clip["id"]))
        drag = self.drag or self.library_drag
        if drag and drag.get("active") and drag.get("trial"):
            trial = drag["trial"]; kind = drag["kind"]
            if kind == "audio": a,b = core.audio_start(trial)/core.RATE,core.audio_end(trial)/core.RATE
            else: a,b = trial["start"]/core.FPS,trial["end"]/core.FPS
            y = {"text":30,"image":self.image_y+9,"audio":self.audio_y+9}[kind]
            track_top,track_bottom={"text":(24,self.image_y),"image":(self.image_y,self.audio_y),
                                    "audio":(self.audio_y,self.audio_y+64)}[kind]
            c.create_rectangle(left,track_top,right,track_bottom,outline="#63dcae" if drag.get("valid") else "#e47777",
                               width=2)
            c.create_rectangle(a*self.zoom,y,b*self.zoom,y+28,fill="#4a9970" if drag.get("valid") else "#a24141",
                               stipple="gray25",outline="white",width=2)
            c.create_text(a*self.zoom+4,y-4,text=f"{clock(a)} – {clock(b)}  ({clock(b-a)})",
                          anchor="sw",fill="white")
        x=self._frame_x(round(self.position*core.FPS))
        self.playhead_height=height
        self.playhead_line=c.create_line(x,16,x,height,fill="#ffb34d",width=2,tags=("playhead",))
        self.playhead_marker=c.create_polygon(x-6,16,x+6,16,x,27,fill="#ffb34d",tags=("playhead",))

    def _timeline_hover(self,event):
        if self.drag or self.library_drag: return
        c=self.timeline; x=c.canvasx(event.x); y=c.canvasy(event.y)
        cursor="crosshair"
        for ident in reversed(c.find_overlapping(x-1,y-1,x+1,y+1)):
            tags=c.gettags(ident)
            if tags and tags[0] in ("image","audio","text") and len(tags)>1:
                kind,clip_id=tags[:2]
                items={"image":self.project["images"],"audio":self.project["audio_clips"],
                       "text":self.project["texts"]}[kind]
                clip=next((item for item in items if item["id"]==clip_id),None)
                if clip:
                    if kind=="audio": left,right=core.audio_start(clip)/core.RATE*self.zoom,core.audio_end(clip)/core.RATE*self.zoom
                    else: left,right=self._frame_x(clip["start"]),self._frame_x(clip["end"])
                    cursor="sb_h_double_arrow" if min(abs(x-left),abs(x-right))<=8 else "fleur"
                break
        if str(c.cget("cursor"))!=cursor: c.configure(cursor=cursor)

    def _draw_clip(self, item, kind, y, h, color, label):
        c = self.timeline
        if kind == "audio": start,end=core.audio_start(item)/core.RATE,core.audio_end(item)/core.RATE
        else: start,end=item["start"]/core.FPS,item["end"]/core.FPS
        x0,x1=start*self.zoom,end*self.zoom
        if x1 < c.canvasx(0) or x0 > c.canvasx(c.winfo_width()): return
        selected = self.selection == (kind,item["id"])
        c.create_rectangle(x0,y,x1,y+h,fill="#5682a9" if selected else color,
                           outline="#fff4d1" if selected else "#7896a9",width=2 if selected else 1,
                           tags=(kind,item["id"]))
        if x1-x0 > 18:
            c.create_line(x0+5,y+5,x0+5,y+h-5,fill="#d6eaf2",width=2,tags=(kind,item["id"]))
            c.create_line(x1-5,y+5,x1-5,y+h-5,fill="#d6eaf2",width=2,tags=(kind,item["id"]))
        if x1-x0 > 36:
            chars=max(1,int((x1-x0-18)/7))
            c.create_text(x0+10,y+h/2,text=label[:chars-1]+"…" if len(label)>chars else label,
                          anchor="w",fill="white",tags=(kind,item["id"]))

    def _snap_frame(self, value, kind, ident, alt=False):
        value=max(0,int(value))
        if not self.snap.get() or alt: return value
        points=[0,round(self.position*core.FPS),round(self.duration*core.FPS)]
        for clip in self.project["images"]+self.project["texts"]:
            if clip["id"] != ident: points.extend((clip["start"],clip["end"]))
        for clip in self.project["audio_clips"]:
            if clip["id"] != ident:
                points.extend((round(core.audio_start(clip)*core.FPS/core.RATE),
                               round(core.audio_end(clip)*core.FPS/core.RATE)))
        closest=min(points,key=lambda p:abs(p-value))
        return closest if abs(closest-value)*self.zoom/core.FPS <= 8 else value

    def _timeline_down(self,event):
        self._commit_text_draft(); self.timeline.focus_set()
        c=self.timeline; x=c.canvasx(event.x); y=c.canvasy(event.y)
        hits=c.find_overlapping(x-1,y-1,x+1,y+1)
        chosen=None
        for ident in reversed(hits):
            tags=c.gettags(ident)
            if tags and tags[0] in ("image","audio","text","transition") and len(tags)>1:
                chosen=(tags[0],tags[1]); break
        if chosen and self._editable():
            kind, ident=chosen
            if kind == "transition": kind="image"
            self.selection=(kind,ident)
            item=self._selected()
            if kind == "audio": start,end=core.audio_start(item)/core.RATE,core.audio_end(item)/core.RATE
            else: start,end=item["start"]/core.FPS,item["end"]/core.FPS
            left,right=start*self.zoom,end*self.zoom
            width=right-left
            edge="body"
            if chosen[0] == "transition": edge="transition"
            elif width < 16: edge="left" if x-left < right-x else "right"
            elif abs(x-left) <= 8: edge="left"
            elif abs(x-right) <= 8: edge="right"
            self._stop_audio()
            self.drag={"kind":kind,"id":ident,"edge":edge,"original":copy.deepcopy(item),
                       "x":x,"screen_x":event.x,"active":False,"valid":True,"trial":None}
            self._show_properties(); self.render_preview(); self.draw_timeline()
        else:
            self.drag={"kind":"seek","x":x}
            self.seek(self._x_to_frame(x)/core.FPS)

    def _timeline_move(self,event):
        drag=self.drag
        if not drag: return
        c=self.timeline; x=c.canvasx(event.x)
        if drag["kind"] == "seek":
            self.seek(self._x_to_frame(x)/core.FPS); return
        if not drag["active"] and abs(event.x-drag["screen_x"]) < 4: return
        drag["active"]=True
        self._stop_audio()
        if event.x < 30: c.xview_scroll(-1,"units"); x=c.canvasx(event.x)
        elif event.x > c.winfo_width()-30: c.xview_scroll(1,"units"); x=c.canvasx(event.x)
        if event.y < 30: c.yview_scroll(-1,"units")
        elif event.y > c.winfo_height()-30: c.yview_scroll(1,"units")
        origin=drag["original"]; trial=copy.deepcopy(origin)
        delta=round((x-drag["x"])*core.FPS/self.zoom)
        edge=drag["edge"]
        alt=bool(event.state & (0x20000 | 0x0008))
        if drag["kind"] == "audio":
            sample_delta=round(delta*core.RATE/core.FPS)
            if edge == "body":
                frame=self._snap_frame(round(origin["start_sample"]*core.FPS/core.RATE)+delta,"audio",origin["id"],alt)
                trial["start_sample"]=max(0,round(frame*core.RATE/core.FPS))
                # Exact edge alignment avoids a sub-frame silent gap.
                for other in self.project["audio_clips"]:
                    if other["id"] != origin["id"] and abs(core.audio_end(other)-trial["start_sample"])*self.zoom/core.RATE <= 8 and not alt:
                        trial["start_sample"]=core.audio_end(other)
            elif edge == "left":
                target_frame=self._snap_frame(round(origin["start_sample"]*core.FPS/core.RATE)+delta,
                                              "audio",origin["id"],alt)
                target_sample=round(target_frame*core.RATE/core.FPS)
                for other in self.project["audio_clips"]:
                    if other["id"]!=origin["id"] and not alt and abs(core.audio_end(other)-target_sample)*self.zoom/core.RATE<=8:
                        target_sample=core.audio_end(other)
                trial["start_sample"]=max(0,origin["start_sample"]-origin["source_in"],target_sample)
                trial["source_in"]=max(0,origin["source_in"]+trial["start_sample"]-origin["start_sample"])
            elif edge == "right":
                asset=next(a for a in self.project["audio_assets"] if a["id"]==origin["asset"])
                end_frame=self._snap_frame(round(core.audio_end(origin)*core.FPS/core.RATE)+delta,
                                           "audio",origin["id"],alt)
                target_sample=round(end_frame*core.RATE/core.FPS)
                for other in self.project["audio_clips"]:
                    if other["id"]!=origin["id"] and not alt and abs(core.audio_start(other)-target_sample)*self.zoom/core.RATE<=8:
                        target_sample=core.audio_start(other)
                trial["source_out"]=min(asset["samples"],max(origin["source_in"]+1,
                                         origin["source_in"]+target_sample-origin["start_sample"]))
            drag["valid"]=self._track_at(c.canvasy(event.y))=="audio" and core.valid_audio(self.project,trial,origin["id"])
        else:
            if edge == "body":
                start=self._snap_frame(origin["start"]+delta,drag["kind"],origin["id"],alt)
                trial["start"]=max(0,start); trial["end"]=trial["start"]+origin["end"]-origin["start"]
            elif edge == "left":
                trial["start"]=min(origin["end"]-1,self._snap_frame(origin["start"]+delta,drag["kind"],origin["id"],alt))
            elif edge == "right":
                trial["end"]=max(origin["start"]+1,self._snap_frame(origin["end"]+delta,drag["kind"],origin["id"],alt))
            elif edge == "transition":
                trial["transition"]["frames"]=max(1,min(origin["end"]-origin["start"],
                                                          origin["transition"]["frames"]+delta))
            valid_time = trial["start"]>=0 and trial["end"]>trial["start"]
            valid_overlap = drag["kind"]=="text" or core.valid_interval(self.project["images"],
                                                trial["start"],trial["end"],origin["id"])
            drag["valid"]=self._track_at(c.canvasy(event.y))==drag["kind"] and valid_time and valid_overlap
        drag["trial"]=trial
        self.draw_timeline()

    def _timeline_up(self,event):
        drag=self.drag; self.drag=None
        if drag and drag.get("active") and drag.get("valid") and drag.get("trial"):
            item=self._selected()
            if item and item != drag["trial"]:
                self._change(); item.update(drag["trial"]); self._refresh()
        else: self.draw_timeline()

    def _escape(self,event):
        if self.drag or self.library_drag:
            self.drag=None; self.library_drag=None; self.draw_timeline(); return "break"
        return None

    def render_preview(self):
        if not self.preview.winfo_exists(): return
        w,h=self.preview.winfo_width(),self.preview.winfo_height()
        if w<20 or h<20: return
        scale=min(w/1920,h/1080)
        width,height=max(1,int(1920*scale)),max(1,int(1080*scale))
        x,y=(w-width)//2,(h-height)//2
        frame=max(0,round(self.position*core.FPS))
        try:
            cue=core.active_image(self.project,frame)
            key=(frame if core.dynamic_frame(self.project,frame) else cue["id"] if cue else None,
                 tuple(t["id"] for t in self.project["texts"] if t["start"]<=frame<t["end"]),
                 width,height,self.revision)
            if key!=self.scene_cache_key or self.preview_ref is None:
                picture=core.render_scene(self.project,frame,self._scene_warn)
                self.preview_ref=ImageTk.PhotoImage(picture.resize((width,height)))
                self.scene_cache_key=key
            self.preview.delete("all"); self.preview.create_image(x,y,image=self.preview_ref,anchor="nw")
            self.preview_rect=(x,y,width,height)
            item=self._selected()
            if item and self.selection[0]=="text" and item["start"]<=frame<item["end"]:
                l,t,r,b=core.text_box(item)
                self.preview.create_rectangle(x+l*scale,y+t*scale,x+r*scale,y+b*scale,
                                              outline="#00d6ff",width=2)
            if item and self.selection[0]=="image" and self.image_mode.get() and cue and cue["id"]==item["id"]:
                state=core.transform_at(item,frame)
                cx,cy=x+state["x"]*scale,y+state["y"]*scale
                bounds=core.image_bounds(self.project,item,frame)
                if bounds:
                    l,t,r,b=bounds
                    l,t,r,b=x+l*scale,y+t*scale,x+r*scale,y+b*scale
                    self.preview.create_rectangle(l,t,r,b,outline="#00d6ff",width=2)
                    for hx,hy in ((l,t),(r,t),(l,b),(r,b)):
                        self.preview.create_rectangle(hx-5,hy-5,hx+5,hy+5,
                                                      fill="#00d6ff",outline="#ffffff")
                self.preview.create_oval(cx-4,cy-4,cx+4,cy+4,outline="#00d6ff",width=2)
                self.preview.create_text(cx+8,cy-10,text=f"{state['zoom']:.0f}%",anchor="w",fill="#00d6ff")
        except core.EditorError as e:
            self.status.set(str(e).splitlines()[0])

    def _preview_down(self,event):
        self._commit_text_draft(); self.preview.focus_set()
        frame=round(self.position*core.FPS)
        cue=core.active_image(self.project,frame)
        if self.image_mode.get() and cue:
            self.selection=("image",cue["id"])
            state=copy.deepcopy(cue[self.motion_key.get()])
            x,y,w,h=self.preview_rect
            bounds=core.image_bounds(self.project,cue,frame)
            mode="move"
            if bounds:
                l,t,r,b=bounds
                points=((x+l*w/1920,y+t*h/1080),(x+r*w/1920,y+t*h/1080),
                        (x+l*w/1920,y+b*h/1080),(x+r*w/1920,y+b*h/1080))
                if any(abs(event.x-px)<=10 and abs(event.y-py)<=10 for px,py in points): mode="zoom"
            self.preview_drag={"kind":"image","id":cue["id"],"x":event.x,"y":event.y,
                               "state":state,"mode":mode,"recorded":False}
            self._show_properties(); self.render_preview()
            return
        super()._preview_down(event)

    def _preview_move(self,event):
        drag=self.preview_drag
        if not isinstance(drag,dict): return super()._preview_move(event)
        if not self._editable(): return
        item=self._selected()
        if not item or item["id"]!=drag["id"]: return
        dx,dy=event.x-drag["x"],event.y-drag["y"]
        if not drag["recorded"] and max(abs(dx),abs(dy))<4: return
        if not drag["recorded"]:
            self._change(); drag["recorded"]=True
        _,_,w,h=self.preview_rect
        state=item[self.motion_key.get()]
        if drag["mode"]=="zoom":
            state["zoom"]=max(10,min(500,drag["state"]["zoom"]*(1+dx/max(40,w/2))))
        else:
            state["x"]=drag["state"]["x"]+dx*1920/w
            state["y"]=drag["state"]["y"]+dy*1080/h
        item.pop("legacy_native",None)
        self.scene_cache_key=None; self.render_preview()

    def _preview_up(self,event):
        if isinstance(self.preview_drag,dict):
            recorded=self.preview_drag["recorded"]
            self.preview_drag=None
            if recorded: self._refresh()
        else: super()._preview_up(event)

    def seek(self,seconds):
        self._commit_text_draft()
        self.position=max(0,min(self.duration,seconds))
        if self.playing:
            self._stop_audio()
            if self.play_thread: self.play_thread.join(timeout=1)
            self._start_audio()
        self.time_label.set(clock(self.position)+" / "+clock(self.duration))
        self.draw_timeline(); self.render_preview()

    def _start_audio(self):
        if not self.project["audio_clips"] or any(c["asset"] not in self.audio_cache for c in self.project["audio_clips"]):
            self.status.set("음원 분석이 끝나면 재생할 수 있습니다."); return
        self.play_stop=threading.Event(); cancel=self.play_stop
        self.playing=True; self.play_button.configure(text="Ⅱ 일시정지")
        start_sample=min(core.duration_samples(self.project),round(self.position*core.RATE))
        self.play_epoch=start_sample/core.RATE
        clips=sorted(copy.deepcopy(self.project["audio_clips"]),key=core.audio_start)
        paths={key:value["pcm"] for key,value in self.audio_cache.items()}
        final=core.duration_samples(self.project)
        def worker():
            try:
                import sounddevice as sd
                from contextlib import ExitStack
                self.play_frames=0
                with ExitStack() as stack:
                    streams={ident:stack.enter_context(Path(path).open("rb")) for ident,path in paths.items()}
                    stream=stack.enter_context(sd.RawOutputStream(samplerate=core.RATE,channels=2,
                                                                   dtype="float32",blocksize=2048))
                    self.play_latency=stream.latency
                    cursor=start_sample
                    while cursor<final and not cancel.is_set():
                        clip=next((c for c in clips if core.audio_start(c)<=cursor<core.audio_end(c)),None)
                        if clip:
                            count=min(2048,core.audio_end(clip)-cursor)
                            source_index=clip["source_in"]+cursor-core.audio_start(clip)
                            source=streams[clip["asset"]]
                            source.seek(source_index*8); raw=source.read(count*8)
                            if len(raw)<count*8: raise core.EditorError("오디오 캐시가 짧습니다. 다시 분석하세요.")
                        else:
                            next_start=min((core.audio_start(c) for c in clips if core.audio_start(c)>cursor),default=final)
                            count=min(2048,next_start-cursor); raw=bytes(count*8)
                        stream.write(raw)
                        cursor+=count; self.play_frames+=count
                if not cancel.is_set(): self.events.put(("play_end",))
            except Exception as e:
                if not cancel.is_set(): self.events.put(("play_error","오디오 재생 실패: 장치를 확인하세요.\n"+str(e)))
        self.play_thread=threading.Thread(target=worker,daemon=True); self.play_thread.start()

    def start_export(self):
        self._commit_text_draft()
        if any(c["asset"] not in self.audio_cache for c in self.project["audio_clips"]):
            messagebox.showwarning("내보내기", "음원 분석이 끝날 때까지 기다리세요."); return
        if not self.project["audio_clips"]:
            messagebox.showwarning("내보내기", "타임라인에 음원을 배치하세요."); return
        # The v1 dialog only reads the legacy audio field in its initial guard.
        self.project["audio"] = self.project["audio_assets"][0]["path"]
        try: return super().start_export()
        finally: self.project.pop("audio", None)

    def _begin_export(self,path):
        self._commit_text_draft()
        if Path(path).exists():
            if self.export_dialog and self.export_dialog.winfo_exists():
                self.export_status_label.configure(text="같은 이름의 파일이 있습니다. 다른 이름을 입력하세요.")
            else: messagebox.showerror("파일 충돌","같은 이름의 파일이 있습니다.")
            return
        try: ffmpeg=self.tool("ffmpeg")
        except core.EditorError as e: messagebox.showerror("내보내기",str(e)); return
        self._stop_audio(); self.exporting=True; self.export_cancel=threading.Event(); self.progress.set(0)
        self.progressbar.stop(); self.progressbar.configure(mode="determinate")
        self.cancel_button.configure(state="normal"); self._show_job_controls(running=True)
        self.status.set("MP4 내보내는 중…")
        if self.export_dialog and self.export_dialog.winfo_exists():
            self.export_status_label.configure(text="MP4 만드는 중…")
            self.export_start_button.configure(state="disabled")
            self.export_cancel_button.configure(text="작업 취소")
        snapshot=copy.deepcopy(self.project); duration=self.duration; cancel=self.export_cancel
        paths={key:value["pcm"] for key,value in self.audio_cache.items()}
        def worker():
            try:
                result=core.export_video(snapshot,duration,path,ffmpeg,cancel,
                                         lambda n:self.events.put(("progress",n)),paths)
                self.events.put(("export_done",result))
            except Exception as e: self.events.put(("export_error",str(e)))
        self.export_thread=threading.Thread(target=worker,daemon=True); self.export_thread.start()

    def _poll(self):
        try:
            while True:
                event=self.events.get_nowait(); kind=event[0]
                if kind=="audio_ready_v2":
                    _,ident,token,pcm,bins,samples=event
                    if self.audio_generation_by_id.get(ident)!=token: continue
                    asset=next((a for a in self.project["audio_assets"] if a["id"]==ident),None)
                    if not asset: continue
                    asset["samples"]=samples
                    for clip in self.project["audio_clips"]:
                        if clip["asset"]==ident:
                            clip["source_out"]=min(clip["source_out"],samples)
                    self.audio_cache[ident]={"pcm":pcm,"bins":bins,"samples":samples}
                    self.progressbar.stop(); self.cancel_button.configure(state="disabled")
                    self._show_job_controls(); self.status.set("음원 분석 완료. 타임라인에 배치하세요.")
                    self._refresh()
                    if not self.project["audio_clips"] and self.project["audio_assets"][0]["id"]==ident:
                        self.place_audio(ident,0)
                elif kind=="audio_error_v2":
                    _,ident,token,error=event
                    if self.audio_generation_by_id.get(ident)==token:
                        self.progressbar.stop(); self._show_job_controls()
                        self.status.set(error.splitlines()[0]); messagebox.showerror("음원 오류",error)
                elif kind=="audio_cancel_v2":
                    self.progressbar.stop(); self._show_job_controls(); self.status.set("음원 분석을 취소했습니다.")
                elif kind=="play_error":
                    self._stop_audio(); self.status.set(event[1].splitlines()[0]); messagebox.showerror("재생 오류",event[1])
                elif kind=="play_end":
                    self._stop_audio(); self.position=self.duration; self._refresh(False)
                elif kind=="progress":
                    self.progress.set(event[1]); self.status.set(f"MP4 내보내는 중… {event[1]}%")
                    if self.export_dialog and self.export_dialog.winfo_exists():
                        self.export_status_label.configure(text=f"MP4 만드는 중… {event[1]}%")
                elif kind=="export_done":
                    self.exporting=False; self.last_output=event[1]; self.progress.set(100)
                    self.cancel_button.configure(state="disabled")
                    self.open_file.configure(state="normal"); self.open_folder.configure(state="normal")
                    self._show_job_controls(complete=True); self.status.set("완료: "+str(event[1]))
                    if self.export_dialog and self.export_dialog.winfo_exists():
                        self.export_status_label.configure(text="완료: "+Path(event[1]).name)
                        self.export_cancel_button.configure(text="닫기"); self.export_result_button.pack(side="left")
                elif kind=="export_error":
                    self.exporting=False; self.cancel_button.configure(state="disabled")
                    self._show_job_controls(); self.status.set(event[1].splitlines()[0])
                    if self.export_dialog and self.export_dialog.winfo_exists():
                        self.export_status_label.configure(text=event[1].splitlines()[0])
                        self.export_start_button.configure(state="normal")
                        self.export_cancel_button.configure(text="닫기")
                    elif "취소" not in event[1]: messagebox.showerror("변환 오류",event[1])
        except queue.Empty: pass
        self.root.after(80,self._poll)

    def close(self):
        self._commit_text_draft()
        if self.exporting:
            self.export_cancel.set(); self.root.after(100,self.close); return
        if not self._confirm_dirty(): return
        self._save_ui_settings(); self._stop_audio(); self.audio_cancel.set()
        if self.audio_thread: self.audio_thread.join(timeout=3)
        if self.play_thread: self.play_thread.join(timeout=2)
        self._dispose_pcm(); self.root.destroy()
