"""Build and smoke-check a portable Windows folder before replacing the previous one."""
from __future__ import annotations

import json
import hashlib
import importlib.metadata
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
from media_runtime import APP_VERSION

ROOT = Path(__file__).resolve().parent
RELEASE = ROOT / "release" / "Seonyuldam"
FFMPEG_FILES = ("bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE", "README.txt")
SOURCE_FILES = ("NOTICE-FFmpeg.txt", "seonyuldam.py", "music_to_video.py", "editor_core.py",
                "editor_ui.py", "editor_ui_v2.py", "studio_theme.py", "video_media.py",
                "project_validation.py", "media_runtime.py", "disk_cache.py", "recovery_store.py",
                "editor_session.py", "requirements.txt", "requirements-release.txt", "ffmpeg-toolchain.json",
                "README.md", "VALIDATION.md", "LICENSE-PortAudio.txt", "LICENSE-sounddevice.txt")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""): digest.update(chunk)
    return digest.hexdigest()


def validate_toolchain(folder, lock_file):
    try:
        expected = json.loads(Path(lock_file).read_text(encoding="utf-8"))
        if not isinstance(expected.get("sha256"), dict): raise ValueError("sha256 mapping required")
    except (ValueError, AttributeError) as error:
        raise RuntimeError("FFmpeg 도구 해시 파일 형식이 잘못되었습니다.") from error
    for name in FFMPEG_FILES:
        if expected["sha256"].get(name) != sha256(Path(folder)/name):
            raise RuntimeError("FFmpeg 묶음이 검증된 도구 해시와 다릅니다: " + name)
    return expected


def release_dependencies(path):
    versions = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"): continue
        requirement = line.split(";", 1)[0].strip()
        name, expected = requirement.split("==", 1)
        try: actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeError("배포 의존성이 없습니다. requirements-build.txt를 설치하세요: " + name) from error
        if actual != expected:
            raise RuntimeError(f"배포 의존성 버전 불일치: {name} ({actual}, 필요 {expected})")
        versions[name] = actual
    return versions


def write_manifest(candidate, toolchain, dependencies):
    commit_id, dirty = None, None
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        if not commit.returncode: commit_id = commit.stdout.strip()
        if not status.returncode: dirty = bool(status.stdout)
    except (OSError, subprocess.SubprocessError): pass
    files = {path.relative_to(candidate).as_posix(): sha256(path) for path in sorted(candidate.rglob("*"))
             if path.is_file() and not path.is_symlink() and path.name != "build-info.json"}
    manifest = dict(schema=1, version=APP_VERSION, python=sys.version, source_commit=commit_id,
                    source_dirty=dirty,
                    dependencies=dependencies, toolchain=toolchain, sha256=files)
    (candidate/"build-info.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def find_ffmpeg(root):
    candidates = [root/"ffmpeg", root/"release/Seonyuldam/ffmpeg", root/"release/MusicToVideo/ffmpeg"]
    candidates += sorted((root/"build").glob("*-dist/Seonyuldam/ffmpeg"), reverse=True)
    return next((folder for folder in candidates if all((folder/item).is_file() for item in FFMPEG_FILES)), None)


def promote_release(candidate, release):
    release.parent.mkdir(exist_ok=True)
    ready = release.parent / ("Seonyuldam.ready-" + uuid.uuid4().hex[:8])
    shutil.copytree(candidate, ready)
    backup = release.parent / ("Seonyuldam.previous-" + uuid.uuid4().hex[:8])
    if release.exists(): release.rename(backup)
    try:
        ready.rename(release)
    except Exception:
        if backup.exists(): backup.rename(release)
        raise
    return backup if backup.exists() else None


def main():
    source_ffmpeg = find_ffmpeg(ROOT)
    if source_ffmpeg is None:
        raise SystemExit("FFmpeg/FFprobe 및 LICENSE/README가 없습니다. ffmpeg 폴더 또는 기존 작업 빌드의 FFmpeg를 준비하세요.")
    toolchain = validate_toolchain(source_ffmpeg, ROOT / "ffmpeg-toolchain.json")
    dependencies = release_dependencies(ROOT / "requirements-release.txt")
    print("빌드 Python:", sys.executable, flush=True)
    print("FFmpeg:", source_ffmpeg, flush=True)
    build = ROOT / "build"
    candidate_root = build / "release-candidate"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--windowed", "--onedir", "--exclude-module", "numpy",
               "--icon", str(ROOT / "assets" / "app_icon.ico"),
               "--add-data", str(ROOT / "assets" / "app_icon.png") + ";assets",
               "--name", "Seonyuldam", "--distpath", str(candidate_root),
               "--specpath", str(build),
               "--workpath", str(build / "pyinstaller"), str(ROOT / "seonyuldam.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    candidate = candidate_root / "Seonyuldam"
    dest_ff = candidate / "ffmpeg"
    (dest_ff / "bin").mkdir(parents=True, exist_ok=True)
    for item in FFMPEG_FILES:
        shutil.copy2(source_ffmpeg / item, dest_ff / item)
    for name in SOURCE_FILES:
        shutil.copy2(ROOT / name, candidate / name)
    with tempfile.TemporaryDirectory(prefix="music_video_smoke_", dir=build) as tmp:
        folder = Path(tmp)
        audio, image, output = folder / "시험 음악.wav", folder / "세로 사진.png", folder / "결과.mp4"
        with wave.open(str(audio), "wb") as file:
            file.setnchannels(1); file.setsampwidth(2); file.setframerate(48000)
            data = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * n / 48000)))
                            for n in range(48000))
            file.writeframes(data)
        Image.new("RGB", (300, 500), "#2070c0").save(image)
        result = subprocess.run([str(candidate / "Seonyuldam.exe"), "--convert",
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
        subprocess.run([str(dest_ff / "bin/ffmpeg.exe"), "-v", "error", "-xerror", "-i", str(output),
                        "-f", "null", "-"], capture_output=True, check=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    write_manifest(candidate, toolchain, dependencies)
    backup = promote_release(candidate, RELEASE)
    if backup: print("이전 배포본 백업:", backup)
    print("배포 완료:", RELEASE)


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        raise SystemExit("빌드 실패: " + str(error)) from error
