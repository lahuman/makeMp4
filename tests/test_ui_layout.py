"""Regression checks against real Tk geometry (requires a desktop session)."""
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

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
            self.root.after_cancel(timer)
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
        self.assertAlmostEqual(app.workspace.sashpos(0), int(height * .59), delta=2)
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


if __name__ == "__main__":
    unittest.main()
