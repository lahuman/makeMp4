"""Failure recovery, resource limits and cache ownership regressions."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageChops
import editor_core as core
from editor_session import ProjectSessionMixin
import media_runtime as runtime
from disk_cache import DiskCache, cache_write_lock
from recovery_store import RecoveryStore
import build_release


class RuntimeTests(unittest.TestCase):
    def test_failure_log_does_not_include_private_error_message(self):
        with self.assertLogs("seonyuldam",level="WARNING") as records:
            runtime.log_failure("import",ValueError("private-media-path-and-token"))
        self.assertNotIn("private-media-path-and-token", " ".join(records.output))
        self.assertIn("ValueError", " ".join(records.output))

    def test_probe_deadline_stops_and_reaps_process(self):
        process = Mock()
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired("probe", .1), (b"", b"")]
        with patch.object(runtime.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(core.EditorError, "제한 시간"):
                runtime.communicate_probe(process, threading.Event())
        process.terminate.assert_called_once(); process.wait.assert_called_once()

    def test_probe_cancel_stops_without_waiting_for_timeout(self):
        process = Mock(); process.poll.return_value = None
        cancel = threading.Event(); cancel.set()
        with self.assertRaisesRegex(core.EditorError, "취소"):
            runtime.communicate_probe(process, cancel)
        process.terminate.assert_called_once()

    def test_real_stalled_process_is_terminated(self):
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)) as process:
            guard = runtime.ProcessGuard(process, threading.Event(), .15)
            try:
                process.communicate(timeout=5)
                with self.assertRaisesRegex(core.EditorError, "멈춰"): guard.check()
                self.assertIsNotNone(process.poll())
            finally: guard.close()

    def test_disk_shortage_prevents_encoder_start(self):
        with tempfile.TemporaryDirectory() as folder:
            p = core.fresh()
            p["audio_clips"] = [core.audio_defaults("a", 0, core.RATE)]
            with patch.object(core,"missing_media",return_value=[]), \
                    patch.object(runtime.shutil,"disk_usage",return_value=SimpleNamespace(free=0)), \
                    patch.object(core,"_run_encoder") as encoder:
                with self.assertRaisesRegex(core.EditorError, "여유 공간"):
                    core._export_video(p,1,Path(folder)/"out.mp4","ffmpeg",threading.Event())
                encoder.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])


class OwnershipTests(unittest.TestCase):
    def test_budget_preserves_active_unknown_files_and_other_instances(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); active="a"*64; stale="b"*64
            (root/(active+".mp4")).write_bytes(b"active")
            (root/(stale+".mp4")).write_bytes(b"old")
            (root/"user.mp4").write_bytes(b"keep")
            first=DiskCache(root)
            try:
                second=DiskCache(root)
                try:
                    first.pin(active,budget=0,max_age=0)
                    self.assertTrue((root/(stale+".mp4")).exists())
                finally: second.close()
                first.pin("c"*64,budget=0,max_age=0)
                self.assertTrue((root/(active+".mp4")).exists())
                self.assertFalse((root/(stale+".mp4")).exists())
                self.assertEqual((root/"user.mp4").read_bytes(),b"keep")
            finally: first.close()

    def test_cancel_while_another_writer_holds_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            cancel=threading.Event(); cancel.set()
            with cache_write_lock(folder,"a"*64,threading.Event()):
                with self.assertRaisesRegex(core.EditorError,"취소"):
                    with cache_write_lock(folder,"a"*64,cancel): self.fail("lock acquired twice")

    def test_recovery_skips_live_sessions_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); original=root/"original.json"
            project=core.fresh(); core.save_project(project,original); before=original.read_bytes()
            first=RecoveryStore(root/"recovery"); second=RecoveryStore(root/"recovery")
            try:
                first.save(project)
                self.assertEqual(list(second.candidates()),[])
                snapshot=first.path; first.close()
                self.assertEqual(list(second.candidates()),[snapshot])
                self.assertEqual(core.load_project(snapshot)["version"],3)
                self.assertEqual(original.read_bytes(),before)
            finally: first.close(); second.close()

    def test_invalid_adoption_does_not_replace_existing_project(self):
        app=SimpleNamespace(project=core.fresh())
        previous=app.project
        invalid=copy.deepcopy(previous); invalid["images"]=[core.image_defaults("missing",0,30)]
        with self.assertRaises(core.EditorError): ProjectSessionMixin._adopt_project(app,invalid,None)
        self.assertIs(app.project,previous)


class ImageSourceCacheTests(unittest.TestCase):
    def test_motion_reuses_source_preserves_pixels_and_invalidates(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"image.png"; Image.new("RGB",(64,36),"red").save(path)
            p=core.fresh(); p["assets"]=[dict(id="i",path=str(path))]
            cue=core.image_defaults("i",0,30); cue["motion"]=True; cue["to"]["zoom"]=150
            p["images"]=[cue]; cache=core.LayerCache()
            expected=[core.render_scene(p,f) for f in range(5)]
            with patch.object(core.Image,"open",wraps=core.Image.open) as opened:
                actual=[core.render_scene(p,f,cache=cache) for f in range(5)]
                self.assertEqual(opened.call_count,1)
            for a,b in zip(expected,actual): self.assertIsNone(ImageChops.difference(a,b).getbbox())
            self.assertLessEqual(cache.bytes,cache.max_bytes)
            Image.new("RGB",(65,37),"blue").save(path)
            changed=core.render_scene(p,0,cache=cache)
            self.assertEqual(changed.getpixel((960,540)),(0,0,255))


class ReleaseIdentityTests(unittest.TestCase):
    def test_manifest_still_works_without_git_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/"Seonyuldam.exe").write_bytes(b"fixture")
            with patch.object(build_release.subprocess,"run",side_effect=FileNotFoundError):
                build_release.write_manifest(root,{}, {})
            info=json.loads((root/"build-info.json").read_text())
            self.assertIsNone(info["source_commit"])
            self.assertIsNone(info["source_dirty"])
            self.assertIn("Seonyuldam.exe",info["sha256"])

    def test_toolchain_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in build_release.FFMPEG_FILES:
                path=root/name; path.parent.mkdir(exist_ok=True,parents=True);path.write_bytes(b"baseline")
            expected={"sha256":{name:build_release.sha256(root/name) for name in build_release.FFMPEG_FILES}}
            lock=root/"lock.json";lock.write_text(json.dumps(expected))
            self.assertEqual(build_release.validate_toolchain(root,lock),expected)
            (root/"bin/ffmpeg.exe").write_bytes(b"changed")
            with self.assertRaises(RuntimeError): build_release.validate_toolchain(root,lock)

    def test_manifest_identifies_sources_executable_and_dirty_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            names=("source.py",)
            for name in (*names,"Seonyuldam.exe",*("ffmpeg/"+n for n in build_release.FFMPEG_FILES)):
                path=root/name;path.parent.mkdir(exist_ok=True,parents=True);path.write_bytes(b"fixture")
            with patch.object(build_release,"SOURCE_FILES",names), \
                    patch.object(build_release.subprocess,"run",side_effect=[
                        SimpleNamespace(returncode=0,stdout="abc123\n"),SimpleNamespace(returncode=0,stdout=" M source.py\n")]):
                build_release.write_manifest(root,{"version":"fixture"},{"Pillow":"fixture"})
            info=json.loads((root/"build-info.json").read_text())
            self.assertEqual(info["source_commit"],"abc123")
            self.assertTrue(info["source_dirty"])
            self.assertEqual(info["sha256"]["Seonyuldam.exe"],build_release.sha256(root/"Seonyuldam.exe"))
