"""Real frame accuracy and resource bounds for cached proxy scrubbing."""
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import editor_core as core
from seonyuldam import bundled_tool
import video_media


class PreviewSeekTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe = bundled_tool("ffmpeg"), bundled_tool("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe: raise unittest.SkipTest("FFmpeg required")
        root = Path(__file__).resolve().parents[1] / "build" / "preview-seek"
        root.mkdir(parents=True, exist_ok=True)
        cls.folder = tempfile.TemporaryDirectory(dir=root)
        cls.addClassCleanup(cls.folder.cleanup)
        cls.source = Path(cls.folder.name) / "long.mp4"
        cls.ff(["-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=90",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                "-g", "15", "-pix_fmt", "yuv420p", "-an", str(cls.source)])
        cls.asset = dict(video_media.probe_video(cls.source, cls.ffprobe), id="v",
                         path=str(cls.source), proxy=str(cls.source),
                         proxy_width=320, proxy_height=180)

    @classmethod
    def ff(cls, args):
        return subprocess.run([cls.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y"] + args,
                              capture_output=True, check=True, timeout=60,
                              creationflags=video_media.FLAGS).stdout

    def decoder(self, preview=True):
        decoder = video_media.VideoDecoder(self.asset, self.ffmpeg, threading.Event(), preview)
        self.addCleanup(decoder.close)
        return decoder

    def references(self, numbers):
        ordered = sorted(set(numbers))
        selection = "+".join(f"eq(n\\,{n})" for n in ordered)
        raw = self.ff(["-i", str(self.source), "-vf", f"select={selection}",
                       "-an", "-fps_mode", "passthrough", "-f", "rawvideo",
                       "-pix_fmt", "rgb24", "pipe:1"])
        size = 320 * 180 * 3
        self.assertEqual(len(raw), size * len(ordered))
        return {n: raw[i*size:(i+1)*size] for i, n in enumerate(ordered)}

    def test_long_random_seeks_match_sequential_decode_byte_for_byte(self):
        numbers = (2699, 0, 14, 15, 1350, 2689, 2690, 1)
        expected = self.references(numbers)
        decoder = self.decoder()
        for n in numbers:
            self.assertEqual(decoder.get(n).tobytes(), expected[n], n)

    def test_backward_scrubbing_preserves_forward_cursor_without_restarting(self):
        numbers = (2600, 2606, 2603, 2601, 2606, 2607, 2618)
        expected = self.references(numbers)
        decoder = self.decoder()
        with patch.object(decoder, "_open", wraps=decoder._open) as opened:
            for n in numbers:
                self.assertEqual(decoder.get(n).tobytes(), expected[n], n)
            self.assertEqual(opened.call_count, 1)
        self.assertEqual(decoder.number, 2618)

    def test_idle_releases_process_but_cached_frames_survive(self):
        decoder = self.decoder()
        first = decoder.get(2600).tobytes()
        decoder.get(2606)
        process = decoder.process
        decoder.suspend()
        self.assertIsNotNone(process.poll())
        self.assertIsNone(decoder.process)
        with patch.object(decoder, "_open", wraps=decoder._open) as opened:
            self.assertEqual(decoder.get(2600).tobytes(), first)
            self.assertEqual(opened.call_count, 0)
            decoder.get(2650)
            self.assertEqual(opened.call_count, 1)
        decoder.close()
        self.assertEqual(decoder.cache_bytes, 0)
        self.assertFalse(decoder.frames)

    def test_lru_eviction_and_overlapping_reads_keep_exact_byte_budget(self):
        decoder = self.decoder()
        decoder.cache_limit = 320 * 180 * 3 * 3
        numbers = (100, 105, 104, 103, 100, 104, 102, 106, 103)
        expected = self.references(numbers)
        for n in numbers:
            self.assertEqual(decoder.get(n).tobytes(), expected[n], n)
            self.assertEqual(decoder.cache_bytes, len(decoder.frames) * 320 * 180 * 3)
            self.assertLessEqual(decoder.cache_bytes, decoder.cache_limit)
        decoder.close()
        decoder.cache_limit = 1
        decoder.get(100)
        self.assertFalse(decoder.frames)
        self.assertEqual(decoder.cache_bytes, 0)

    def test_export_decoder_does_not_retain_frame_cache(self):
        decoder = self.decoder(preview=False)
        expected = self.references((2000, 2001))
        for n in expected:
            self.assertEqual(decoder.get(n).tobytes(), expected[n])
        self.assertFalse(decoder.frames)
        self.assertEqual(decoder.cache_bytes, 0)

    def test_preview_provider_retains_only_two_clips_and_closes_on_exit(self):
        clips = [core.video_defaults("v", 0, 2700) for _ in range(3)]
        provider = video_media.VideoFrameProvider([self.asset], self.ffmpeg, threading.Event(), True)
        self.addCleanup(provider.close)
        provider.get(clips[0], 2600)
        first = provider.decoders[clips[0]["id"]]
        process = first.process
        provider.begin_frame([])
        self.assertIsNotNone(process.poll())
        self.assertGreater(first.cache_bytes, 0)
        with patch.object(first, "_open", wraps=first._open) as opened:
            provider.get(clips[0], 2600)
            self.assertEqual(opened.call_count, 0)
        for clip in clips[1:]: provider.get(clip, 2600)
        self.assertEqual(len(provider.decoders), 2)
        self.assertFalse(first.frames)
        self.assertLessEqual(sum(d.cache_bytes for d in provider.decoders.values()),
                             2 * video_media.PREVIEW_FRAME_CACHE_BYTES)
        processes = [d.process for d in provider.decoders.values()]
        provider.close()
        self.assertTrue(all(p.poll() is not None for p in processes))

    def test_cancelled_cached_request_does_not_return_stale_success(self):
        decoder = self.decoder()
        decoder.get(2600)
        decoder.cancel.set()
        with self.assertRaisesRegex(core.EditorError, "취소"):
            decoder.get(2600)


if __name__ == "__main__": unittest.main()
