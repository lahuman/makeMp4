"""Build and smoke-check a portable Windows folder before replacing the previous one."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import wave

from PIL import Image

ROOT = Path(__file__).resolve().parent
RELEASE = ROOT / "release" / "MusicToVideo"
SOURCE_FFMPEG = ROOT / "ffmpeg"
if not (SOURCE_FFMPEG / "bin" / "ffmpeg.exe").is_file():
    SOURCE_FFMPEG = RELEASE / "ffmpeg"


def main():
    if not all((SOURCE_FFMPEG / item).is_file() for item in
               ("bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE", "README.txt")):
        raise SystemExit("FFmpeg/FFprobe 및 LICENSE/README를 ffmpeg 폴더에 배치하세요.")
    build = ROOT / "build"
    candidate_root = build / "release-candidate"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--windowed", "--onedir", "--exclude-module", "numpy",
               "--name", "MusicToVideo", "--distpath", str(candidate_root),
               "--workpath", str(build / "pyinstaller"), str(ROOT / "music_to_video.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    candidate = candidate_root / "MusicToVideo"
    dest_ff = candidate / "ffmpeg"
    (dest_ff / "bin").mkdir(parents=True, exist_ok=True)
    for item in ("bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE", "README.txt"):
        shutil.copy2(SOURCE_FFMPEG / item, dest_ff / item)
    for name in ("NOTICE-FFmpeg.txt", "music_to_video.py", "editor_core.py",
                 "editor_ui.py", "editor_ui_v2.py", "requirements.txt", "README.md",
                 "VALIDATION.md", "LICENSE-PortAudio.txt", "LICENSE-sounddevice.txt"):
        shutil.copy2(ROOT / name, candidate / name)
    with tempfile.TemporaryDirectory(prefix="music_video_smoke_") as tmp:
        folder = Path(tmp)
        audio, image, output = folder / "시험 음악.wav", folder / "세로 사진.png", folder / "결과.mp4"
        with wave.open(str(audio), "wb") as file:
            file.setnchannels(1); file.setsampwidth(2); file.setframerate(48000)
            data = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * n / 48000)))
                            for n in range(48000))
            file.writeframes(data)
        Image.new("RGB", (300, 500), "#2070c0").save(image)
        result = subprocess.run([str(candidate / "MusicToVideo.exe"), "--convert",
                                 str(audio), str(image), str(output)],
                                timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not output.is_file():
            raise RuntimeError("배포 EXE의 실제 MP4 변환 검사에 실패했습니다.")
        probe = subprocess.run([str(dest_ff / "bin" / "ffprobe.exe"), "-v", "error",
                                "-show_entries", "format=duration:stream=codec_name,width,height,pix_fmt",
                                "-of", "json", str(output)], capture_output=True, text=True, check=True)
        info = json.loads(probe.stdout)
        streams = info["streams"]
        video = next(x for x in streams if x["codec_name"] == "h264")
        if (video["width"], video["height"], video["pix_fmt"]) != (1920, 1080, "yuv420p"):
            raise RuntimeError("배포 EXE의 영상 규격 검사에 실패했습니다.")
        if not any(x["codec_name"] == "aac" for x in streams):
            raise RuntimeError("배포 EXE의 AAC 검사에 실패했습니다.")
    RELEASE.parent.mkdir(exist_ok=True)
    ready = RELEASE.parent / ("MusicToVideo.ready-" + uuid.uuid4().hex[:8])
    shutil.copytree(candidate, ready)
    backup = RELEASE.parent / ("MusicToVideo.previous-" + uuid.uuid4().hex[:8])
    if RELEASE.exists(): RELEASE.rename(backup)
    try:
        ready.rename(RELEASE)
    except Exception:
        if backup.exists(): backup.rename(RELEASE)
        raise
    if backup.exists():
        resolved = backup.resolve()
        if resolved.parent != (ROOT / "release").resolve() or not resolved.name.startswith("MusicToVideo.previous-"):
            raise RuntimeError("배포 백업 경로가 예상 범위를 벗어났습니다.")
        shutil.rmtree(resolved)
    print("배포 완료:", RELEASE)


if __name__ == "__main__":
    main()
