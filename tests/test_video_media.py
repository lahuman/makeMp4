"""Video timing, linked audio, real FFmpeg rendering and Tk editing regressions."""
from array import array
import copy
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageStat, ImageChops, ImageGrab

import editor_core as core
from editor_ui_v2 import EditorApp
from seonyuldam import bundled_tool
import video_media


class VideoTimingTests(unittest.TestCase):
    def project(self):
        project = core.fresh()
        project["video_assets"] = [{"id": "v", "path": "missing.mp4", "frames": 600, "has_audio": True}]
        clip = core.video_defaults("v", 600, 300)
        clip.update(source_in_frame=150, source_out_frame=450, audio_enabled=True)
        project["videos"] = [clip]
        return project, clip

    def test_video_only_duration_and_linked_source_range(self):
        project, clip = self.project()
        self.assertTrue(core.valid_video(project, clip))
        self.assertEqual(core.duration_seconds(project), 30)
        audio = core.timeline_audio(project)[0]
        self.assertEqual((audio["start_sample"], audio["source_in"], audio["source_out"]),
                         (20*core.RATE, 5*core.RATE, 15*core.RATE))
        clip["audio_enabled"] = False
        self.assertEqual(core.timeline_audio(project), [])
        self.assertEqual(core.duration_seconds(project), 30)

    def test_music_and_video_duration_uses_latest_end(self):
        project, clip = self.project()
        project["audio_clips"] = [core.audio_defaults("music", 0, core.RATE*60)]
        self.assertEqual(core.duration_seconds(project), 60)
        clip["start"] = 2100; clip["end"] = core.video_end(clip)
        self.assertEqual(core.duration_seconds(project), 80)
        project["images"] = [core.image_defaults("image", 0, 9000)]
        self.assertEqual(core.duration_seconds(project), 80)

    def test_gain_is_applied_before_final_mix_clamp(self):
        clips = [{"asset": "a", "start_sample": 0, "source_in": 0, "source_out": 3, "gain": 2},
                 {"asset": "b", "start_sample": 0, "source_in": 0, "source_out": 3, "gain": .5}]
        streams = {"a": BytesIO(array("f", [.75]*6).tobytes()), "b": BytesIO(array("f", [-1]*6).tobytes())}
        raw = core.mixed_audio_chunk(clips, streams, 0, 3)
        values = array("f"); values.frombytes(raw)
        self.assertEqual(list(values), [1]*6)
        streams["a"].seek(0)
        raw = core.mixed_audio_chunk(clips[:1], streams, 0, 3)
        values = array("f"); values.frombytes(raw)
        self.assertEqual(list(values), [1]*6)

    def test_project_roundtrip_and_v2_migration_preserve_trim(self):
        with tempfile.TemporaryDirectory() as folder:
            project, clip = self.project()
            path = Path(folder)/"project.json"
            source = Path(folder)/"clip.mp4"
            source.write_bytes(b"fixture")
            project["video_assets"][0]["path"] = str(source)
            core.save_project(project, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["video_assets"][0]["path"], "clip.mp4")
            self.assertNotIn("proxy", saved["video_assets"][0])
            loaded = core.load_project(path)
            self.assertEqual(loaded["videos"], project["videos"])
            self.assertEqual(core.missing_media(loaded), [])
            loaded["videos"][0]["source_out_frame"] = 999
            path.write_text(json.dumps(loaded), encoding="utf-8")
            with self.assertRaises(core.EditorError): core.load_project(path)
            project = core.fresh(); project["version"] = 2
            music = core.audio_defaults("a", 100, 1000); music["source_in"] = 300
            project["audio_clips"] = [music]
            project["images"] = [core.image_defaults("one",0,10), core.image_defaults("two",0,10)]
            core.save_project(project, path)
            loaded = core.load_project(path)
            self.assertEqual(loaded["version"], 3)
            self.assertEqual(loaded["audio_clips"], [music])
            self.assertEqual([c["order"] for c in loaded["images"]], [0,1])
            self.assertTrue(loaded["_migrated"])
            self.assertFalse(loaded.get("_migrated_v1"))


class RealVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe = bundled_tool("ffmpeg"), bundled_tool("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe: raise unittest.SkipTest("FFmpeg/FFprobe required")
        base = Path(__file__).resolve().parents[1]/"build"/"video-tests"
        base.mkdir(parents=True, exist_ok=True)
        cls.folder = tempfile.TemporaryDirectory(dir=base)
        cls.addClassCleanup(cls.folder.cleanup)
        cls.work = Path(cls.folder.name)
        cls.source = cls.work/"시험 영상.mp4"
        cls.run_ff(["-f", "lavfi", "-i", "color=red:s=160x90:r=30:d=1[r];color=blue:s=160x90:r=30:d=1[b];[r][b]concat=n=2:v=1:a=0",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "2", str(cls.source)])
        cls.cancel = threading.Event()
        cls.prepared = video_media.prepare_video(cls.source, cls.ffmpeg, cls.ffprobe, cls.cancel, cls.work/"cache")

    @classmethod
    def run_ff(cls, arguments):
        return subprocess.run([cls.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y"]+arguments,
                              capture_output=True, check=True, timeout=60, creationflags=video_media.FLAGS).stdout

    def project(self, start=0, count=60):
        project = core.fresh()
        asset = dict(self.prepared, id="v", path=str(self.source))
        project["video_assets"] = [asset]
        project["videos"] = [core.video_defaults("v", start, count)]
        return project

    def test_preparation_cache_and_bounded_seek_match_original(self):
        cached = video_media.prepare_video(self.source, self.ffmpeg, self.ffprobe, self.cancel, self.work/"cache")
        self.assertEqual(cached, self.prepared)
        self.assertEqual(cached["frames"], 60)
        self.assertTrue(cached["has_audio"])
        self.assertEqual(Path(cached["pcm"]).stat().st_size, 2*core.RATE*8)
        project = self.project()
        with video_media.VideoFrameProvider(project["video_assets"],self.ffmpeg,self.cancel) as original, \
                video_media.VideoFrameProvider(project["video_assets"],self.ffmpeg,self.cancel,preview=True) as proxy:
            for frame in (0,1,29,30,45,5,59):
                a = original.get(project["videos"][0],frame)
                b = proxy.get(project["videos"][0],frame)
                self.assertLess(max(ImageStat.Stat(ImageChops.difference(a,b)).mean), 8)
                color = a.getpixel((80,45))
                self.assertGreater(color[0] if frame<30 else color[2], 230)
            processes = [d.process for d in original.decoders.values()]
        self.assertTrue(all(p.poll() is not None for p in processes))

    def test_overlapping_video_copies_and_image_order(self):
        project = self.project(count=30)
        first = project["videos"][0]; first["order"] = 0
        second = core.video_defaults("v",0,30)
        second.update(source_in_frame=30,source_out_frame=60,order=1)
        project["videos"].append(second)
        image = self.work/"overlay.png"; Image.new("RGB",(160,90),"lime").save(image)
        project["assets"] = [{"id":"p","path":str(image)}]
        top = core.image_defaults("p",0,30); top["order"] = 2
        project["images"] = [top]
        with video_media.VideoFrameProvider(project["video_assets"],self.ffmpeg,self.cancel) as provider:
            frame = core.render_scene(project,0,video_frames=provider)
            self.assertEqual(frame.getpixel((960,540)),(0,255,0))
            top["order"] = -1
            frame = core.render_scene(project,0,video_frames=provider)
            self.assertGreater(frame.getpixel((960,540))[2],230)
            self.assertEqual(len(provider.decoders),2)

    def test_video_only_export_trim_gaps_and_silent_audio(self):
        project = self.project(start=6,count=12)
        clip = project["videos"][0]
        clip.update(source_in_frame=30,source_out_frame=42)
        output = self.work/"무음 결과.mp4"
        core.export_video(project,.6,output,self.ffmpeg,self.cancel)
        result = subprocess.run([self.ffprobe,"-v","error","-show_streams","-show_format","-of","json",str(output)],
                                capture_output=True,check=True,creationflags=video_media.FLAGS)
        info = json.loads(result.stdout)
        video = next(s for s in info["streams"] if s["codec_type"]=="video")
        self.assertEqual((video["width"],video["height"],video["pix_fmt"],video["r_frame_rate"]),(1920,1080,"yuv420p","30/1"))
        self.assertEqual(int(video["nb_frames"]),18)
        self.assertTrue(any(s["codec_name"]=="aac" for s in info["streams"]))
        raw = self.run_ff(["-i",str(output),"-vf","select=eq(n\\,0)+eq(n\\,6),scale=16:9",
                           "-an","-f","rawvideo","-pix_fmt","rgb24","-fps_mode","passthrough","pipe:1"])
        self.assertLess(max(raw[:16*9*3]),8)
        picture = Image.frombytes("RGB",(16,9),raw[16*9*3:2*16*9*3])
        self.assertGreater(picture.getpixel((8,4))[2],220)
        pcm = self.run_ff(["-i",str(output),"-vn","-f","f32le","-ac","2","pipe:1"])
        values = array("f"); values.frombytes(pcm)
        self.assertLess(max(abs(v) for v in values),.0001)

    def test_linked_audio_mix_matches_playback_with_trim_gain_and_fades(self):
        project = self.project(start=3,count=15)
        clip = project["videos"][0]
        clip.update(source_in_frame=30,source_out_frame=45,audio_enabled=True,audio_gain=.5,
                    fade_in_samples=4800,fade_out_samples=4800)
        paths = {"video:v": self.prepared["pcm"]}
        mix = self.work/"mix.f32le"
        count = core.assemble_audio(project,paths,mix,self.cancel)
        with Path(paths["video:v"]).open("rb") as stream:
            expected = core.mixed_audio_chunk(core.timeline_audio(project),{"video:v":stream},0,count)
        self.assertEqual(mix.read_bytes(),expected)
        self.assertEqual(expected[:3*(core.RATE//core.FPS)*8],bytes(3*(core.RATE//core.FPS)*8))
        output = self.work/"소리 결과.mp4"
        core.export_video(project,count/core.RATE,output,self.ffmpeg,self.cancel,pcm_paths=paths)
        raw = self.run_ff(["-i",str(output),"-vn","-f","f32le","-ar","48000","-ac","2","pipe:1"])
        values = array("f"); values.frombytes(raw)
        self.assertGreater(max(abs(v) for v in values),.01)

    def test_portrait_rotation_vfr_and_delayed_audio(self):
        portrait = self.work/"portrait.mp4"
        self.run_ff(["-f","lavfi","-i","testsrc2=s=90x160:r=24:d=1","-c:v","libx264","-an",str(portrait)])
        rotated = self.work/"rotated.mp4"
        self.run_ff(["-display_rotation","90","-i",str(portrait),"-c","copy",str(rotated)])
        prepared = video_media.prepare_video(rotated,self.ffmpeg,self.ffprobe,self.cancel,self.work/"cache")
        self.assertEqual((prepared["width"],prepared["height"]),(160,90))
        self.assertFalse(prepared["has_audio"])
        vfr = self.work/"vfr.mp4"
        self.run_ff(["-i",str(self.source),"-vf","select='if(lt(t,1),not(mod(n,2)),1)'",
                     "-fps_mode","vfr","-an",str(vfr)])
        prepared = video_media.prepare_video(vfr,self.ffmpeg,self.ffprobe,self.cancel,self.work/"cache")
        asset = dict(prepared,id="vfr",path=str(vfr)); clip = core.video_defaults("vfr",0,prepared["frames"])
        with video_media.VideoFrameProvider([asset],self.ffmpeg,self.cancel) as original, \
                video_media.VideoFrameProvider([asset],self.ffmpeg,self.cancel,preview=True) as proxy:
            for number in (0,20,29,30,prepared["frames"]-1):
                a,b = original.get(clip,number),proxy.get(clip,number)
                self.assertLess(max(ImageStat.Stat(ImageChops.difference(a,b)).mean),10)
        delayed = self.work/"delayed.mp4"
        self.run_ff(["-f","lavfi","-i","color=red:s=160x90:r=30:d=1",
                     "-itsoffset","0.3","-f","lavfi","-i","sine=frequency=700:sample_rate=48000:duration=0.6",
                     "-c:v","libx264","-c:a","aac","-t","1",str(delayed)])
        prepared = video_media.prepare_video(delayed,self.ffmpeg,self.ffprobe,self.cancel,self.work/"cache")
        values = array("f"); values.frombytes(Path(prepared["pcm"]).read_bytes())
        self.assertLess(max(abs(v) for v in values[:int(.2*core.RATE)*2]),.0001)
        self.assertGreater(max(abs(v) for v in values[int(.4*core.RATE)*2:int(.6*core.RATE)*2]),.01)

    def test_cancel_and_invalid_file_leave_no_partial_cache(self):
        cancel = threading.Event(); cancel.set()
        folder = self.work/"cancel-cache"
        with self.assertRaisesRegex(core.EditorError,"취소"):
            video_media.prepare_video(self.source,self.ffmpeg,self.ffprobe,cancel,folder)
        self.assertEqual(list(folder.glob("*.part*")),[])
        bad = self.work/"bad.mp4"; bad.write_bytes(b"not a video")
        with self.assertRaises(core.EditorError): video_media.probe_video(bad,self.ffprobe)

    def test_active_decoder_and_export_cancel_release_processes_and_output(self):
        cancel = threading.Event()
        project = self.project()
        with video_media.VideoFrameProvider(project["video_assets"],self.ffmpeg,cancel) as provider:
            provider.get(project["videos"][0],0)
            process = next(iter(provider.decoders.values())).process
            cancel.set()
            process.wait(timeout=3)
            with self.assertRaises(core.EditorError): provider.get(project["videos"][0],1)
        self.assertIsNotNone(process.poll())
        cancel = threading.Event()
        output = self.work/"cancelled-output.mp4"
        timer = threading.Timer(.1,cancel.set); timer.start()
        try:
            with self.assertRaisesRegex(core.EditorError,"취소"): core.export_video(project,2,output,self.ffmpeg,cancel)
        finally: timer.cancel(); timer.join()
        self.assertFalse(output.exists())
        self.assertEqual(list(self.work.glob(".cancelled-output_*.tmp.mp4")),[])

    def test_tk_video_import_trim_preview_playback_and_undo(self):
        with patch.dict(os.environ,LOCALAPPDATA=str(self.work/"ui")), \
                patch("editor_ui_v2.filedialog.askopenfilenames",return_value=(str(self.source),)):
            root = tk.Tk(); root.withdraw()
            app = EditorApp(root)
            app._save_ui_settings = lambda: None
            errors = []; root.report_callback_exception = lambda *error: errors.append(error)
            def settle_until(check, seconds=15):
                limit = time.monotonic()+seconds
                while not check() and time.monotonic()<limit:
                    root.update(); time.sleep(.01)
                self.assertTrue(check())
            try:
                root.deiconify(); root.update()
                app.choose_video()
                ident = app.project["video_assets"][0]["id"]
                settle_until(lambda: ident in app.video_cache)
                buttons = [child for panel in app.left.winfo_children() for child in panel.winfo_children()
                           if child.winfo_class()=="TButton" and child.cget("text") in ("+ 음악","+ 이미지","+ 영상")]
                self.assertEqual(len(buttons),3)
                for button in buttons:
                    self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),app.left.winfo_rootx()+app.left.winfo_width())
                app.library_selection=ident; app.add_selected_asset()
                self.assertEqual(app.duration,2)
                clip=app.project["videos"][0]
                app._commit("source_in_seconds","1",clip["id"])
                self.assertEqual((clip["source_in_frame"],clip["end"]),(30,30))
                settle_until(lambda: app.video_preview_key == app.preview_requested)
                self.assertGreater(app.video_preview_picture.getpixel((960,540))[2],230)
                root.update_idletasks()
                ImageGrab.grab(window=root.winfo_id()).save(self.work.parent/"video-feature-preview.png")
                app._start_audio(); self.assertTrue(app.playing); self.assertTrue(app.silent_playback)
                start=app.position
                settle_until(lambda: app.position>start+.1)
                app._stop_audio(); app.seek(0)
                app.snap.set(False)
                y=app.image_y+25
                app._timeline_down(SimpleNamespace(x=1,y=y,state=0))
                app._timeline_move(SimpleNamespace(x=19,y=y,state=0))
                app._timeline_up(SimpleNamespace(x=19,y=y,state=0))
                self.assertEqual((clip["start"],clip["source_in_frame"]),(10,40))
                app.undo_action(); clip=app.project["videos"][0]
                self.assertEqual((clip["start"],clip["source_in_frame"]),(0,30))
                app.duplicate_selected()  # Undo clears the selection.
                app.selection=("video",clip["id"]); app.duplicate_selected()
                self.assertEqual(len(app.project["videos"]),2)
                app.delete_selected(); self.assertEqual(len(app.project["videos"]),1)
                app.project_file = self.work/"saved-video.json"
                self.assertTrue(app.save_project())
                saved = json.loads(app.project_file.read_text(encoding="utf-8"))
                self.assertNotIn("proxy",saved["video_assets"][0])
                with patch("editor_ui_v2.filedialog.askopenfilename",return_value=str(app.project_file)):
                    app.open_project()
                settle_until(lambda: ident in app.video_cache)
                self.assertEqual(app.project["videos"][0]["source_in_frame"],30)
                app.start_export()
                self.assertTrue(app.export_dialog.winfo_exists())
                app._close_export_dialog()
                self.assertEqual(errors,[])
            finally:
                app._stop_audio(); app.audio_cancel.set(); app._dispose_videos()
                for timer in root.tk.call("after","info"): root.tk.call("after","cancel",timer)
                root.destroy()


if __name__ == "__main__": unittest.main()
