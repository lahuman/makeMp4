"""Run from the repository root: python tests/benchmark_preview_seek.py.

The baseline disables the new frame cache and drops the decoder on idle, as
before this change. Pauses are signalled, not slept through; timing measures
frame retrieval and process cleanup, excluding time spent waiting for the user.
"""
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from seonyuldam import bundled_tool
import video_media


def main():
    root = Path(__file__).resolve().parents[1] / "build" / "preview-seek"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "benchmark-90s-960x540.mp4"
    ffmpeg, ffprobe = bundled_tool("ffmpeg"), bundled_tool("ffprobe")
    if not source.exists():
        subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        "testsrc2=s=960x540:r=30:d=90", "-c:v", "libx264", "-preset", "ultrafast",
                        "-crf", "25", "-g", "15", "-pix_fmt", "yuv420p", "-an", str(source)],
                       check=True, timeout=120, creationflags=video_media.FLAGS)
    info = video_media.probe_video(source, ffprobe)
    assert info["frames"] == 2700
    asset = dict(info, path=str(source), proxy=str(source), proxy_width=960, proxy_height=540)
    sequence = [2500,2501,2502,2503,2504,2505,2506,2507,2508,None,
                2504,2503,2508,2509,2510,2400,2403,2401,2405,2689,2690]
    expected = None
    runs = []
    for repeat in range(3):
        for baseline in ((True, False) if repeat % 2 == 0 else (False, True)):
            decoder = video_media.VideoDecoder(asset, ffmpeg, threading.Event(), preview=True)
            if baseline: decoder.cache_limit = 0
            opened = decoder._open
            starts = [0]
            def counted(number):
                starts[0] += 1
                opened(number)
            decoder._open = counted
            digest = hashlib.sha256()
            peak_cache = 0
            begin = time.perf_counter()
            try:
                for number in sequence:
                    if number is None:
                        if baseline: decoder.close()
                        else: decoder.suspend()
                    else:
                        digest.update(decoder.get(number).tobytes())
                        peak_cache = max(peak_cache, decoder.cache_bytes)
            finally: decoder.close()
            elapsed = time.perf_counter() - begin
            if expected is None: expected = digest.hexdigest()
            assert digest.hexdigest() == expected, "Frame mismatch between baseline and optimized"
            run = dict(mode="baseline" if baseline else "cached", repeat=repeat+1,
                       seconds=elapsed, process_starts=starts[0], cache_bytes_peak=peak_cache)
            runs.append(run)
            print(json.dumps(run), flush=True)
    medians = {mode: statistics.median(r["seconds"] for r in runs if r["mode"] == mode)
               for mode in ("baseline", "cached")}
    result = dict(source=str(source), size=[960,540], frames=2700, sequence=sequence,
                  runs=runs, medians=medians, matching_frame_sha256=expected,
                  reduction_percent=(1-medians["cached"]/medians["baseline"])*100,
                  scope="Decoder-only mixed scrubbing; excludes compositor, Tk drawing, and actual idle waits")
    (root / "benchmark.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__": main()
