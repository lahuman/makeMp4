#!/usr/bin/env python3
"""Windows launcher and simple one-image CLI compatibility entry point."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import tkinter as tk

import editor_core as core


def bundled_tool(name: str) -> str | None:
    exe = name + (".exe" if os.name == "nt" else "")
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    candidates = [app_dir / "ffmpeg" / "bin" / exe,
                  app_dir / "release" / "Seonyuldam" / "ffmpeg" / "bin" / exe,
                  app_dir / "release" / "MusicToVideo" / "ffmpeg" / "bin" / exe,
                  app_dir / "bin" / exe]
    return next((str(p) for p in candidates if p.is_file()), None) or shutil.which(name)


def convert(audio: Path, image: Path, output: Path, ffmpeg: str, ffprobe: str,
            progress=None, cancel: threading.Event | None = None) -> dict:
    """Compatibility wrapper for the original single-image conversion API."""
    audio, image, output = Path(audio), Path(image), Path(output)
    if audio.suffix.lower() not in core.AUDIO_EXTS or not audio.is_file():
        raise core.EditorError("음악은 FLAC/WAV/MP3 파일을 선택하세요.")
    if image.suffix.lower() not in core.IMAGE_EXTS or not image.is_file():
        raise core.EditorError("이미지는 JPG/JPEG/PNG 파일을 선택하세요.")
    core.probe_audio(audio, ffprobe)
    project = core.fresh()
    cancel = cancel or threading.Event()
    pcm, bins, samples = core.analyze_audio_asset(audio, ffmpeg, cancel)
    duration = samples / core.RATE
    audio_id = core.uid()
    project["audio_assets"] = [{"id": audio_id, "path": str(audio.resolve()), "samples": samples}]
    project["audio_clips"] = [{"id": core.uid(), "asset": audio_id, "start_sample": 0,
                               "source_in": 0, "source_out": samples}]
    asset = core.uid()
    project["assets"] = [{"id": asset, "path": str(image.resolve())}]
    project["images"] = [core.image_defaults(asset, 0, core.total_frames(duration))]
    result = core.export_video(project, duration, output, ffmpeg,
                               cancel, progress, {audio_id: pcm})
    return {"duration": duration, "size": result.stat().st_size}


def main() -> int:
    parser = argparse.ArgumentParser(description="선율담")
    parser.add_argument("--convert", nargs=3, metavar=("AUDIO", "IMAGE", "OUTPUT"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--ffmpeg", help=argparse.SUPPRESS)
    parser.add_argument("--ffprobe", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.convert:
        try:
            ffmpeg = args.ffmpeg or bundled_tool("ffmpeg")
            ffprobe = args.ffprobe or bundled_tool("ffprobe")
            if not ffmpeg or not ffprobe:
                raise core.EditorError("FFmpeg/FFprobe를 찾지 못했습니다.")
            reporter = (lambda n: print(f"{n}%", flush=True)) if sys.stdout else None
            result = convert(*map(Path, args.convert), ffmpeg, ffprobe, reporter)
            if sys.stdout:
                print(json.dumps(result, ensure_ascii=False))
            return 0
        except core.EditorError as e:
            if sys.stderr:
                print(f"오류: {e}", file=sys.stderr)
            return 1
    from editor_ui_v2 import EditorApp
    root = tk.Tk()
    icon_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "app_icon.png"
    if icon_path.is_file():
        app_icon = tk.PhotoImage(file=str(icon_path))
        root.iconphoto(True, app_icon)
    EditorApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
