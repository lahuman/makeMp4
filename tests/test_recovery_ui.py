"""Real Tk lifecycle checks for atomic project adoption and recovery drafts."""
import copy
import json
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

import editor_core as core
from editor_ui_v2 import EditorApp
from recovery_store import RecoveryStore


class RecoveryUITests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.work=Path(self.folder.name)
        env=patch.dict(os.environ, LOCALAPPDATA=self.folder.name);env.start();self.addCleanup(env.stop)
        self.root=tk.Tk();self.root.withdraw()
        self.app=EditorApp(self.root)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.app._dispose_videos()
        self.app.media_executor.shutdown(wait=True,cancel_futures=True)
        for timer in self.root.tk.call("after","info"): self.root.tk.call("after","cancel",timer)
        self.root.destroy()

    def caption(self):
        return dict(id="t",start=0,end=30,text="saved",x=960,y=100,width=600,size=32)

    def test_invalid_open_preserves_project_and_undo(self):
        p=self.app.project; self.app.undo=[copy.deepcopy(p)]
        path=self.work/"invalid.json";path.write_text(json.dumps(dict(p,images=[3])))
        with patch("editor_session.filedialog.askopenfilename",return_value=str(path)), \
                patch("editor_session.messagebox.showerror") as error:
            self.app.open_project()
        error.assert_called_once();self.assertIs(self.app.project,p);self.assertEqual(len(self.app.undo),1)

    def test_legacy_probe_failure_does_not_replace_current_project(self):
        p=self.app.project
        legacy=core.fresh();legacy.update(_migrated=True,_migrated_v1=True)
        legacy["audio_assets"]=[dict(id="a",path="missing.wav",samples=0)]
        with patch("editor_session.filedialog.askopenfilename",return_value="old.json"), \
                patch.object(core,"load_project",return_value=legacy), \
                patch.object(core,"missing_media",return_value=[]), \
                patch.object(core,"probe_audio",side_effect=core.EditorError("probe failed")), \
                patch.object(self.app,"tool",return_value="ffprobe"), \
                patch("editor_session.messagebox.showerror") as error:
            self.app.open_project()
        error.assert_called_once();self.assertIs(self.app.project,p)

    def test_uncommitted_text_is_recovered_without_changing_live_document(self):
        item=self.caption();self.app.project["texts"]=[item]
        widget=tk.Text(self.root);widget.insert("1.0","draft text")
        self.app.text_draft=("t",widget)
        self.app._save_recovery()
        restored=core.load_project(self.app.recovery.path)
        self.assertEqual(restored["texts"][0]["text"],"draft text")
        self.assertEqual(item["text"],"saved");self.assertFalse(self.app.dirty)

    def test_failed_save_keeps_snapshot_successful_save_removes_it(self):
        self.app.recovery.save(self.app.project);snapshot=self.app.recovery.path
        self.app.project_file=self.work/"saved.json"
        with patch.object(core,"save_project",side_effect=core.EditorError("disk full")), \
                patch("editor_session.messagebox.showerror"):
            self.assertFalse(self.app.save_project())
        self.assertTrue(snapshot.exists())
        self.assertTrue(self.app.save_project());self.assertFalse(snapshot.exists())

    def test_orphan_snapshot_restores_as_unsaved_project(self):
        old=RecoveryStore(self.app.recovery.root)
        data=core.fresh();data["texts"]=[self.caption()];old.save(data)
        path=old.path;old.close()
        with patch("editor_session.messagebox.askyesno",return_value=True) as prompt:
            self.app._offer_recovery()
        prompt.assert_called_once();self.assertTrue(self.app.dirty);self.assertIsNone(self.app.project_file)
        self.assertEqual(self.app.project["texts"][0]["text"],"saved")
        self.assertTrue(self.app.recovery.path.exists());self.assertFalse(path.exists())

    def test_active_other_session_does_not_prompt(self):
        other=RecoveryStore(self.app.recovery.root)
        try:
            other.save(core.fresh())
            with patch("editor_session.messagebox.askyesno") as prompt:
                self.app._offer_recovery()
                prompt.assert_not_called()
        finally: other.close(discard=True)
