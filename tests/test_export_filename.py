"""Check export filename input and Windows filename boundaries with real Tk variables."""
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from editor_ui import EditorApp


class ExportFilenameTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tcl()
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.folder.cleanup)
        self.app = EditorApp.__new__(EditorApp)
        self.app.export_name_var = tk.StringVar(self.root)
        self.app.export_name_var.trace_add("write", self.app._filter_export_name)
        self.app.export_folder_var = tk.StringVar(self.root, value=self.folder.name)
        self.app.export_status_label = SimpleNamespace(configure=Mock())
        self.app._remember_export_folder = Mock()
        self.app._begin_export = Mock()

    def test_pasted_invalid_characters_are_removed_and_export_uses_visible_name(self):
        self.app.export_name_var.set('오늘<은> :"뭐/할\\까|?*\n\t\x00.mp4')
        self.assertEqual(self.app.export_name_var.get(), "오늘은 뭐할까.mp4")
        self.app._export_from_dialog()
        self.app._begin_export.assert_called_once_with(str(Path(self.folder.name) / "오늘은 뭐할까.mp4"))

    def test_valid_characters_are_preserved(self):
        name = "한글 (여행)_추억-2026! & # +.MP4"
        self.app.export_name_var.set(name)
        self.app._export_from_dialog()
        self.assertEqual(self.app.export_name_var.get(), name)
        self.app._begin_export.assert_called_once()

    def test_empty_and_reserved_names_do_not_start_export(self):
        for name in ("?*", "... ", ".mp4", "CON", "nul.mp4", "COM1.video.mp4", "LPT².mp4", "AUX .mp4"):
            with self.subTest(name=name):
                self.app.export_name_var.set(name)
                self.app._export_from_dialog()
                self.app._begin_export.assert_not_called()

    def test_trailing_dots_and_spaces_are_removed_and_extension_is_added(self):
        self.app.export_name_var.set(" 여행 영상.  ")
        self.app._export_from_dialog()
        self.assertEqual(self.app.export_name_var.get(), "여행 영상.mp4")
        self.app._begin_export.assert_called_once_with(str(Path(self.folder.name) / "여행 영상.mp4"))

    def test_existing_file_still_uses_collision_check(self):
        output = Path(self.folder.name) / "여행.mp4"
        output.write_bytes(b"original")
        self.app.export_name_var.set("여행?.mp4")
        # Use the actual pre-export collision check, before any encoder starts.
        self.app._begin_export = EditorApp._begin_export.__get__(self.app)
        self.app.export_dialog = None
        from unittest.mock import patch
        with patch("editor_ui.messagebox.showerror") as error:
            self.app._export_from_dialog()
        error.assert_called_once()
        self.assertEqual(output.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
