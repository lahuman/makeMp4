"""Regression checks against real Tk geometry (requires a desktop session)."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

import editor_core as core
from editor_ui_v2 import EditorApp
from editor_ui import UI_COLORS


class InitialLayoutTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parents[1] / "build" / "layout-tests"
        work.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.folder.cleanup)
        self.env = patch.dict(os.environ, LOCALAPPDATA=self.folder.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.addCleanup(self.destroy_root)

    def destroy_root(self):
        self.root.update_idletasks()
        for timer in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", timer)
        self.root.destroy()
        self.assertEqual(self.callback_errors, [])

    def settings(self, data, legacy=False):
        path = Path(self.folder.name) / ("MusicToVideo" if legacy else "Seonyuldam") / "ui.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def settle(self, milliseconds=180):
        self.root.after(milliseconds, self.root.quit)
        self.root.mainloop()

    def assert_default_panes(self, app):
        width = app.body.winfo_width()
        height = app.workspace.winfo_height()
        self.assertAlmostEqual(app.body.sashpos(0), min(270, max(210, int(width * .19))), delta=2)
        self.assertAlmostEqual(app.body.sashpos(1), max(650, width - 300), delta=2)
        self.assertAlmostEqual(app.workspace.sashpos(0), int(height * .64), delta=2)
        self.assertGreater(app.body.winfo_height(), 160)
        self.assertGreater(app.timeline.winfo_height(), 100)
        self.assertLessEqual(app.left.winfo_width(), app.body.sashpos(0) + 6)
        self.assertGreaterEqual(app.right.winfo_x(), app.body.sashpos(1))
        self.assertLessEqual(app.preview.winfo_rootx() + app.preview.winfo_width(), app.right.winfo_rootx())

    def test_first_run_with_delayed_window_mapping(self):
        app = EditorApp(self.root)
        self.settle(350)  # Startup timers expire before Windows maps the window.
        self.root.deiconify()
        self.settle()
        self.assert_default_panes(app)

    def test_studio_toolbar_fits_minimum_window_and_project_heading_updates(self):
        app = EditorApp(self.root)
        self.root.geometry("1040x680+0+0")
        self.root.deiconify()
        self.settle()
        for widget in app.timeline_bar.winfo_children():
            self.assertTrue(widget.winfo_ismapped(), widget.cget("text"))
            self.assertGreaterEqual(widget.winfo_x(), 0)
            self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), app.timeline_bar.winfo_width())
        self.assertEqual([app.arrange_menu.entrycget(i, "label") for i in range(3)],
                         ["이미지 모두 이어 붙이기", "음악 모두 이어 붙이기", "영상 모두 이어 붙이기"])
        self.assertEqual(app.project_state_label.cget("text"), "저장 전")
        app.project_file = Path(self.folder.name) / "제주의 저녁.json"
        app._refresh(False)
        self.assertEqual(app.project_name_label.cget("text"), "제주의 저녁")
        self.assertEqual(app.project_state_label.cget("text"), "저장됨")
        app._change()
        self.assertEqual(app.project_state_label.cget("text"), "● 수정됨")

    def test_first_run_with_slow_startup_before_event_loop(self):
        app = EditorApp(self.root)
        time.sleep(.2)
        self.root.deiconify()
        self.settle()
        self.assert_default_panes(app)

    def test_compact_image_inspector_keeps_transition_visible_and_fields_editable(self):
        image_path = Path(self.folder.name) / "photo.png"
        Image.new("RGB", (160, 90), "#3579a5").save(image_path)
        app = EditorApp(self.root)
        app.project["assets"] = [{"id": "photo", "path": str(image_path)}]
        clip = core.image_defaults("photo", 0, 180)
        app.project["images"] = [clip]
        app.selection = ("image", clip["id"])
        self.root.geometry("1420x850+0+0")
        self.root.deiconify()
        self.settle()
        app._refresh()
        self.root.update_idletasks()
        transition = next(w for w in app.properties.winfo_children() if isinstance(w, ttk.Combobox))
        self.assertLessEqual(transition.winfo_y() + transition.winfo_height(), app.property_canvas.winfo_height())
        self.assertFalse(app.property_groups.get("image_advanced", False))
        # The two time entries must retain their commit behavior after rearranging.
        time_row = next(w for w in app.properties.winfo_children()
                        if isinstance(w, ttk.Frame) and len(w.winfo_children()) == 2
                        and all(isinstance(c, ttk.Frame) for c in w.winfo_children()))
        fields = [next(c for c in w.winfo_children() if isinstance(c, ttk.Entry))
                  for w in time_row.winfo_children()]
        self.assertEqual(fields[0].winfo_rooty(), fields[1].winfo_rooty())
        fields[1].delete(0, "end"); fields[1].insert(0, "7")
        fields[1].event_generate("<FocusOut>")
        self.root.update_idletasks()
        self.assertEqual(app.project["images"][0]["end"], 210)
        app._toggle_property_group("image_advanced")
        self.assertTrue(app.property_groups["image_advanced"])
        app._commit("image_zoom", "125")
        self.assertEqual(app.project["images"][0]["from"]["zoom"], 125)
        app.undo_action()
        self.assertEqual(app.project["images"][0]["from"]["zoom"], 100)

    def test_media_type_filters_preserve_assets_and_import_commands(self):
        app = EditorApp(self.root)
        app.project["assets"] = [{"id": "photo", "path": "photo.png"}]
        app.project["video_assets"] = [{"id": "video", "path": "video.mp4", "frames": 30}]
        app._refresh_library()
        self.assertEqual(set(app.library_markers), {"photo", "video"})
        app.library_filter.set("사진")
        self.assertEqual(set(app.library_markers), {"photo"})
        app.library_filter.set("영상")
        self.assertEqual(set(app.library_markers), {"video"})
        app.library_filter.set("전체")
        self.assertEqual(set(app.library_markers), {"photo", "video"})
        self.assertEqual(len(app.project["assets"]), 1)
        self.assertEqual(len(app.project["video_assets"]), 1)
        self.assertEqual([app.import_menu.entrycget(i, "label") for i in range(3)],
                         ["음악 가져오기", "이미지 가져오기", "영상 가져오기"])

    def test_music_library_visible_in_small_window_and_scrolls_to_last_song(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [
            {"id": f"song{i}", "path": f"음악 {i + 1:02d}.mp3", "samples": 48000}
            for i in range(2)]
        app._refresh()
        self.root.deiconify()
        for size in ("1420x850+0+0", "1040x680+0+0"):
            self.root.geometry(size)
            self.settle()
            self.assertEqual(set(app.library_markers), {"song0", "song1"})
            self.assertGreater(app.library_canvas.winfo_height(), 40)
            for label in app.audio_rows.values():
                self.assertTrue(label.winfo_ismapped())
                self.assertGreaterEqual(label.winfo_rooty(), app.library_canvas.winfo_rooty())
                self.assertLessEqual(label.winfo_rooty() + label.winfo_height(),
                                     app.library_canvas.winfo_rooty() + app.library_canvas.winfo_height())
        app.project["audio_assets"].extend(
            {"id": f"song{i}", "path": f"음악 {i + 1:02d}.mp3", "samples": 48000}
            for i in range(2, 30))
        app._refresh()
        app.library_filter.set("음악")
        self.root.update_idletasks()
        self.assertEqual(len(app.audio_rows), 30)
        self.assertLess(app.library_canvas.yview()[1], 1)
        self.assertTrue(app.library_scrollbar.winfo_ismapped())
        canvas = app.library_canvas
        self.assertEqual(app._panel_wheel(SimpleNamespace(widget=canvas, delta=-120,
                         x_root=canvas.winfo_rootx()+5, y_root=canvas.winfo_rooty()+5)), "break")
        self.assertGreater(canvas.yview()[0], 0)
        self.root.tk.call(app.library_scrollbar.cget("command"), "moveto", 1)
        self.root.update_idletasks()
        last = app.audio_rows["song29"]
        self.assertGreaterEqual(last.winfo_rooty(), app.library_canvas.winfo_rooty())
        self.assertLessEqual(last.winfo_rooty() + last.winfo_height(),
                             app.library_canvas.winfo_rooty() + app.library_canvas.winfo_height())

    def test_music_import_preserves_filters_and_selection_adds_audio_clip(self):
        app = EditorApp(self.root)
        paths = [Path(self.folder.name) / name for name in ("첫 음악.wav", "둘째 음악.wav")]
        for path in paths:
            path.touch()
        image = Path(self.folder.name) / "photo.png"
        Image.new("RGB", (160, 90), "#3579a5").save(image)
        app.project["assets"] = [{"id": "photo", "path": str(image)}]
        app.project["video_assets"] = [{"id": "video", "path": "video.mp4"}]
        app.library_filter.set("영상")
        app.project_search.set("없는 이름")
        app.library_view.set("목록")
        with patch("editor_ui_v2.filedialog.askopenfilenames", return_value=[str(p) for p in paths]), \
                patch.object(app, "_analyze_asset"):
            app.choose_audio()
        ids = {a["id"] for a in app.project["audio_assets"]}
        self.assertEqual(app.library_filter.get(), "영상")
        self.assertEqual(app.project_search.get(), "없는 이름")
        self.assertEqual(app.library_view.get(), "목록")
        self.assertEqual(set(app.library_markers), set())
        app.project_search.set("")
        self.assertEqual(set(app.library_markers), {"video"})
        app.library_filter.set("음악")
        self.assertEqual(set(app.library_markers), ids)
        second = app.project["audio_assets"][1]
        app.audio_cache[second["id"]] = {"bins": []}
        second["samples"] = 48000
        app._refresh()
        self.assertIn("✓", app.audio_rows[second["id"]].cget("text"))
        app._library_down(second["id"])
        self.assertEqual(app.library_drag["kind"], "audio")
        app.add_selected_asset()
        self.assertEqual(app.project["audio_clips"][0]["asset"], second["id"])
        app.project_search.set("첫 음악")
        self.assertEqual(set(app.library_markers), {app.project["audio_assets"][0]["id"]})
        app.project_search.set("")
        app.library_filter.set("전체")
        self.assertEqual(set(app.library_markers), ids | {"photo", "video"})
        self.assertEqual(len(app.thumb_refs), 1)
        app.library_view.set("목록")
        self.assertEqual(set(app.library_markers), ids | {"photo", "video"})
        app.library_filter.set("사진")
        self.assertEqual(set(app.library_markers), {"photo"})
        app.library_filter.set("영상")
        self.assertEqual(set(app.library_markers), {"video"})

    def test_timeline_clipboard_preserves_trim_loop_settings_and_undo(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [{"id": "a", "path": "music.wav", "samples": 480000}]
        app.audio_cache["a"] = {"bins": [], "samples": 480000}
        audio = core.audio_defaults("a", 96000, 480000)
        audio.update(source_in=1200, source_out=97123, duration_samples=240001,
                     loop_offset_samples=99, gain=.3, fade_in_samples=111, fade_out_samples=222)
        app.project["audio_clips"] = [audio]
        app.project["assets"] = [{"id": "p", "path": "photo.png"}]
        image = core.image_defaults("p", 30, 120)
        image.update(motion=True, order=1)
        image["from"]["zoom"] = 125
        image["transition"] = {"type": "dissolve", "frames": 10}
        app.project["images"] = [image]
        metadata = {"width": 320, "height": 180, "frames": 300, "video_stream": 0,
                    "audio_stream": 1, "has_audio": True, "rotation": 0, "video_start": 0, "audio_start": 0}
        app.project["video_assets"] = [dict(metadata, id="v", path="video.mp4")]
        app.video_cache["v"] = metadata
        video = core.video_defaults("v", 60, 90)
        video.update(source_in_frame=15, source_out_frame=75, duration_frames=180,
                     loop_offset_frame=17, end=240, audio_enabled=True, audio_gain=.4, order=2)
        app.project["videos"] = [video]
        app._duration_refresh()
        app.add_text(30)
        text = app.project["texts"][0]
        app.text_draft = None
        for kind, key in (("audio", "audio_clips"), ("image", "images"), ("video", "videos"), ("text", "texts")):
            with self.subTest(kind=kind):
                source = app.project[key][0]
                snapshot = copy.deepcopy(source)
                app.selection = (kind, source["id"])
                app.undo.clear(); app.redo.clear()
                app.copy_selected()
                self.assertEqual(len(app.undo), 0)
                if kind == "audio": source["gain"] = .9
                elif kind == "text": source["text"] = "복사 후 수정"
                else: source["from"]["zoom"] = 200
                app.position = 2.5
                app.paste_clip()
                pasted = app.project[key][-1]
                self.assertNotEqual(pasted["id"], source["id"])
                if kind == "audio":
                    self.assertEqual(pasted["start_sample"], 120000)
                    moved = {"id", "start_sample"}
                else:
                    self.assertEqual((pasted["start"], pasted["end"]),
                                     (75, 75 + snapshot["end"] - snapshot["start"]))
                    self.assertGreater(pasted["order"], source.get("order", 0))
                    moved = {"id", "start", "end", "order"}
                self.assertEqual({k: v for k, v in pasted.items() if k not in moved},
                                 {k: v for k, v in snapshot.items() if k not in moved})
                self.assertEqual(len(app.undo), 1)
                if kind == "video": self.assertTrue(core.valid_video(app.project, pasted))
                app.undo_action()
                self.assertEqual(len(app.project[key]), 1)
                app.redo_action()
                self.assertEqual(app.project[key][-1], pasted)
                app.text_draft = None
                app.paste_clip()
                self.assertEqual(len({c["id"] for c in app.project[key]}), 3)
                if kind in ("image", "video"):
                    app.project[key][-1]["from"]["zoom"] = 333
                    self.assertEqual(pasted["from"]["zoom"], snapshot["from"]["zoom"])
                app.project[key][:] = app.project[key][:1]
                app.text_draft = None

    def test_timeline_clipboard_shortcuts_commit_text_and_preserve_entry_editing(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [{"id": "a", "path": "music.wav", "samples": 480000}]
        app.project["audio_clips"] = [core.audio_defaults("a", 0, 480000)]
        app.audio_cache["a"] = {"bins": [], "samples": 480000}
        app._duration_refresh()
        app.add_text(0)
        text = app._selected()
        box = app.text_draft[1]
        box.delete("1.0", "end"); box.insert("1.0", "최종 문구\n두 줄")
        app.copy_selected()
        self.assertEqual(app.clipboard_clip[1]["text"], "최종 문구\n두 줄")
        self.root.deiconify(); self.settle()
        app.timeline.focus_force()
        self.root.update()
        app.timeline.event_generate("<Control-c>")
        app.position = 4
        app.timeline.event_generate("<Control-v>")
        self.root.update_idletasks()
        self.assertEqual(len(app.project["texts"]), 2)
        self.assertEqual((app.project["texts"][-1]["start"], app.project["texts"][-1]["text"]),
                         (120, "최종 문구\n두 줄"))
        items = [i for i in app.timeline.find_all()
                 if app.timeline.gettags(i)[:2] == ("text", app.project["texts"][-1]["id"])]
        left, top, right, bottom = app.timeline.bbox(*items)
        event = SimpleNamespace(x=round((left+right)/2-app.timeline.canvasx(0)),
                                y=round((top+bottom)/2-app.timeline.canvasy(0)),
                                state=0, x_root=200, y_root=200)
        with patch.object(app.timeline_menu, "tk_popup") as popup:
            app._timeline_context(event)
        popup.assert_called_once_with(200, 200)
        self.assertEqual(app.selection, ("text", app.project["texts"][-1]["id"]))
        self.assertIsNone(app.drag)
        self.assertEqual(app.timeline_menu.entrycget(0, "state"), "normal")
        entry = ttk.Entry(app.properties)
        for widget in (entry, box):
            action = unittest.mock.Mock()
            self.assertIsNone(app._shortcut(SimpleNamespace(widget=widget), action))
            action.assert_not_called()
        app.exporting = True
        app.paste_clip()
        self.assertEqual(len(app.project["texts"]), 2)
        app.exporting = False
        with patch.object(app, "_confirm_dirty", return_value=True):
            app.new_project()
        self.assertIsNone(app.clipboard_clip)
        app.paste_clip()
        self.assertEqual(app.project["texts"], [])

    def test_media_deletion_removes_linked_clips_preserves_files_and_undoes(self):
        app = EditorApp(self.root)
        for kind, asset_key, clip_key in (("audio", "audio_assets", "audio_clips"),
                                         ("image", "assets", "images"), ("video", "video_assets", "videos")):
            with self.subTest(kind=kind):
                app.project = core.fresh()
                app.selection = None; app.text_draft = None
                path = Path(self.folder.name) / {"audio": "music.wav", "image": "photo.png", "video": "video.mp4"}[kind]
                path.touch()
                app.project[asset_key] = [{"id": "one", "path": str(path), "samples": 480000, "frames": 300},
                                          {"id": "two", "path": str(path), "samples": 480000, "frames": 300}]
                make_clip = {"audio": core.audio_defaults, "image": core.image_defaults, "video": core.video_defaults}[kind]
                end = 48000 if kind == "audio" else 30
                app.project[clip_key] = [make_clip("one", 0, end),
                                         make_clip("one", end, end*2 if kind == "image" else end),
                                         make_clip("two", 0, end)]
                app.selection = (kind, app.project[clip_key][0]["id"])
                app.copy_selected()
                before = copy.deepcopy(app.project)
                app.library_selection = "one"
                app.delete_media()
                self.assertTrue(path.exists())
                self.assertEqual([a["id"] for a in app.project[asset_key]], ["two"])
                self.assertEqual([c["asset"] for c in app.project[clip_key]], ["two"])
                self.assertIsNone(app.selection)
                app.paste_clip()
                self.assertEqual(len(app.project[clip_key]), 1)
                self.assertIn("원본 미디어", app.status.get())
                with patch.object(app, "_recheck_audio"), patch.object(app, "_recheck_videos"):
                    app.undo_action()
                    self.assertEqual(app.project, before)
                    app.redo_action()
                    self.assertEqual([c["asset"] for c in app.project[clip_key]], ["two"])

    def test_deleted_pending_media_ignores_late_results_and_delete_key_uses_focus(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [{"id": "a", "path": "music.wav", "samples": 0}]
        app.project["video_assets"] = [{"id": "v", "path": "video.mp4"}]
        app.audio_pending["a"] = 1; app.audio_generation_by_id["a"] = 1
        app.video_pending["v"] = 2; app.video_generation_by_id["v"] = 2
        app.video_progress["v"] = (0, "영상 정보 확인")
        app.library_selection = "a"
        app.delete_media()
        app.library_selection = "v"
        app.delete_media()
        self.assertFalse(app.audio_pending or app.video_pending)
        self.assertEqual(str(app.cancel_button.cget("state")), "disabled")
        app.events.put(("audio_ready_v2", "a", 1, "unused.pcm", [], 48000))
        app.events.put(("video_ready", "v", 2, {}))
        app.events.put(("video_progress", "v", 2, 99, "마무리"))
        app._poll()
        self.assertFalse(app.audio_cache or app.video_cache)
        self.assertFalse(app.project["audio_assets"] or app.project["video_assets"])
        app.project["assets"] = [{"id": "p", "path": "photo.png"}]
        app.project["images"] = [core.image_defaults("p", 0, 30)]
        self.root.deiconify(); self.settle()
        with patch.object(app.media_menu, "tk_popup") as popup:
            app._library_context("p", SimpleNamespace(x_root=200, y_root=200))
        popup.assert_called_once_with(200, 200)
        self.assertIsNone(app.library_drag)
        app.library_canvas.focus_force(); self.root.update()
        app.library_canvas.event_generate("<Delete>")
        self.assertEqual(app.project["assets"], [])
        self.assertEqual(app.project["images"], [])

    def test_property_scrollbar_stays_visible_and_hint_tracks_hidden_content(self):
        app = EditorApp(self.root)
        self.root.geometry("1040x680+0+0")
        self.root.deiconify()
        self.settle()
        self.assertEqual(app.property_scroll_hint.cget("text"), "")
        extra = [ttk.Label(app.properties, text=f"추가 설정 {i}", padding=8) for i in range(30)]
        for row in extra:
            row.pack(fill="x")
        self.root.update_idletasks()
        bar, canvas = app.property_scrollbar, app.property_canvas
        self.assertTrue(bar.winfo_ismapped())
        self.assertGreaterEqual(bar.winfo_width(), 16)
        self.assertGreaterEqual(bar.winfo_x(), canvas.winfo_x() + canvas.winfo_width())
        self.assertLessEqual(bar.winfo_x() + bar.winfo_width(), bar.master.winfo_width())
        self.assertEqual(app.property_scroll_hint.cget("text"), "아래로 스크롤 ↓")
        canvas.yview_moveto(.4)
        self.root.update_idletasks()
        self.assertEqual(app.property_scroll_hint.cget("text"), "↑ 위·아래로 스크롤 ↓")
        # Exercise the same command used by the scrollbar arrows and track.
        self.root.tk.call(bar.cget("command"), "moveto", 1)
        self.root.update_idletasks()
        self.assertEqual(app.property_scroll_hint.cget("text"), "↑ 위로 스크롤")
        # Selecting no clip replaces the long inspector with the short empty state.
        app.selection = None
        app._show_properties()
        self.root.update_idletasks()
        self.assertEqual(app.property_scroll_hint.cget("text"), "")
        self.assertEqual(canvas.yview(), (0.0, 1.0))

    def test_video_progress_updates_each_file_and_ignores_old_jobs(self):
        app = EditorApp(self.root)
        app.project["video_assets"] = [{"id": "v1", "path": "first.mp4"}, {"id": "v2", "path": "second.mp4"}]
        app.video_pending = {"v1": 1, "v2": 2}
        app.video_generation_by_id = dict(app.video_pending)
        app.video_progress = {"v1": (0, "영상 정보 확인"), "v2": (0, "영상 정보 확인")}
        app._refresh_library()
        first_label = app.library_video_progress["v1"][0]
        app.events.put(("video_progress", "v1", 1, 37, "미리보기 생성"))
        app.events.put(("video_progress", "v2", 2, 64, "미리보기 생성"))
        app.events.put(("video_progress", "v1", 0, 99, "마무리"))
        app._poll()
        self.assertIs(first_label, app.library_video_progress["v1"][0])
        self.assertIn("37%", first_label.cget("text"))
        self.assertIn("64%", app.library_video_progress["v2"][0].cget("text"))
        self.assertIn("외 1개 처리 중", app.status.get())
        self.assertEqual(app.progress.get(), 37)
        app.events.put(("audio_cancel_v2", "audio", 3, "취소"))
        app._poll()
        self.assertIn("37%", app.status.get())
        self.assertEqual(str(app.progressbar.cget("mode")), "determinate")
        app.library_filter.set("영상")
        self.assertIn("37%", app.library_video_progress["v1"][0].cget("text"))
        app.video_cancel.set()
        app.events.put(("video_error", "v1", 1, "작업을 취소했습니다."))
        app._poll()
        self.assertNotIn("v1", app.video_pending)
        self.assertIn("취소됨", app.library_video_progress["v1"][0].cget("text"))
        self.assertEqual(app.progress.get(), 64)
        self.assertIn("second.mp4", app.status.get())
        app.events.put(("video_error", "v2", 2, "작업을 취소했습니다."))
        app._poll()
        self.assertFalse(app.video_pending)
        self.assertEqual(str(app.cancel_button.cget("state")), "disabled")
        app.events.put(("video_progress", "v2", 2, 99, "마무리"))
        app._poll()
        self.assertIn("취소됨", app.library_video_progress["v2"][0].cget("text"))

    def test_collapsed_saved_layout_recovers(self):
        self.settings({"geometry": "1280x720+0+0", "panes": [0, 6, 0]})
        app = EditorApp(self.root)
        self.root.deiconify()
        self.settle()
        self.assert_default_panes(app)

    def test_valid_custom_layout_survives_restart(self):
        self.settings({"geometry": "1280x720+0+0", "panes": [230, 950, 350]})
        app = EditorApp(self.root)
        self.root.deiconify()
        self.settle()
        self.assertEqual([app.body.sashpos(0), app.body.sashpos(1), app.workspace.sashpos(0)], [230, 950, 350])
        app._save_ui_settings()
        saved = json.loads(app.ui_settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["panes"], [230, 950, 350])

    def test_previous_app_settings_are_loaded_and_saved_under_new_name(self):
        self.settings({"geometry": "1280x720+0+0", "panes": [230, 950, 350]}, legacy=True)
        app = EditorApp(self.root)
        self.root.deiconify()
        self.settle()
        self.assertEqual([app.body.sashpos(0), app.body.sashpos(1), app.workspace.sashpos(0)], [230, 950, 350])
        app._save_ui_settings()
        self.assertEqual(app.ui_settings_file.parent.name, "Seonyuldam")
        self.assertTrue(app.ui_settings_file.is_file())

    def test_malformed_pane_values_fall_back_to_defaults(self):
        self.settings({"panes": [None, "900", {}]})
        app = EditorApp(self.root)
        self.root.deiconify()
        self.settle()
        self.assert_default_panes(app)

    def test_non_object_settings_do_not_break_startup(self):
        self.settings([])
        app = EditorApp(self.root)
        self.root.deiconify()
        self.settle()
        self.assert_default_panes(app)

    def test_small_screen_and_reset_restore_visible_panels(self):
        with patch.object(self.root, "winfo_screenwidth", return_value=1280), \
                patch.object(self.root, "winfo_screenheight", return_value=720):
            app = EditorApp(self.root)
            self.root.deiconify()
            self.settle()
            self.assertLessEqual(self.root.winfo_width(), 1280 - 80)
            self.assertLessEqual(self.root.winfo_height(), 720 - 120)
            self.assertLessEqual(self.root.winfo_rooty() + self.root.winfo_height(), 720 - 40)
            self.assert_default_panes(app)
            app.toggle_panel("left")
            app.toggle_panel("right")
            app.reset_layout()
            self.settle()
            self.assertEqual(len(app.body.panes()), 3)
            self.assert_default_panes(app)

    def test_close_before_mapping_does_not_save_invalid_layout(self):
        app = EditorApp(self.root)
        app._save_ui_settings()
        self.assertFalse(app.ui_settings_file.exists())

    def test_oversized_previous_window_recovers_on_smaller_screen(self):
        self.settings({"geometry": "1420x850+200+200", "panes": [269, 1120, 464]})
        with patch.object(self.root, "winfo_screenwidth", return_value=1280), \
                patch.object(self.root, "winfo_screenheight", return_value=720):
            app = EditorApp(self.root)
            self.root.deiconify()
            self.settle()
            self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (1200, 600))
            self.assert_default_panes(app)

    def test_canvas_text_width_drag_and_undo(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [{"id": "audio", "path": "test.wav", "samples": 480000}]
        app.project["audio_clips"] = [{"id": "music", "asset": "audio", "start_sample": 0,
                                       "source_in": 0, "source_out": 480000}]
        self.root.deiconify()
        self.settle()
        app._refresh()
        app.add_text(0)
        item = app._selected()
        original_width = item["width"]
        l, top, r, bottom = core.text_box(item)
        x, y, w, h = app.preview_rect
        handle_x = round(x + r * w / 1920)
        handle_y = round(y + (top + bottom) * h / 2160)
        self.assertEqual(app._preview_target(handle_x, handle_y)[2], "width_right")
        app._preview_down(SimpleNamespace(x=handle_x, y=handle_y))
        app._preview_move(SimpleNamespace(x=handle_x + 25, y=handle_y))
        app._preview_up(SimpleNamespace(x=handle_x + 25, y=handle_y))
        self.assertGreater(app._selected()["width"], original_width)
        app.undo_action()
        self.assertEqual(app.project["texts"][0]["width"], original_width)

    def test_image_handle_direct_edit_and_composition_gaps(self):
        app = EditorApp(self.root)
        image_path = Path(self.folder.name) / "sample.png"
        Image.new("RGB", (1600, 900), "#456789").save(image_path)
        app.project["audio_assets"] = [{"id": "audio", "path": "test.wav", "samples": 480000}]
        app.project["audio_clips"] = [{"id": "music", "asset": "audio", "start_sample": 0,
                                       "source_in": 0, "source_out": 480000}]
        app.project["assets"] = [{"id": "photo", "path": str(image_path)}]
        image = core.image_defaults("photo", 0, 150)
        app.project["images"] = [image]
        self.root.deiconify()
        self.settle()
        app._refresh()
        x, y, w, h = app.preview_rect
        app._preview_down(SimpleNamespace(x=x + w // 2, y=y + h // 2))
        self.assertEqual(app.selection, ("image", image["id"]))
        corner_x, corner_y = x + w, y + h
        self.assertEqual(app._preview_target(corner_x, corner_y)[2], "zoom")
        app._preview_down(SimpleNamespace(x=corner_x, y=corner_y))
        app._preview_move(SimpleNamespace(x=corner_x + 20, y=corner_y + 20))
        app._preview_up(SimpleNamespace(x=corner_x + 20, y=corner_y + 20))
        self.assertGreater(app.project["images"][0]["from"]["zoom"], 100)
        self.assertEqual(app._preview_target(corner_x, corner_y)[2], "zoom")
        app.show_composition()
        self.assertTrue(any(row["name"] == "검은 화면" for row in
                            ({"name": app.composition_tree.item(i, "values")[1]}
                             for i in app.composition_tree.get_children())))
        self.assertIn("검은 화면 1곳", app.composition_summary.cget("text"))
        image_row = next(row for row in app.composition_tree.get_children()
                         if app.composition_rows[row][1] == image["id"])
        app.composition_tree.selection_set(image_row)
        app._composition_edit("duplicate")
        self.assertEqual(len(app.project["images"]), 2)
        app.undo_action()
        self.assertEqual(len(app.project["images"]), 1)

    def test_export_completion_shows_result_details_and_actions(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [{"id": "audio", "path": "test.wav", "samples": 48000}]
        app.project["audio_clips"] = [{"id": "music", "asset": "audio", "start_sample": 0,
                                       "source_in": 0, "source_out": 48000}]
        app.audio_cache["audio"] = {"pcm": "", "bins": [], "samples": 48000}
        self.root.deiconify()
        self.settle()
        app._refresh()
        with patch("editor_ui.messagebox.askyesno", return_value=True):
            app.start_export()
        result = Path(self.folder.name) / "완료.mp4"
        result.write_bytes(b"sample output")
        app.last_output = result
        app._show_export_result(result)
        self.assertEqual(app.export_heading.cget("text"), "MP4 생성 완료")
        self.assertFalse(app.export_form.winfo_manager())
        self.assertEqual(app.export_result_name.cget("text"), result.name)
        self.assertTrue(app.export_result_button.winfo_manager())
        self.assertTrue(app.export_folder_button.winfo_manager())
        app._copy_export_path()
        self.assertEqual(self.root.clipboard_get(), str(result))
        app._close_export_dialog()

    def test_bulk_media_append_keeps_order_and_can_be_undone(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [{"id": "a", "path": "a.wav", "samples": 96000},
                                       {"id": "b", "path": "b.wav", "samples": 144001}]
        app.append_all_audio()
        self.assertEqual(app.project["audio_clips"], [])
        app.audio_cache = {a["id"]: {"pcm": "", "bins": [], "samples": a["samples"]}
                           for a in app.project["audio_assets"]}
        app.arrange_menu.invoke(1)
        clips = app.project["audio_clips"]
        self.assertEqual([c["asset"] for c in clips], ["a", "b"])
        self.assertEqual(core.audio_start(clips[1]), core.audio_end(clips[0]))
        self.assertEqual(core.duration_samples(app.project), 240001)
        app.append_all_audio()
        self.assertEqual(len(clips), 2)
        app.selection = ("audio", clips[1]["id"])
        app._commit("fade_in_seconds", "0.25")
        self.assertEqual(clips[1]["fade_in_samples"], 12000)
        app.undo_action()
        self.assertEqual(app.project["audio_clips"][1]["fade_in_samples"], core.RATE)
        for index in range(3):
            path = Path(self.folder.name) / f"image{index}.png"
            Image.new("RGB", (16, 9), "blue").save(path)
            app.project["assets"].append({"id": str(index), "path": str(path)})
        app.arrange_menu.invoke(0)
        images = app.project["images"]
        self.assertEqual([c["asset"] for c in images], ["0", "1", "2"])
        self.assertEqual(images[0]["start"], 0)
        self.assertEqual(images[-1]["end"], core.total_frames(app.duration))
        self.assertEqual(images[0]["end"], images[1]["start"])
        self.assertEqual(images[1]["end"], images[2]["start"])
        app.append_all_images()
        self.assertEqual(len(images), 3)
        app.undo_action()
        self.assertEqual(app.project["images"], [])


    def test_export_folder_selection_is_saved_and_restored(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [{"id": "audio", "path": "test.wav", "samples": 48000}]
        app.project["audio_clips"] = [{"id": "music", "asset": "audio", "start_sample": 0,
                                       "source_in": 0, "source_out": 48000}]
        app.audio_cache["audio"] = {"pcm": "", "bins": [], "samples": 48000}
        self.root.deiconify()
        self.settle()
        app._refresh()
        folder = Path(self.folder.name) / "영상 저장"
        folder.mkdir()
        with patch("editor_ui.messagebox.askyesno", return_value=True):
            app.start_export()
        with patch("editor_ui.filedialog.askdirectory", return_value=str(folder)):
            app._browse_export_folder()
        saved = json.loads(app.ui_settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["export_folder"], str(folder))
        app._close_export_dialog()
        app.last_export_folder = None
        app._restore_ui_settings()
        app.project_file = Path(self.folder.name) / "다른 프로젝트.json"
        with patch("editor_ui.messagebox.askyesno", return_value=True):
            app.start_export()
        self.assertEqual(app.export_folder_var.get(), str(folder))
        # Manually entered folders must also persist when starting an export.
        app.export_folder_var.set(self.folder.name)
        with patch.object(app, "_begin_export") as begin:
            app._export_from_dialog()
        begin.assert_called_once()
        saved = json.loads(app.ui_settings_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["export_folder"], self.folder.name)
        app.export_folder_var.set(str(folder / "없는 폴더"))
        with patch.object(app, "_begin_export") as begin:
            app._export_from_dialog()
        begin.assert_not_called()
        self.assertEqual(app.last_export_folder, self.folder.name)
        app._close_export_dialog()

    def test_missing_export_folder_falls_back_to_project_folder(self):
        self.settings({"export_folder": str(Path(self.folder.name) / "없는 폴더")})
        app = EditorApp(self.root)
        app.project_file = Path(self.folder.name) / "프로젝트.json"
        app.project["audio_assets"] = [{"id": "audio", "path": "test.wav", "samples": 48000}]
        app.project["audio_clips"] = [{"id": "music", "asset": "audio", "start_sample": 0,
                                       "source_in": 0, "source_out": 48000}]
        app.audio_cache["audio"] = {"pcm": "", "bins": [], "samples": 48000}
        app._refresh()
        with patch("editor_ui.messagebox.askyesno", return_value=True):
            app.start_export()
        self.assertEqual(app.export_folder_var.get(), self.folder.name)
        app._close_export_dialog()

    def test_audio_track_uses_available_height_and_black_preview_is_explained(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [{"id": "audio", "path": "test.wav", "samples": 480000}]
        app.project["audio_clips"] = [{"id": "music", "asset": "audio", "start_sample": 0,
                                        "source_in": 0, "source_out": 480000}]
        app.audio_cache["audio"] = {"pcm": "", "bins": [], "samples": 480000}
        self.root.deiconify()
        self.settle()
        app.position = 5
        app._refresh()
        self.assertGreater(app.audio_track_height, 64)
        self.assertEqual(app._track_at(app.audio_y + app.audio_track_height - 8), "audio")
        self.assertEqual(app.preview_context.cget("text"), "이미지 없음 · 검은 화면")

    def test_timeline_shows_light_ruler_image_thumbnail_and_selected_clip(self):
        image_path = Path(self.folder.name) / "timeline.png"
        Image.new("RGB", (160, 90), "#3579a5").save(image_path)
        app = EditorApp(self.root)
        app.project["assets"] = [{"id": "asset", "path": str(image_path)}]
        clip = core.image_defaults("asset", 0, 180)
        app.project["images"] = [clip]
        app.selection = ("image", clip["id"])
        self.root.deiconify()
        self.settle()
        app._refresh()
        ruler_items = app.timeline.find_overlapping(1, 1, 10, 10)
        self.assertTrue(any(app.timeline.itemcget(item, "fill") == UI_COLORS["ruler"]
                            for item in ruler_items))
        clip_items = [item for item in app.timeline.find_all()
                      if app.timeline.gettags(item)[:2] == ("image", clip["id"])]
        self.assertTrue(any(app.timeline.type(item) == "image" for item in clip_items))
        self.assertTrue(any(app.timeline.itemcget(item, "outline") == UI_COLORS["primary"]
                            for item in clip_items if app.timeline.type(item) in ("rectangle", "polygon")))

    def test_timeline_drag_commits_once_and_allows_parallel_images(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [{"id": "audio", "path": "test.wav", "samples": 480000}]
        app.project["audio_clips"] = [{"id": "music", "asset": "audio", "start_sample": 0,
                                       "source_in": 0, "source_out": 480000}]
        first = core.image_defaults("first", 0, 60)
        second = core.image_defaults("second", 150, 210)
        app.project["images"] = [first, second]
        self.root.deiconify()
        self.settle()
        app._refresh()
        self.assertEqual(app._snap_move_start(116, 30, "image", first["id"], False), 120)
        y = app.image_y + 25
        down = SimpleNamespace(x=55, y=y, state=0)
        moved = SimpleNamespace(x=110, y=y, state=0)
        app._timeline_down(down)
        app._timeline_move(moved)
        app._timeline_up(moved)
        self.assertEqual(app.project["images"][0]["start"], 30)
        app.undo_action()
        self.assertEqual(app.project["images"][0]["start"], 0)
        overlap = SimpleNamespace(x=275, y=y, state=0)
        app._timeline_down(down)
        app._timeline_move(overlap)
        self.assertTrue(app.drag["valid"])
        app._timeline_up(overlap)
        self.assertGreater(app.project["images"][0]["start"], 0)
        self.assertLess(app.project["images"][0]["start"], second["end"])
        self.assertEqual(len(app._media_lanes(app.project["images"],
                                              lambda clip: clip["start"], lambda clip: clip["end"])), 2)

    def test_parallel_audio_and_image_tracks_have_separate_rows(self):
        app = EditorApp(self.root)
        app.project["audio_assets"] = [{"id": "a", "path": "one.wav", "samples": 480000}]
        app.audio_cache["a"] = {"pcm": "", "bins": [], "samples": 480000}
        app.place_audio("a", 0)
        app.place_audio("a", 30)
        app.project["assets"] = [{"id": "p", "path": "one.png"}]
        app.place_asset("p", 0)
        app.place_asset("p", 30)
        self.root.deiconify()
        self.settle()
        app.draw_timeline()
        labels = [app.track_header.itemcget(item, "text") for item in app.track_header.find_all()
                  if app.track_header.type(item) == "text"]
        self.assertIn("이미지 2", labels)
        self.assertIn("음악 2", labels)
        self.assertEqual(len(app.project["audio_clips"]), 2)
        self.assertEqual(len(app.project["images"]), 2)
        back, front = app.project["images"]
        app.selection = ("image", front["id"])
        app._move_image_layer(-1)
        self.assertEqual(core.active_image(app.project, 30)["id"], back["id"])
        self.assertEqual(app.project["images"][0]["id"], front["id"])
        image_lanes = app._media_lanes(app.project["images"],
                                       lambda clip: clip["start"], lambda clip: clip["end"], stacked=True)
        self.assertEqual(image_lanes[-1][0]["id"], back["id"])
        app.undo_action()
        self.assertEqual(app.project["images"][-1]["id"], front["id"])


if __name__ == "__main__":
    unittest.main()
