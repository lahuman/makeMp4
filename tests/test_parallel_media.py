"""Overlapping media must render and export with the same result as preview."""
from array import array
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest

from PIL import Image

import editor_core as core


class ParallelMediaTests(unittest.TestCase):
    def test_fades_follow_clip_time_across_chunks_and_source_trim(self):
        clips = [{"asset": "a", "start_sample": 2, "source_in": 3, "source_out": 11,
                  "fade_in_samples": 3, "fade_out_samples": 3}]
        streams = {"a": BytesIO(array("f", [.5, .25] * 11).tobytes())}
        whole = core.mixed_audio_chunk(clips, streams, 0, 12)
        pieces = b"".join(core.mixed_audio_chunk(clips, streams, start, 3) for start in range(0, 12, 3))
        self.assertEqual(whole, pieces)
        samples = array("f")
        samples.frombytes(whole)
        self.assertEqual(list(samples[::2]), [0, 0, 0, .25, .5, .5, .5, .5, .25, 0, 0, 0])
        self.assertEqual(list(samples[1::2]), [x / 2 for x in samples[::2]])

    def test_faded_export_matches_playback_and_survives_project_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            project = core.fresh()
            source = Path(folder) / "source.f32le"
            source.write_bytes(array("f", [.5, .5] * 20000).tobytes())
            project["audio_assets"] = [{"id": "a", "path": str(source), "samples": 20000}]
            first = core.audio_defaults("a", 0, 20000)
            second = core.audio_defaults("a", 20000, 20000)
            project["audio_clips"] = [first, second]
            saved = Path(folder) / "project.json"
            core.save_project(project, saved)
            project = core.load_project(saved)
            self.assertEqual(project["audio_clips"][1]["fade_out_samples"], core.RATE)
            output = Path(folder) / "mix.f32le"
            core.assemble_audio(project, {"a": source}, output, threading.Event())
            with source.open("rb") as stream:
                expected = core.mixed_audio_chunk(project["audio_clips"], {"a": stream}, 0, 40000)
            self.assertEqual(output.read_bytes(), expected)
            values = array("f")
            values.frombytes(expected)
            self.assertEqual(values[0], 0)
            self.assertEqual(values[39998], 0)
            self.assertEqual(values[40000], 0)
            self.assertEqual(values[-1], 0)
            self.assertGreater(values[20000], 0)

    def test_overlapping_images_compose_in_project_order(self):
        with tempfile.TemporaryDirectory() as folder:
            background = Path(folder) / "background.png"
            foreground = Path(folder) / "foreground.png"
            Image.new("RGB", (160, 90), "red").save(background)
            Image.new("RGB", (90, 90), "blue").save(foreground)
            project = core.fresh()
            project["assets"] = [{"id": "red", "path": str(background)},
                                 {"id": "blue", "path": str(foreground)}]
            project["images"] = [core.image_defaults("red", 0, 60),
                                 core.image_defaults("blue", 0, 60)]
            self.assertEqual(len(core.active_images(project, 15)), 2)
            frame = core.render_scene(project, 15)
            self.assertEqual(frame.getpixel((30, 30)), (255, 0, 0))
            self.assertEqual(frame.getpixel((960, 540)), (0, 0, 255))

    def test_overlapping_audio_is_mixed_and_clamped(self):
        project = core.fresh()
        project["audio_assets"] = [{"id": "a", "samples": 4}, {"id": "b", "samples": 4}]
        project["audio_clips"] = [
            {"id": "one", "asset": "a", "start_sample": 0, "source_in": 0, "source_out": 4},
            {"id": "two", "asset": "b", "start_sample": 1, "source_in": 0, "source_out": 4},
        ]
        streams = {"a": BytesIO(array("f", [.75, .75] * 4).tobytes()),
                   "b": BytesIO(array("f", [.5, .5] * 4).tobytes())}
        self.assertTrue(all(core.valid_audio(project, clip) for clip in project["audio_clips"]))
        samples = array("f")
        samples.frombytes(core.mixed_audio_chunk(project["audio_clips"], streams, 0, 5))
        self.assertEqual(list(samples[::2]), [.75, 1.0, 1.0, 1.0, .5])
        self.assertEqual(list(samples[1::2]), [.75, 1.0, 1.0, 1.0, .5])

    def test_export_audio_uses_same_mix(self):
        with tempfile.TemporaryDirectory() as folder:
            project = core.fresh()
            paths = {}
            for ident, value in (("a", .25), ("b", .5)):
                source = Path(folder) / f"{ident}.f32le"
                source.write_bytes(array("f", [value, value] * 4).tobytes())
                paths[ident] = source
                project["audio_assets"].append({"id": ident, "samples": 4})
            project["audio_clips"] = [
                {"id": "one", "asset": "a", "start_sample": 0, "source_in": 0, "source_out": 4},
                {"id": "two", "asset": "b", "start_sample": 1, "source_in": 0, "source_out": 4},
            ]
            output = Path(folder) / "mix.f32le"
            self.assertEqual(core.assemble_audio(project, paths, output, threading.Event()), 5)
            samples = array("f")
            samples.frombytes(output.read_bytes())
            self.assertEqual(list(samples[::2]), [.25, .75, .75, .75, .5])

    def test_non_overlapping_audio_keeps_silence_and_source_samples(self):
        clips = [{"asset": "a", "start_sample": 0, "source_in": 0, "source_out": 2},
                 {"asset": "b", "start_sample": 4, "source_in": 0, "source_out": 2}]
        streams = {"a": BytesIO(array("f", [.25, .25] * 2).tobytes()),
                   "b": BytesIO(array("f", [.5, .5] * 2).tobytes())}
        samples = array("f")
        samples.frombytes(core.mixed_audio_chunk(clips, streams, 0, 6))
        self.assertEqual(list(samples[::2]), [.25, .25, 0, 0, .5, .5])


if __name__ == "__main__":
    unittest.main()
