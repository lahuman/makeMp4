"""Project file lifecycle and crash recovery, independent of editor widgets."""
import copy
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import editor_core as core
import media_runtime as runtime
from recovery_store import RecoveryStore


class ProjectSessionMixin:
    def show_diagnostics(self):
        info = runtime.build_identity()
        commit = info.get("source_commit") or "소스 실행 / 빌드 정보 없음"
        messagebox.showinfo("진단 정보", f"선율담 {info['version']}\nPython {info['python'].split()[0]}\n빌드: {commit}\n"
                            f"로그: {self.ui_settings_file.parent / 'logs'}\n복구: {self.ui_settings_file.parent / 'recovery'}")

    def _init_recovery(self):
        self.recovery = None
        self._recovery_timer = None
        try:
            self.recovery = RecoveryStore(self.ui_settings_file.parent / "recovery")
        except OSError as error:
            runtime.log_failure("recovery_init", error)
            self.status.set("자동 복구 파일을 만들 수 없습니다. 작업을 자주 저장하세요.")
        self.root.bind("<Destroy>", self._release_recovery, add="+")
        self.root.after_idle(self._offer_recovery)
        self._recovery_timer = self.root.after(15000, self._save_recovery)

    def _release_recovery(self, event=None):
        if event is not None and event.widget is not self.root: return
        if self._recovery_timer is not None:
            self.root.after_cancel(self._recovery_timer); self._recovery_timer = None
        if self.recovery:
            try: self.recovery.close()
            except OSError as error: runtime.log_failure("recovery_close", error)
            self.recovery = None

    def _discard_recovery(self):
        store = getattr(self, "recovery", None)
        if store:
            try: store.discard()
            except OSError as error: runtime.log_failure("recovery_discard", error)

    def _save_recovery(self):
        try:
            if self.recovery:
                snapshot = copy.deepcopy(self.project)
                changed = self.dirty
                if self.text_draft:
                    ident, widget = self.text_draft
                    value = widget.get("1.0", "end-1c")
                    item = next((t for t in snapshot["texts"] if t["id"] == ident), None)
                    if item and item["text"] != value:
                        item["text"] = value; changed = True
                if changed: self.recovery.save(snapshot)
        except (OSError, core.EditorError, tk.TclError) as error:
            runtime.log_failure("recovery_save", error)
            self.status.set("자동 복구 저장에 실패했습니다. 파일 > 저장으로 작업을 보관하세요.")
        finally:
            self._recovery_timer = self.root.after(15000, self._save_recovery)

    def _offer_recovery(self):
        if not self.recovery or self.dirty: return
        try:
            for path in self.recovery.candidates():
                try: data = core.load_project(path)
                except core.EditorError as error:
                    runtime.log_failure("recovery_read", error); continue
                if messagebox.askyesno("작업 복구", "이전에 종료되지 않은 편집 내용이 있습니다. 복구할까요?\n원본 프로젝트는 변경하지 않습니다. 아니오를 선택하면 복구 파일을 보관합니다."):
                    self._adopt_project(data, None, dirty=True)
                    self.recovery.save(self.project)
                    path.unlink(missing_ok=True)
                    self.status.set("작업을 복구했습니다. 다른 이름으로 저장하세요. 누락된 미디어는 다시 연결해야 합니다.")
                break
        except (OSError, core.EditorError) as error:
            runtime.log_failure("recovery_offer", error)

    def save_project(self):
        self._commit_text_draft()
        if self.project.get("_migrated") and self.project_file == getattr(self,"_legacy_project_file",None):
            return self.save_as()
        saved = super().save_project()
        if saved: self._discard_recovery()
        return saved

    def save_as(self):
        self._commit_text_draft()
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("프로젝트 JSON", "*.json")],
                 initialfile=(self.project_file.stem + "_v3.json" if self.project_file and self.project.get("_migrated") else None))
        if not path: return False
        previous = self.project_file; self.project_file = Path(path)
        if self.save_project(): self.project.pop("_migrated", None); return True
        self.project_file = previous; return False

    def new_project(self):
        self._commit_text_draft()
        if not self._editable() or not self._confirm_dirty(): return
        self._discard_recovery()
        self._stop_audio(); self.audio_cancel.set(); self._dispose_pcm()
        self._dispose_videos()
        self.audio_cancel = threading.Event()
        self.project = core.fresh(); self.project_file = None; self.duration = 0; self.position = 0
        self.clipboard_clip = None
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
                types = {"audio": [("음악", "*.flac *.wav *.mp3")], "image": [("이미지", "*.jpg *.jpeg *.png")],
                         "video": [("영상", "*.mp4 *.mov *.mkv *.webm")]}[kind]
                replacement = filedialog.askopenfilename(filetypes=types)
                if not replacement: return
                collection = {"audio": data["audio_assets"], "image": data["assets"], "video": data["video_assets"]}[kind]
                next(a for a in collection if a["id"] == asset_id)["path"] = replacement
            if data.get("_migrated_v1") and data["audio_assets"]:
                asset = data["audio_assets"][0]
                duration = core.probe_audio(asset["path"], self.tool("ffprobe"))
                asset["samples"] = round(duration * core.RATE)
                data["audio_clips"][0]["source_out"] = asset["samples"]
                end = core.total_frames(duration)
                cues = sorted(data["images"], key=lambda c: c["start"])
                if cues: cues[-1]["end"] = max(cues[-1]["start"] + 1, end)
            self._adopt_project(data, Path(path), dirty=bool(missing or data.get("_migrated")))
            if data.get("_migrated"): self.status.set("이전 프로젝트를 변환했습니다. 저장 시 _v3 이름을 사용하세요.")
        except core.EditorError as e: messagebox.showerror("열기 오류", str(e))


    def _adopt_project(self, data, path, dirty=False):
        core.validate_project(data)
        self._discard_recovery()
        self._stop_audio(); self.audio_cancel.set(); self._dispose_pcm()
        self._dispose_videos()
        self.audio_cancel = threading.Event()
        self.project = data; self.project_file = path
        self.clipboard_clip = None
        self._legacy_project_file = path if data.get("_migrated") else None
        self.selection = None; self.library_selection = None
        self.undo.clear(); self.redo.clear(); self.dirty = dirty
        self.position = 0; self.revision += 1; self.scene_cache_key = None
        self._refresh(); self._recheck_audio(); self._recheck_videos()
