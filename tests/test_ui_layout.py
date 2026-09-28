"""Regression checks against real Tk geometry (requires a desktop session)."""
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

import editor_core as core
from editor_ui_v2 import EditorApp


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

    def settings(self, data):
        path = Path(self.folder.name) / "MusicToVideo" / "ui.json"
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

    def test_first_run_with_delayed_window_mapping(self):
        app = EditorApp(self.root)
        self.settle(350)  # Startup timers expire before Windows maps the window.
        self.root.deiconify()
        self.settle()
        self.assert_default_panes(app)

    def test_first_run_with_slow_startup_before_event_loop(self):
        app = EditorApp(self.root)
        time.sleep(.2)
        self.root.deiconify()
        self.settle()
        self.assert_default_panes(app)

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
        self.assertTrue(any(app.timeline.itemcget(item, "fill") == "#F5F6F8"
                            for item in ruler_items))
        clip_items = [item for item in app.timeline.find_all()
                      if app.timeline.gettags(item)[:2] == ("image", clip["id"])]
        self.assertTrue(any(app.timeline.type(item) == "image" for item in clip_items))
        self.assertTrue(any(app.timeline.itemcget(item, "outline") == "#6366F1"
                            for item in clip_items if app.timeline.type(item) == "rectangle"))

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
