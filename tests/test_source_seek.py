"""Original-media seeking must match decoding from frame zero, including VFR."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

import editor_core as core
from editor_ui_v2 import EditorApp
from seonyuldam import bundled_tool
import video_media


class OriginalSeekTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe = bundled_tool("ffmpeg"), bundled_tool("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe: raise unittest.SkipTest("FFmpeg required")
        root = Path(__file__).resolve().parents[1]/"build/source-export"
        root.mkdir(parents=True,exist_ok=True)
        cls.folder = tempfile.TemporaryDirectory(dir=root)
        cls.addClassCleanup(cls.folder.cleanup)
        cls.work = Path(cls.folder.name)
        cls.cancel = threading.Event()
        cls.source = cls.work/"ntsc.mp4"
        cls.ff(["-f","lavfi","-i","testsrc2=s=320x180:r=30000/1001:d=12",
                "-c:v","libx264","-g","90","-bf","3","-sc_threshold","0","-pix_fmt","yuv420p",str(cls.source)])

    @classmethod
    def ff(cls, args):
        return subprocess.run([cls.ffmpeg,"-hide_banner","-nostdin","-v","error","-y",
                               "-threads","2","-filter_threads","2"]+args,
                              capture_output=True,check=True,timeout=90,creationflags=video_media.FLAGS).stdout

    def prepare(self, path, require_origin=True):
        data = video_media.prepare_video(path,self.ffmpeg,self.ffprobe,self.cancel,self.work/"cache")
        if require_origin: self.assertIsNotNone(data["decode_origin"])
        return dict(data,id=path.stem,path=str(path))

    def assert_matches_full_decode(self, asset):
        numbers = [n for n in (0,1,59,60,61,62,89,90,91,130,200,230,300,asset["frames"]-2,asset["frames"]-1)
                   if 0 <= n < asset["frames"]]
        ordered = sorted(set(numbers))
        selection = "+".join(f"eq(n\\,{n})" for n in ordered)
        filters = video_media.tone_map_filter(asset) + video_media.NORMALIZE
        filters += f",select={selection},scale={asset['width']}:{asset['height']},setsar=1"
        raw = self.ff(["-i",asset["path"],"-map",f"0:{asset['video_stream']}","-vf",filters,
                       "-an","-f","rawvideo","-pix_fmt","rgb24","-threads","1","-fps_mode","passthrough","pipe:1"])
        size = asset["width"]*asset["height"]*3
        self.assertEqual(len(raw),size*len(ordered))
        expected = {n:raw[i*size:(i+1)*size] for i,n in enumerate(ordered)}
        decoder = video_media.VideoDecoder(asset,self.ffmpeg,self.cancel)
        processes = []
        original = decoder._open
        def opened(number):
            original(number)
            processes.append(decoder.process)
        try:
            with patch.object(decoder,"_open",side_effect=opened):
                for n in reversed(numbers):
                    self.assertEqual(decoder.get(n).tobytes(),expected[n],(asset["id"],n))
            self.assertFalse(decoder.seek_disabled,"A fast read unexpectedly required a full-scan fallback")
        finally: decoder.close()
        self.assertTrue(all(p.poll() is not None for p in processes))
        return expected

    def test_fractional_cfr_b_frames_and_keyframe_boundaries(self):
        asset = self.prepare(self.source)
        self.assert_matches_full_decode(asset)

    def test_variable_rate_and_long_timestamp_gaps(self):
        for name, select in (("vfr","if(lt(mod(t,4),2),not(mod(n,5)),not(mod(n,2)))"),
                             ("gap","lt(t,0.1)+gt(t,6.5)")):
            path = self.work/f"{name}.mp4"
            self.ff(["-i",str(self.source),"-vf",f"select='{select}'","-fps_mode","vfr",
                     "-c:v","libx264","-g","90","-bf","3","-sc_threshold","0","-an",str(path)])
            self.assert_matches_full_decode(self.prepare(path))

    def test_positive_origin_and_missing_initial_timestamps(self):
        for name, shift, extension in (("positive",5.375,"mp4"),("negative",-2.375,"mkv")):
            path = self.work/f"{name}.{extension}"
            self.ff(["-copyts","-i",str(self.source),"-vf",f"setpts=PTS+({shift})/TB",
                     "-fps_mode","passthrough","-avoid_negative_ts","disabled",
                     "-c:v","libx264","-g","90","-bf","3","-an",str(path)])
            asset = self.prepare(path,require_origin=name=="positive")
            if name == "positive": self.assertNotEqual(asset["decode_origin"]["pts"],0)
            else: self.assertIsNone(asset["decode_origin"])
            self.assert_matches_full_decode(asset)

    def test_rotation_and_video_after_audio_stream(self):
        path = self.work/"audio-first.mp4"
        self.ff(["-itsoffset","1.25","-i",str(self.source),"-f","lavfi","-i","sine=d=14:r=48000",
                 "-map","1:a:0","-map","0:v:0","-c:v","copy","-c:a","aac",str(path)])
        asset = self.prepare(path)
        self.assertEqual(asset["video_stream"],1)
        self.assert_matches_full_decode(asset)
        rotated = self.work/"rotated.mov"
        self.ff(["-display_rotation","90","-i",str(self.source),"-map","0:v:0","-c","copy",str(rotated)])
        asset = self.prepare(rotated)
        self.assertEqual((asset["width"],asset["height"]),(180,320))
        self.assert_matches_full_decode(asset)

    def test_hdr_tone_mapping_after_fast_seek(self):
        path = self.work/"hdr.mp4"
        self.ff(["-f","lavfi","-i","testsrc2=s=160x90:r=24:d=9","-vf","format=yuv420p10le",
                 "-c:v","libx265","-preset","ultrafast","-x265-params",
                 "pools=1:frame-threads=1:log-level=error:keyint=72:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
                 "-an",str(path)])
        self.assert_matches_full_decode(self.prepare(path))

    def test_vp9_webm_seek(self):
        path = self.work/"vp9.webm"
        self.ff(["-i",str(self.source),"-c:v","libvpx-vp9","-deadline","realtime","-cpu-used","8",
                 "-g","90","-an",str(path)])
        self.assert_matches_full_decode(self.prepare(path))

    def test_missing_or_invalid_origin_keeps_legacy_path(self):
        asset = self.prepare(self.source)
        for origin in (None,{},dict(pts="bad",time_base="1/30000"),dict(pts=0,time_base="1/0")):
            decoder = video_media.VideoDecoder(dict(asset,decode_origin=origin),self.ffmpeg,self.cancel)
            try:
                decoder.get(200)
                self.assertFalse(decoder.used_seek)
            finally: decoder.close()

    def test_empty_fast_read_retries_once_and_closes_both_processes(self):
        asset = self.prepare(self.source)
        decoder = video_media.VideoDecoder(asset,self.ffmpeg,self.cancel)
        reference = video_media.VideoDecoder(dict(asset,decode_origin=None),self.ffmpeg,self.cancel)
        original = decoder._open
        opens, processes = [], []
        def opened(number):
            original(number); opens.append(decoder.used_seek); processes.append(decoder.process)
            if decoder.used_seek:
                pipe = decoder.process.stdout
                decoder.process.stdout = Mock(wraps=pipe)
                decoder.process.stdout.read.return_value = b""
        try:
            with patch.object(decoder,"_open",side_effect=opened):
                self.assertEqual(decoder.get(250).tobytes(),reference.get(250).tobytes())
            self.assertEqual(opens,[True,False])
            self.assertTrue(decoder.seek_disabled)
        finally: decoder.close(); reference.close()
        self.assertTrue(all(p.poll() is not None for p in processes))

    def test_metadata_roundtrip_and_ui_refresh_preserve_origin(self):
        asset = self.prepare(self.source)
        project = core.fresh(); project["video_assets"] = [asset]
        project["videos"] = [core.video_defaults(asset["id"],0,30)]
        path = self.work/"origin-project.json"
        core.save_project(project,path)
        loaded = core.load_project(path)
        self.assertEqual(loaded["video_assets"][0]["decode_origin"],asset["decode_origin"])
        target = {}; EditorApp._video_metadata(target,asset)
        self.assertEqual(target["decode_origin"],asset["decode_origin"])
        self.assertIsNot(target["decode_origin"],asset["decode_origin"])
        EditorApp._video_metadata(target,dict(asset,decode_origin=None))
        self.assertIsNone(target["decode_origin"])

    def test_cancellation_after_fast_seek_releases_process_without_retry(self):
        asset = self.prepare(self.source)
        cancel = threading.Event()
        decoder = video_media.VideoDecoder(asset,self.ffmpeg,cancel)
        try:
            decoder.get(200)
            self.assertTrue(decoder.used_seek)
            process = decoder.process
            cancel.set(); process.wait(timeout=3)
            with patch.object(decoder,"_open",wraps=decoder._open) as opened:
                with self.assertRaisesRegex(core.EditorError,"취소"): decoder.get(201)
                opened.assert_not_called()
            self.assertFalse(decoder.seek_disabled)
        finally: decoder.close()

    def test_existing_proxy_gains_origin_metadata_without_reencoding(self):
        asset = self.prepare(self.source)
        proxy = Path(asset["proxy"])
        manifest = proxy.with_suffix(".json")
        legacy = json.loads(manifest.read_text(encoding="utf-8"))
        legacy.pop("decode_origin")
        manifest.write_text(json.dumps(legacy),encoding="utf-8")
        modified = proxy.stat().st_mtime_ns
        with patch.object(core,"_run_encoder",wraps=core._run_encoder) as encode:
            upgraded = self.prepare(self.source)
            encode.assert_not_called()
        self.assertEqual(upgraded["decode_origin"],asset["decode_origin"])
        self.assertEqual(proxy.stat().st_mtime_ns,modified)
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["decode_origin"],asset["decode_origin"])


class OriginProbeTests(unittest.TestCase):
    def test_probe_cancel_does_not_launch_process(self):
        cancel = threading.Event(); cancel.set()
        with patch.object(video_media.subprocess,"Popen") as process:
            with self.assertRaisesRegex(core.EditorError,"취소"):
                video_media.probe_decode_origin("unused",0,"1/30","unused",cancel)
            process.assert_not_called()

    def test_probe_timeout_terminates_optional_probe_and_returns_no_origin(self):
        with patch.object(video_media.subprocess,"Popen") as start, \
                patch.object(video_media.time,"monotonic",side_effect=[0,11]):
            process = start.return_value
            process.communicate.side_effect = [subprocess.TimeoutExpired("probe",.1),(b"",b"")]
            process.poll.return_value = None
            self.assertIsNone(video_media.probe_decode_origin("unused",0,"1/30","unused",threading.Event()))
            process.kill.assert_called_once()


if __name__ == "__main__": unittest.main()
