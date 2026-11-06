"""Verify delivered pixels, color metadata and mixed static/motion MP4 playback."""
from array import array
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageStat

import editor_core as core
from seonyuldam import bundled_tool
from editor_ui_v2 import EditorApp


class ExportQualityTests(unittest.TestCase):
    def test_invalid_quality_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "invalid.mp4"
            with self.assertRaisesRegex(core.EditorError, "출력 품질"):
                core.export_video(core.fresh(), 1, output, "unused", threading.Event(), quality="invalid")
            self.assertFalse(output.exists())

    def test_quality_preserves_detail_and_color_across_static_motion_boundary(self):
        ffmpeg, ffprobe = bundled_tool("ffmpeg"), bundled_tool("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg/FFprobe required")
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            rng = random.Random(52)
            pixels = []
            for y in range(180):
                for x in range(320):
                    if y < 60:
                        pixels.append(((220, 30, 50), (30, 210, 70), (40, 60, 220),
                                       (0, 0, 0), (255, 255, 255))[x // 64])
                    else:
                        v = max(0, min(255, round(x * 255 / 319) + rng.randint(-25, 25)))
                        pixels.append((v, v, v))
            image = Image.new("RGB", (320, 180)); image.putdata(pixels)
            path = work / "detail.png"; image.save(path)
            pcm = work / "audio.pcm"
            pcm.write_bytes(array("f", [.02, -.02] * core.RATE).tobytes())
            project = core.fresh()
            project["assets"] = [dict(id="i", path=str(path))]
            project["audio_assets"] = [dict(id="a", path=str(pcm), samples=core.RATE)]
            project["audio_clips"] = [core.audio_defaults("a", 0, core.RATE)]
            first = core.image_defaults("i", 0, 15, (320, 180))
            second = core.image_defaults("i", 15, 30, (320, 180))
            second["motion"] = True
            project["images"] = [first, second]
            errors = {}
            for quality in core.EXPORT_QUALITIES:
                output = work / (str(len(errors)) + ".mp4")
                with patch.object(core, "project_size", return_value=(320, 180)):
                    core.export_video(project, 1, output, ffmpeg, threading.Event(),
                                      pcm_paths={"a": pcm}, quality=quality)
                metadata = json.loads(subprocess.check_output(
                    [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(output)]))
                video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
                self.assertEqual(video["codec_name"], "h264")
                self.assertEqual(video["pix_fmt"], "yuv420p")
                self.assertEqual(video["nb_frames"], "30")
                for key in ("color_space", "color_transfer", "color_primaries"):
                    self.assertEqual(video[key], "bt709")
                self.assertEqual(video["color_range"], "tv")
                self.assertTrue(any(s["codec_name"] == "aac" for s in metadata["streams"]))
                raw = subprocess.check_output([ffmpeg, "-v", "error", "-i", str(output),
                                               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
                frame_bytes = 320 * 180 * 3
                self.assertEqual(len(raw), frame_bytes * 30)
                for index in (0, 14, 15, 29):
                    decoded = Image.frombytes("RGB", (320, 180), raw[index*frame_bytes:(index+1)*frame_bytes])
                    for x in (32, 96, 160, 224, 288):
                        expected = image.getpixel((x, 30)); actual = decoded.getpixel((x, 30))
                        self.assertLess(max(abs(a-b) for a, b in zip(expected, actual)), 12)
                difference = ImageChops.difference(image.crop((0, 70, 320, 180)),
                                                  decoded.crop((0, 70, 320, 180)))
                errors[quality] = sum(ImageStat.Stat(difference).rms) / 3
            self.assertLess(errors[core.DEFAULT_EXPORT_QUALITY], errors["빠르게"])


class ExportQualityUITests(unittest.TestCase):
    def test_export_dialog_sends_selected_quality_to_worker(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, LOCALAPPDATA=folder):
            root = tk.Tk()
            try:
                app = EditorApp(root)
                app.duration = 1
                root.update()
                with patch("editor_ui.messagebox.askyesno", return_value=True):
                    app.start_export()
                root.update()
                self.assertEqual(app.export_quality_var.get(), core.DEFAULT_EXPORT_QUALITY)
                self.assertTrue(app.export_start_button.winfo_viewable())
                app.export_quality_var.set("최고화질")
                app.export_acceleration_var.set("GPU 자동 (지원 시)")
                output = Path(folder) / "output.mp4"
                with patch.object(app, "tool", return_value="ffmpeg"), \
                        patch.object(threading.Thread, "start"), \
                        patch.object(core, "export_video", return_value=output) as encode:
                    app._begin_export(output)
                    app.export_thread.run()
                self.assertEqual(encode.call_args.kwargs["quality"], "최고화질")
                self.assertEqual(encode.call_args.kwargs["acceleration"], "auto")
            finally:
                for timer in root.tk.call("after", "info"):
                    root.tk.call("after", "cancel", timer)
                root.destroy()


if __name__ == "__main__":
    unittest.main()
