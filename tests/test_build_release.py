"""Build input discovery and release replacement must preserve the prior build."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_release


class BuildReleaseTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/"build/build-script-tests"
        root.mkdir(parents=True,exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def bundle(self, folder):
        for item in build_release.FFMPEG_FILES:
            path = folder/item
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b"fixture")
        return folder

    def test_complete_root_bundle_has_priority(self):
        preferred = self.bundle(self.root/"ffmpeg")
        self.bundle(self.root/"build/source-export-dist/Seonyuldam/ffmpeg")
        self.assertEqual(build_release.find_ffmpeg(self.root),preferred)

    def test_partial_root_bundle_falls_back_to_complete_work_build(self):
        partial = self.root/"ffmpeg/bin/ffmpeg.exe"
        partial.parent.mkdir(parents=True); partial.write_bytes(b"partial")
        fallback = self.bundle(self.root/"build/source-export-dist/Seonyuldam/ffmpeg")
        self.assertEqual(build_release.find_ffmpeg(self.root),fallback)

    def test_missing_ffmpeg_does_not_start_pyinstaller_or_touch_release(self):
        release = self.root/"release/Seonyuldam"; release.mkdir(parents=True)
        marker = release/"original.txt"; marker.write_text("keep")
        with patch.object(build_release,"ROOT",self.root),patch.object(build_release.subprocess,"run") as run:
            with self.assertRaisesRegex(SystemExit,"FFmpeg"):
                build_release.main()
            run.assert_not_called()
        self.assertEqual(marker.read_text(),"keep")

    def fixtures(self):
        candidate = self.root/"candidate"; candidate.mkdir()
        (candidate/"new.txt").write_text("new")
        release = self.root/"release/Seonyuldam"; release.mkdir(parents=True)
        (release/"old.txt").write_text("old")
        return candidate,release

    def test_success_preserves_previous_release_as_backup(self):
        candidate,release = self.fixtures()
        backup = build_release.promote_release(candidate,release)
        self.assertEqual((release/"new.txt").read_text(),"new")
        self.assertEqual((backup/"old.txt").read_text(),"old")
        self.assertTrue((candidate/"new.txt").is_file())

    def test_failed_promotion_restores_previous_release(self):
        candidate,release = self.fixtures()
        original = Path.rename
        def rename(path,target):
            if path.name.startswith("Seonyuldam.ready-"): raise PermissionError("locked")
            return original(path,target)
        with patch.object(Path,"rename",autospec=True,side_effect=rename):
            with self.assertRaises(PermissionError): build_release.promote_release(candidate,release)
        self.assertEqual((release/"old.txt").read_text(),"old")
        self.assertFalse((release/"new.txt").exists())

    def test_failed_candidate_copy_leaves_previous_release_intact(self):
        candidate,release = self.fixtures()
        with patch.object(build_release.shutil,"copytree",side_effect=OSError("disk full")):
            with self.assertRaises(OSError): build_release.promote_release(candidate,release)
        self.assertEqual((release/"old.txt").read_text(),"old")


if __name__=="__main__": unittest.main()
