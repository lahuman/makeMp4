"""Continuity and undo guarantees for local clip editing operations."""
from array import array
import copy
from io import BytesIO
import json
import math
import os
import subprocess
from pathlib import Path
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops

import editor_core as core
from editor_ui_v2 import EditorApp
from seonyuldam import bundled_tool
import video_media


class ClipEditingTests(unittest.TestCase):
    def test_audio_split_retains_samples_fades_gain_and_loop_phase(self):
        raw = array("f", [.6*math.sin(n/19) for n in range(core.RATE*4)]).tobytes()
        for loop, point in ((False, 17), (True, 17), (True, 1)):
            project = core.fresh()
            clip = core.audio_defaults("a", 77, core.RATE)
            clip.update(gain=1.3, fade_in_samples=48000, fade_out_samples=20000)
            if loop: clip.update(duration_samples=95000, loop_offset_samples=1234)
            count = core.audio_end(clip)+30
            expected = core.mixed_audio_chunk([clip], {"a":BytesIO(raw)}, 0, count)
            halves = core.split_clip(project, "audio", clip, point)
            actual = core.mixed_audio_chunk(halves, {"a":BytesIO(raw)}, 0, count)
            self.assertEqual(expected, actual)
            # Repeated division must keep the original envelope and source phase.
            left, right = core.split_clip(project, "audio", halves[1], point+1)
            self.assertEqual(expected, core.mixed_audio_chunk([halves[0], left, right], {"a":BytesIO(raw)}, 0, count))

    def test_linked_video_audio_split_preserves_partial_loops(self):
        project = core.fresh()
        project["video_assets"] = [dict(id="v", has_audio=True, frames=18)]
        video = core.video_defaults("v", 0, 18)
        video.update(audio_enabled=True, duration_frames=50, end=50, loop_offset_frame=7,
                     audio_gain=.7, fade_in_samples=50000, fade_out_samples=40000)
        project["videos"] = [video]
        raw = array("f", [.25, -.3]*18*(core.RATE//core.FPS)).tobytes()
        expected = core.mixed_audio_chunk(core.timeline_audio(project), {"video:v":BytesIO(raw)}, 0, 80000)
        project["videos"] = list(core.split_clip(project, "video", video, 23))
        self.assertTrue(all(core.valid_video(project, c) for c in project["videos"]))
        self.assertEqual(expected, core.mixed_audio_chunk(core.timeline_audio(project), {"video:v":BytesIO(raw)}, 0, 80000))
        for frame in range(50):
            part = next(c for c in project["videos"] if c["start"] <= frame < c["end"])
            self.assertEqual((7+frame)%18, part["source_in_frame"] +
                             (part.get("loop_offset_frame", 0)+frame-part["start"])%18)

    def test_image_split_preserves_every_motion_and_transition_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            project = core.fresh()
            for ident, color in (("a", "red"), ("b", "blue")):
                path = Path(folder)/(ident+".png")
                Image.new("RGB", (64,36), color).save(path)
                project["assets"].append(dict(id=ident, path=str(path)))
            previous = core.image_defaults("a", 0, 12, (64,36))
            incoming = core.image_defaults("b", 12, 42, (64,36))
            incoming.update(motion=True)
            incoming["to"].update(x=48, y=20, zoom=140)
            for effect in ("cut", "dissolve", "fade_black", "slide"):
                incoming["transition"] = dict(type=effect, frames=12)
                for point in (13, 17, 30, 41):
                    project["images"] = [previous, incoming]
                    with patch.object(core, "project_size", return_value=(64,36)):
                        expected = [core.render_scene(project, f) for f in range(42)]
                        halves = core.split_clip(project, "image", incoming, point)
                        project["images"] = [previous, *halves]
                        for frame, original in enumerate(expected):
                            delta = ImageChops.difference(original, core.render_scene(project, frame))
                            self.assertIsNone(delta.getbbox(), (effect, point, frame))
                        if effect == "dissolve" and point == 17:
                            saved = Path(folder)/"split.json"
                            core.save_project(project, saved)
                            restored = core.load_project(saved)
                            for frame, original in enumerate(expected):
                                self.assertIsNone(ImageChops.difference(original, core.render_scene(restored, frame)).getbbox())

    def test_rotation_flip_bounds_and_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"corners.png"
            image = Image.new("RGBA", (8,4), "red")
            for x in range(4,8):
                for y in range(4): image.putpixel((x,y), (0,0,255,255))
            image.save(path)
            project = core.fresh(); project["assets"] = [dict(id="a", path=str(path))]
            clip = core.image_defaults("a", 0, 30, (8,8)); project["images"] = [clip]
            with patch.object(core, "project_size", return_value=(8,8)):
                cache = core.LayerCache()
                before = core.render_scene(project, 0, cache=cache)
                clip["flip_h"] = True
                self.assertEqual(core.render_scene(project, 0, cache=cache).getpixel((1,3)), (0,0,255))
                clip.update(flip_h=False, rotation=90)
                after = core.render_scene(project, 0, cache=cache)
                self.assertEqual(core.image_bounds(project, clip, 0), (2,0,6,8))
                self.assertEqual(after.getpixel((3,1)), (255,0,0))
                self.assertEqual(after.getpixel((3,6)), (0,0,255))
                self.assertIsNotNone(ImageChops.difference(before, after).getbbox())

    def test_mute_preserves_volume_and_gain_changes_samples(self):
        clip = core.audio_defaults("a",0,20)
        clip.update(gain=.4, fade_in_samples=0, fade_out_samples=0)
        raw = array("f", [.5, -.5]*20).tobytes()
        output = array("f"); output.frombytes(core.mixed_audio_chunk([clip], {"a":BytesIO(raw)}, 0, 20))
        self.assertAlmostEqual(output[0], .2)
        clip["muted"] = True
        self.assertEqual(core.mixed_audio_chunk([clip], {"a":BytesIO(raw)}, 0, 20), bytes(160))
        self.assertEqual(clip["gain"], .4)
        for gain in (float("nan"), -1, 2.01):
            self.assertFalse(core.valid_audio({}, dict(clip, gain=gain)))

    def test_gap_respects_every_track_and_sample_boundaries(self):
        project = core.fresh()
        project["audio_clips"] = [core.audio_defaults("a",0,1601), core.audio_defaults("a",8000,1000)]
        self.assertEqual(core.empty_gap_at(project, 2), (2,5))
        self.assertIsNone(core.empty_gap_at(project, 1))
        project["texts"] = [dict(start=2, end=5)]
        self.assertIsNone(core.empty_gap_at(project, 2))

    def test_split_boundaries_fail_without_changing_clip(self):
        clip = core.image_defaults("a", 10, 20)
        original = copy.deepcopy(clip)
        for point in (0,10,20,30):
            with self.assertRaises(core.EditorError): core.split_clip({}, "image", clip, point)
        self.assertEqual(clip, original)

    def test_actual_mp4_with_rotated_split_loop_and_linked_volume(self):
        ffmpeg, ffprobe = bundled_tool("ffmpeg"), bundled_tool("ffprobe")
        if not ffmpeg or not ffprobe: self.skipTest("FFmpeg/FFprobe required")
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source, pcm, output = work/"원본.mp4", work/"audio.pcm", work/"분할 결과.mp4"
            def run(args):
                return subprocess.run([ffmpeg, "-v", "error", "-y", *args],
                    check=True, capture_output=True, timeout=60, creationflags=video_media.FLAGS).stdout
            run(["-f","lavfi","-i","testsrc2=s=160x90:r=30:d=1", "-f","lavfi","-i", "sine=frequency=440:sample_rate=48000:duration=1",
                 "-c:v","libx264","-c:a","aac",str(source)])
            run(["-i",str(source),"-vn","-ar","48000","-ac","2","-f","f32le",str(pcm)])
            project = core.fresh()
            project["video_assets"] = [dict(video_media.probe_video(source, ffprobe), id="v", path=str(source))]
            clip = core.video_defaults("v",0,30,(160,90))
            clip.update(duration_frames=36, end=36, rotation=90, flip_h=True, audio_enabled=True, audio_gain=.5)
            project["videos"] = list(core.split_clip(project,"video",clip,11))
            with patch.object(core,"project_size",return_value=(160,90)):
                core.export_video(project,1.2,output,ffmpeg,threading.Event(),pcm_paths={"video:v":pcm})
            metadata = json.loads(subprocess.check_output([ffprobe,"-v","error","-show_streams","-of","json",str(output)]))
            video = next(s for s in metadata["streams"] if s["codec_type"]=="video")
            self.assertEqual((video["width"],video["height"],video["nb_frames"]),(160,90,"36"))
            frames = run(["-i",str(output),"-map","0:v:0","-f","rawvideo","-pix_fmt","rgb24","-"])
            self.assertEqual(len(frames),160*90*3*36)
            samples = array("f"); samples.frombytes(run(["-i",str(output),"-vn","-f","f32le","-ac","2","-"]))
            rms = math.sqrt(sum(v*v for v in samples)/len(samples))
            original = array("f"); original.frombytes(pcm.read_bytes())
            source_rms = math.sqrt(sum(v*v for v in original)/len(original))
            self.assertAlmostEqual(rms/source_rms,.5,delta=.06)


class ClipEditingUITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, LOCALAPPDATA=self.folder.name)
        env.start(); self.addCleanup(env.stop)
        self.root = tk.Tk()
        self.addCleanup(self.destroy)
        self.app = EditorApp(self.root)
        for name in ("_recheck_audio", "_recheck_videos"):
            guard = patch.object(self.app, name)
            guard.start(); self.addCleanup(guard.stop)
        self.root.update()

    def destroy(self):
        for timer in self.root.tk.call("after", "info"): self.root.tk.call("after", "cancel", timer)
        self.root.destroy()

    def test_split_undo_and_gap_closure_move_linked_tracks_together(self):
        app = self.app
        app.project["audio_assets"] = [dict(id="a", path="audio.wav", samples=96000)]
        clip = core.audio_defaults("a", 0, 96000)
        app.project["audio_clips"] = [clip]
        app.selection = ("audio",clip["id"]); app.position = 1
        original = copy.deepcopy(app.project)
        app.split_selected()
        self.assertEqual(len(app.project["audio_clips"]), 2)
        self.assertEqual(app.selection[1], app.project["audio_clips"][1]["id"])
        app.undo_action(); self.assertEqual(app.project, original)
        app.redo_action(); self.assertEqual(len(app.project["audio_clips"]), 2)
        app.project["audio_clips"] = [core.audio_defaults("a", 0, 48000), core.audio_defaults("a", 144000, 48000)]
        app.project["texts"] = [dict(id="t",start=90,end=120,text="Caption",x=960,y=100,width=300,size=32)]
        app.position = 2
        before = copy.deepcopy(app.project)
        app.close_gap()
        self.assertEqual(app.project["audio_clips"][1]["start_sample"], 48000)
        self.assertEqual(app.project["texts"][0]["start"], 30)
        app.undo_action(); self.assertEqual(app.project, before)

    def test_gain_mute_and_split_do_not_steal_text_shortcut(self):
        app = self.app
        app.project["audio_assets"] = [dict(id="a",path="music.wav",samples=96000)]
        clip = core.audio_defaults("a",0,96000); app.project["audio_clips"] = [clip]
        app.selection = ("audio",clip["id"]); app.position = 1; app._refresh()
        app._commit("gain_percent", "35", clip["id"])
        app._commit("audio_muted", True, clip["id"])
        self.assertEqual((clip["gain"], clip["muted"]), (.35, True))
        entry = tk.Entry(self.root)
        self.assertIsNone(app._shortcut(type("Event", (), {"widget":entry})(), app.split_selected))
        self.assertEqual(len(app.project["audio_clips"]), 1)
        app.exporting = True
        app.split_selected()
        self.assertEqual(len(app.project["audio_clips"]), 1)

    def test_visual_tools_fit_small_window_and_rotate_with_undo(self):
        app = self.app
        path = Path(self.folder.name)/"image.png"
        Image.new("RGB",(80,40),"green").save(path)
        app.project["assets"] = [dict(id="i",path=str(path))]
        app.project["audio_clips"] = [core.audio_defaults("a",0,48000)]
        clip = core.image_defaults("i",0,30)
        app.project["images"] = [clip]; app.selection = ("image",clip["id"])
        self.root.geometry("1040x680"); app._refresh(); self.root.update()
        app._orient_visual("rotation")
        self.assertEqual(clip["rotation"],90)
        self.assertEqual(str(app.visual_action_buttons[-1].cget("state")),"normal")
        last = app.visual_action_buttons[-1]
        self.assertLessEqual(last.winfo_x()+last.winfo_width(),app.preview_actions.winfo_width())
        app.undo_action()
        self.assertEqual(app.project["images"][0].get("rotation",0),0)


if __name__ == "__main__": unittest.main()
