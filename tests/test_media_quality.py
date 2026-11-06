"""Pixel parity, ICC/HDR handling and real local audio mastering."""
from array import array
import copy
from io import BytesIO
import json
import math
import os
from pathlib import Path
import random
import subprocess
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image, ImageCms, ImageChops, ImageStat

import editor_core as core
from editor_ui_v2 import EditorApp
from seonyuldam import bundled_tool
import video_media


class ColorAndCompositeTests(unittest.TestCase):
    def test_cropped_composites_match_full_frame_including_transitions_and_text(self):
        with tempfile.TemporaryDirectory() as folder:
            rng = random.Random(19)
            project = core.fresh()
            for i in range(3):
                path = Path(folder) / f"{i}.png"
                image = Image.new("RGBA", (73, 41))
                image.putdata([tuple(rng.randrange(256) for _ in range(4)) for _ in range(73*41)])
                image.save(path)
                project["assets"].append(dict(id=str(i), path=str(path)))
            first = core.image_defaults("0", 0, 10, (320,180))
            second = core.image_defaults("1", 10, 30, (320,180))
            second.update(motion=True, rotation=90, flip_h=True)
            second["from"].update(x=-30, zoom=130)
            second["to"].update(x=250, zoom=55)
            top = core.image_defaults("2", 0, 30, (320,180))
            top["from"].update(x=290, y=155, zoom=25)
            project["images"] = [first, second, top]
            project["texts"] = [dict(id="t", start=0, end=30, text="색상 Alpha", x=150, y=90,
                                      width=200, size=20, background_alpha=100)]
            def reference(canvas, layer):
                canvas.paste(Image.alpha_composite(canvas, layer))
                return canvas
            for effect in ("cut", "dissolve", "fade_black", "slide"):
                second["transition"] = dict(type=effect, frames=8)
                with patch.object(core, "project_size", return_value=(320,180)):
                    cache = core.LayerCache()
                    for frame in (0,9,10,11,13,17,18,29):
                        actual = core.render_scene(project, frame, cache=cache)
                        with patch.object(core, "composite_layer", side_effect=reference):
                            expected = core.render_scene(project, frame)
                        self.assertEqual(actual.tobytes(), expected.tobytes(), (effect,frame))

    def test_opaque_and_empty_layers_match_reference_without_mutating_inputs(self):
        for mode in ("RGB", "RGBA"):
            image = Image.new(mode, (64,36), "orange")
            for x, zoom in ((160,100), (-500,100), (310,50)):
                cue = core.image_defaults("x", 0, 3, (320,180))
                cue["from"].update(x=x, zoom=zoom)
                layer = core.picture_layer(image, cue, 0, (320,180))
                original = layer.tobytes()
                canvas = Image.new("RGBA", (320,180), (20,60,100,155))
                expected = Image.alpha_composite(canvas, layer)
                core.composite_layer(canvas, layer)
                self.assertEqual(canvas.tobytes(), expected.tobytes())
                self.assertEqual(layer.tobytes(), original)

    def test_icc_conversion_changes_lab_colors_and_preserves_png_transparency(self):
        source = Image.new("LAB", (8,8), (150,160,95))
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB"))
        source.info["icc_profile"] = profile.tobytes()
        expected = ImageCms.profileToProfile(source, profile, ImageCms.createProfile("sRGB"),
                                            outputMode="RGB", renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
        actual = core.color_managed_image(source, "RGB")
        self.assertEqual(actual.tobytes(), expected.tobytes())
        self.assertNotEqual(actual.getpixel((0,0)), (150,160,95))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.png"
            image = Image.new("RGBA", (16,16), (65,140,210,83))
            image.save(path, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
            with Image.open(path) as source:
                converted = core.color_managed_image(source)
                self.assertEqual(converted.getchannel("A").tobytes(), image.getchannel("A").tobytes())
            thumb = core.thumbnail_image(path, (8,8))
            self.assertEqual(thumb.getpixel((4,4))[3], 83)

    def test_invalid_icc_is_reported_and_unprofiled_images_are_unchanged(self):
        image = Image.new("RGBA", (8,8), (40,90,120,50))
        self.assertEqual(core.color_managed_image(image).tobytes(), image.tobytes())
        image.info["icc_profile"] = b"broken profile"
        with self.assertRaisesRegex(core.EditorError, "색상 프로필"):
            core.color_managed_image(image)

    def test_unclipped_mix_preserves_headroom_and_legacy_mix_still_clamps(self):
        clips = [dict(asset="a",start_sample=0,source_in=0,source_out=5,gain=2)] * 2
        raw = array("f", [.6,-.6]*5).tobytes()
        values = array("f"); values.frombytes(core.mixed_audio_chunk(clips,{"a":BytesIO(raw)},0,5,False))
        self.assertAlmostEqual(values[0], 2.4, places=6)
        self.assertAlmostEqual(values[1], -2.4, places=6)
        clipped = array("f"); clipped.frombytes(core.mixed_audio_chunk(clips,{"a":BytesIO(raw)},0,5))
        self.assertEqual(list(clipped), [1,-1]*5)
        self.assertAlmostEqual(core.pcm_peak_db(array("f", [.5,-.5]).tobytes()), -6.0206, places=4)
        self.assertEqual(core.pcm_peak_db(bytes(16)), -math.inf)


class LocalMasteringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe = bundled_tool("ffmpeg"), bundled_tool("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe: raise unittest.SkipTest("FFmpeg required")

    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build" / "media-quality"
        root.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.folder.cleanup)
        self.work = Path(self.folder.name)
        self.cancel = threading.Event()

    def ff(self, args):
        return subprocess.run([self.ffmpeg,"-hide_banner","-nostdin","-v","error","-y"]+args,
                              capture_output=True, check=True, timeout=60,
                              creationflags=video_media.FLAGS).stdout

    def sine(self, path, amplitude, seconds=6):
        samples = round(seconds*core.RATE)
        values = array("f", (amplitude*math.sin(2*math.pi*997*(n//2)/core.RATE) for n in range(samples*2)))
        path.write_bytes(values.tobytes())
        return samples

    def test_normalization_matches_loudness_for_quiet_and_loud_sources(self):
        results = []
        for i, amplitude in enumerate((.03,.8,2.5)):
            source, output = self.work/f"in{i}.pcm", self.work/f"out{i}.pcm"
            samples = self.sine(source, amplitude)
            messages = []
            core.master_audio(source, output, samples, self.ffmpeg, self.cancel, "normalize", messages.append)
            stats = core.measure_loudness(output, self.ffmpeg, self.cancel)
            self.assertAlmostEqual(stats["input_i"], -16, delta=.5)
            self.assertLessEqual(stats["input_tp"], -1.8)
            self.assertEqual(output.stat().st_size, samples*8)
            self.assertIn("AAC 인코딩 전", messages[-1])
            results.append(stats["input_i"])
        self.assertLess(max(results)-min(results), .5)

    def test_peak_limiter_keeps_sync_length_and_prevents_flat_clipping(self):
        source, output = self.work/"hot.pcm", self.work/"limited.pcm"
        samples = self.sine(source, 1.8, 2)
        core.master_audio(source, output, samples, self.ffmpeg, self.cancel, "limit")
        values = array("f"); values.frombytes(output.read_bytes())
        self.assertEqual(len(values), samples*2)
        self.assertLessEqual(max(abs(v) for v in values), .80)
        stats = core.measure_loudness(output, self.ffmpeg, self.cancel)
        self.assertLessEqual(stats["input_tp"], -1.8)
        # A lookahead limiter retains the sine shape instead of truncating its peaks.
        flat = sum(abs(v) > .79 for v in values[core.RATE:core.RATE+4800])
        self.assertLess(flat/4800, .12)

    def test_silence_short_audio_and_cancel_are_handled(self):
        source, output = self.work/"short.pcm", self.work/"short-out.pcm"
        for amplitude in (0,.1):
            samples = self.sine(source, amplitude, .2)
            notices = []
            core.master_audio(source, output, samples, self.ffmpeg, self.cancel, "normalize", notices.append)
            self.assertEqual(output.stat().st_size, samples*8)
            self.assertIn("측정 불가", notices[-1])
            if amplitude == 0: self.assertEqual(output.read_bytes(), bytes(samples*8))
        output.unlink()
        self.cancel.set()
        with self.assertRaisesRegex(core.EditorError, "취소"):
            core.master_audio(source, output, samples, self.ffmpeg, self.cancel, "normalize")
        self.assertFalse(output.exists())

    def test_limiter_latency_compensation_preserves_impulse_sample_positions(self):
        source, output = self.work/"impulses.pcm", self.work/"aligned.pcm"
        samples = core.RATE
        values = array("f", [0]) * (samples*2)
        for n in (4800,9600,43000): values[n*2] = values[n*2+1] = .2
        source.write_bytes(values.tobytes())
        core.master_audio(source,output,samples,self.ffmpeg,self.cancel,"limit")
        actual = array("f"); actual.frombytes(output.read_bytes())
        for n in (4800,9600,43000):
            maximum = max(range(n-20,n+21),key=lambda i:abs(actual[i*2]))
            self.assertEqual(maximum,n)
            self.assertAlmostEqual(actual[n*2],actual[n*2+1],places=6)

    def test_pq_and_hlg_proxy_and_original_share_tone_mapping(self):
        for transfer in ("smpte2084", "arib-std-b67"):
            source = self.work/f"{transfer}.mp4"
            self.ff(["-f","lavfi","-i","testsrc2=s=160x90:r=24:d=1", "-vf","format=yuv420p10le",
                     "-c:v","libx265", "-preset","ultrafast", "-x265-params",
                     f"pools=1:frame-threads=1:log-level=error:colorprim=bt2020:transfer={transfer}:colormatrix=bt2020nc",
                     "-color_trc",transfer, "-color_primaries","bt2020",
                     "-colorspace","bt2020nc", "-color_range","tv", "-an",str(source)])
            prepared = video_media.prepare_video(source,self.ffmpeg,self.ffprobe,self.cancel,self.work/"cache")
            self.assertEqual(prepared["color_transfer"], transfer)
            asset = dict(prepared, id=transfer, path=str(source))
            original = video_media.VideoDecoder(asset,self.ffmpeg,self.cancel)
            proxy = video_media.VideoDecoder(asset,self.ffmpeg,self.cancel,True)
            try:
                for n in (0,15,29,7):
                    a, b = original.get(n), proxy.get(n)
                    self.assertLess(max(ImageStat.Stat(ImageChops.difference(a,b)).mean), 8)
                    self.assertGreater(max(ImageStat.Stat(a).stddev), 15)
                mapped = original.get(0)
            finally: original.close(); proxy.close()
            naive = self.ff(["-i",str(source),"-frames:v","1","-f","rawvideo","-pix_fmt","rgb24","pipe:1"])
            naive = Image.frombytes("RGB",(160,90),naive)
            self.assertGreater(sum(ImageStat.Stat(ImageChops.difference(mapped,naive)).mean)/3, 5)
            metadata = video_media.probe_video(prepared["proxy"], self.ffprobe)
            self.assertEqual(metadata["color_transfer"], "bt709")
            target = {}; EditorApp._video_metadata(target, prepared)
            self.assertEqual(target["color_transfer"], transfer)

    def test_real_mastered_mp4_has_expected_audio_peak_and_frame_count(self):
        pcm = self.work/"music.pcm"; samples = self.sine(pcm,1.4,2)
        image = self.work/"image.png"; Image.new("RGB",(160,90),(60,140,210)).save(image)
        project = core.fresh()
        project["assets"] = [dict(id="i", path=str(image))]
        project["images"] = [core.image_defaults("i",0,60,(160,90))]
        project["images"][0]["motion"] = True
        project["audio_assets"] = [dict(id="a",path=str(pcm),samples=samples)]
        clip = core.audio_defaults("a",0,samples); clip.update(fade_in_samples=0,fade_out_samples=0)
        project["audio_clips"] = [clip]
        original = copy.deepcopy(project)
        path = self.work/"result.mp4"
        with patch.object(core,"project_size",return_value=(160,90)):
            core.export_video(project,2,path,self.ffmpeg,self.cancel,pcm_paths={"a":pcm},audio_mode="normalize")
        self.assertEqual(project,original)
        streams = json.loads(subprocess.check_output([self.ffprobe,"-v","error","-show_streams","-of","json",str(path)]))["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        self.assertEqual((video["nb_frames"],video["color_space"]),("60","bt709"))
        decoded = self.work/"decoded.pcm"
        self.ff(["-i",str(path),"-vn","-ar","48000","-ac","2","-f","f32le",str(decoded)])
        stats = core.measure_loudness(decoded,self.ffmpeg,self.cancel)
        self.assertAlmostEqual(stats["input_i"],-16,delta=.6)
        self.assertLess(stats["input_tp"],-1)
        self.ff(["-xerror","-i",str(path),"-f","null",os.devnull])

    def test_hdr_composite_export_matches_tone_mapped_reference(self):
        source = self.work/"hdr.mp4"
        self.ff(["-f","lavfi","-i","testsrc2=s=160x90:r=30:d=1","-vf","format=yuv420p10le",
                 "-c:v","libx265","-preset","ultrafast","-x265-params",
                 "pools=1:frame-threads=1:log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
                 "-an",str(source)])
        prepared = video_media.prepare_video(source,self.ffmpeg,self.ffprobe,self.cancel,self.work/"cache")
        project = core.fresh()
        project["video_assets"] = [dict(prepared,id="v",path=str(source))]
        project["videos"] = [core.video_defaults("v",0,30,(160,90))]
        path = self.work/"hdr-sdr.mp4"
        with patch.object(core,"project_size",return_value=(160,90)):
            core.export_video(project,1,path,self.ffmpeg,self.cancel,audio_mode="limit")
        raw = self.ff(["-i",str(path),"-f","rawvideo","-pix_fmt","rgb24","pipe:1"])
        self.assertEqual(len(raw),30*160*90*3)
        with video_media.VideoFrameProvider(project["video_assets"],self.ffmpeg,self.cancel) as frames:
            for n in (0,15,29):
                expected = frames.get(project["videos"][0],n)
                actual = Image.frombytes("RGB",(160,90),raw[n*160*90*3:(n+1)*160*90*3])
                self.assertLess(max(ImageStat.Stat(ImageChops.difference(expected,actual)).mean),7)
        info = video_media.probe_video(path,self.ffprobe)
        self.assertEqual(info["color_transfer"],"bt709")


class AudioQualityUITests(unittest.TestCase):
    def test_audio_choice_snapshot_meter_and_export_controls_fit(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,LOCALAPPDATA=folder):
            root = tk.Tk()
            app = EditorApp(root); app._save_ui_settings = lambda: None
            try:
                root.geometry("1100x760"); root.update()
                app.duration = 1
                with patch("editor_ui.messagebox.askyesno",return_value=True): app.start_export()
                root.update()
                self.assertEqual(app.export_audio_var.get(),"피크 보호 (권장)")
                self.assertLess(app.export_dialog.winfo_reqheight(),root.winfo_screenheight()-60)
                self.assertTrue(app.export_start_button.winfo_viewable())
                app.export_audio_var.set("음량 정규화 · -16 LUFS")
                output = Path(folder)/"out.mp4"
                with patch.object(app,"tool",return_value="ffmpeg"), patch.object(threading.Thread,"start"), \
                        patch.object(core,"export_video",return_value=output) as encode:
                    app._begin_export(output)
                    app.export_audio_var.set("원래 음량")
                    app.export_thread.run()
                self.assertEqual(encode.call_args.kwargs["audio_mode"],"normalize")
                encode.call_args.kwargs["on_audio"]("출력 음량 -16 LUFS")
                app.playing = True; app.play_stop = threading.Event()
                app.events.put(("audio_meter",app.play_stop,-6.0))
                app._poll()
                self.assertEqual(app.audio_meter_var.get(),"피크 -6.0 dBFS")
                self.assertEqual(app.export_audio_status,"출력 음량 -16 LUFS")
                app._stop_audio()
                self.assertEqual(app.audio_meter_var.get(),"피크 — dBFS")
            finally:
                app.exporting = False; app.audio_cancel.set(); app._dispose_videos()
                for timer in root.tk.call("after","info"): root.tk.call("after","cancel",timer)
                root.destroy()


if __name__ == "__main__": unittest.main()
