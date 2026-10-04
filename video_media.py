"""FFmpeg video preparation and bounded, sequential frame readers."""
from __future__ import annotations

from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from PIL import Image

import editor_core as core

FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NORMALIZE = "setpts=PTS-STARTPTS,fps=fps=30:start_time=0:round=near:eof_action=round"


def probe_video(path, ffprobe, cancel=None, count=False):
    command = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json"]
    if count: command += ["-count_frames"]
    try:
        process = subprocess.Popen(command + [str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=FLAGS)
        while True:
            try:
                stdout, stderr = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired:
                if cancel and cancel.is_set():
                    process.kill(); process.communicate()
                    raise core.EditorError("영상 분석을 취소했습니다.")
        if process.returncode: raise core.EditorError("영상을 읽을 수 없습니다. 파일 손상과 코덱을 확인하세요.")
        data = json.loads(stdout)
        videos = [s for s in data["streams"] if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")]
        if not videos: raise core.EditorError("실제 영상 스트림이 없는 파일입니다.")
        video = next((s for s in videos if s.get("disposition", {}).get("default")), videos[0])
        audios = [s for s in data["streams"] if s.get("codec_type") == "audio"]
        audio = next((s for s in audios if s.get("disposition", {}).get("default")), audios[0] if audios else None)
        width, height = int(video["width"]), int(video["height"])
        if width <= 0 or height <= 0: raise ValueError("해상도가 없습니다.")
        sar = video.get("sample_aspect_ratio", "1:1").split(":")
        ratio = float(sar[0]) / float(sar[1]) if len(sar) == 2 and float(sar[1]) else 1
        width *= ratio if ratio > 0 else 1
        rotation = next((float(s["rotation"]) for s in video.get("side_data_list", []) if "rotation" in s),
                        float(video.get("tags", {}).get("rotate", 0)))
        if round(rotation) % 180: width, height = height, width
        factor = min(1, 1920/width, 1080/height)
        width, height = max(2, round(width*factor/2)*2), max(2, round(height*factor/2)*2)
        duration = float(video.get("duration", data.get("format", {}).get("duration", 0)))
        if not math.isfinite(duration) or duration <= 0: raise ValueError("영상 길이를 확인할 수 없습니다.")
        frames = video.get("nb_read_frames" if count else "nb_frames", "0")
        return {"width": width, "height": height, "duration": duration,
                "frames": int(frames) if str(frames).isdigit() else 0,
                "video_stream": video["index"], "audio_stream": audio["index"] if audio else None,
                "has_audio": bool(audio), "rotation": rotation,
                "video_start": float(video.get("start_time", 0)),
                "audio_start": float(audio.get("start_time", 0)) if audio else 0}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise core.EditorError("영상 정보 분석 실패: " + str(e)) from e


def prepare_video(path, ffmpeg, ffprobe, cancel, cache_dir=None, progress=None):
    last_report = (-1, "")
    def report(percent, stage):
        nonlocal last_report
        current = (max(last_report[0], min(100, int(percent))), stage)
        if current != last_report:
            last_report = current
            if progress: progress(*current)

    if cancel.is_set(): raise core.EditorError("영상 분석을 취소했습니다.")
    report(0, "영상 정보 확인")
    source = Path(path).resolve()
    stat = source.stat()
    key = hashlib.sha256(f"video-v3-1|{source}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()
    folder = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "Seonyuldam_Video"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / (key + ".json")
    try:
        cached = json.loads(manifest.read_text(encoding="utf-8"))
        if (Path(cached["proxy"]).is_file() and Path(cached["thumbnail"]).is_file()
                and (not cached["has_audio"] or Path(cached["pcm"]).stat().st_size == cached["frames"]*(core.RATE//core.FPS)*8)):
            if cancel.is_set(): raise core.EditorError("영상 분석을 취소했습니다.")
            report(100, "가져오기 완료")
            return cached
    except (OSError, ValueError, KeyError, TypeError): pass
    info = probe_video(source, ffprobe, cancel)
    # Stage weights describe completed work, not a prediction of remaining time.
    proxy_end = 80 if info["has_audio"] else 96
    report(2, "미리보기 생성")
    scale = min(1, 960/info["width"], 540/info["height"])
    pw, ph = max(2, round(info["width"]*scale/2)*2), max(2, round(info["height"]*scale/2)*2)
    token = core.uid()
    parts = {kind: folder / f"{key}.{token}.{suffix}" for kind, suffix in
             (("proxy", "part.mp4"), ("thumbnail", "part.png"), ("pcm", "part.f32le"))}
    final = {kind: folder / f"{key}.{suffix}" for kind, suffix in
             (("proxy", "mp4"), ("thumbnail", "png"), ("pcm", "f32le"))}
    try:
        core._run_encoder([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(source),
                           "-map", f"0:{info['video_stream']}", "-vf", NORMALIZE + f",scale={pw}:{ph},setsar=1",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-g", "15",
                           "-pix_fmt", "yuv420p", "-an", str(parts["proxy"])], cancel,
                          output_time=lambda seconds: report(2 + (proxy_end - 2) * min(1, seconds / info["duration"]), "미리보기 생성"))
        report(proxy_end, "미리보기 확인")
        proxy_info = probe_video(parts["proxy"], ffprobe, cancel)
        if not proxy_info["frames"]: proxy_info = probe_video(parts["proxy"], ffprobe, cancel, count=True)
        frames = proxy_info["frames"]
        if frames <= 0: raise core.EditorError("디코딩할 영상 프레임이 없습니다.")
        core._run_encoder([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
                           "-i", str(parts["proxy"]), "-frames:v", "1", str(parts["thumbnail"])], cancel)
        bins = []
        if info["has_audio"]:
            report(82, "소리 추출")
            delay = info["audio_start"] - info["video_start"]
            samples = frames * (core.RATE // core.FPS)
            audio_filter = (f"asetpts=PTS-STARTPTS+({delay:.9f})/TB,"
                            f"aresample={core.RATE}:async=1:first_pts=0,apad,atrim=end_sample={samples}")
            core._run_encoder([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(source),
                               "-map", f"0:{info['audio_stream']}", "-vn", "-af", audio_filter,
                               "-ac", "2", "-ar", str(core.RATE), "-c:a", "pcm_f32le", "-f", "f32le",
                               str(parts["pcm"])], cancel,
                              output_time=lambda seconds: report(82 + 12 * min(1, seconds / (frames / core.FPS)), "소리 추출"))
            if parts["pcm"].stat().st_size != samples*8:
                raise core.EditorError("영상 오디오 길이가 일치하지 않습니다.")
            report(94, "파형 분석")
            with parts["pcm"].open("rb") as stream:
                while raw := stream.read((core.RATE//100)*8):
                    if cancel.is_set(): raise core.EditorError("영상 분석을 취소했습니다.")
                    values = array("f"); values.frombytes(raw)
                    bins.append(max((abs(v) for v in values), default=0))
                    report(94 + 5 * stream.tell() / (samples * 8), "파형 분석")
        report(99, "마무리")
        if cancel.is_set(): raise core.EditorError("영상 분석을 취소했습니다.")
        for kind in parts:
            if parts[kind].is_file(): os.replace(parts[kind], final[kind])
        info.update(frames=frames, proxy_width=pw, proxy_height=ph, bins=bins,
                    proxy=str(final["proxy"]), thumbnail=str(final["thumbnail"]),
                    pcm=str(final["pcm"]) if info["has_audio"] else None)
        temp_manifest = folder / f"{key}.{token}.part.json"
        parts["manifest"] = temp_manifest
        temp_manifest.write_text(json.dumps(info), encoding="utf-8")
        os.replace(temp_manifest, manifest)
        report(100, "가져오기 완료")
        return info
    finally:
        for part in parts.values(): part.unlink(missing_ok=True)


class VideoDecoder:
    def __init__(self, asset, ffmpeg, cancel, preview=False):
        self.asset, self.ffmpeg, self.cancel, self.preview = asset, ffmpeg, cancel, preview
        self.width = asset["proxy_width"] if preview else asset["width"]
        self.height = asset["proxy_height"] if preview else asset["height"]
        self.process = None
        self.number = -1
        self.image = None
        self.errors = []
        self.finished = threading.Event()
        self.watchdog = None
        self.reader = None

    def _open(self, number):
        self.close()
        self.finished = threading.Event()
        self.errors = []
        command = [self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
        if self.preview:
            command += ["-ss", f"{number/core.FPS:.9f}", "-i", self.asset["proxy"], "-map", "0:v:0"]
            filters = f"scale={self.width}:{self.height},setsar=1"
        else:
            command += ["-i", self.asset["path"], "-map", f"0:{self.asset['video_stream']}"]
            # Normalize before trimming to preserve the same VFR frame selection as the proxy.
            filters = NORMALIZE + f",trim=start_frame={number},scale={self.width}:{self.height},setsar=1"
        command += ["-vf", filters, "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-fps_mode", "passthrough", "pipe:1"]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, creationflags=FLAGS)
        except OSError as e: raise core.EditorError("영상 디코더 실행 실패: " + str(e)) from e
        process = self.process
        def drain():
            for raw in iter(process.stderr.readline, b""):
                self.errors.append(raw.decode("utf-8", "replace"))
                if len(self.errors) > 20: del self.errors[:10]
        def watch():
            while not self.finished.wait(.05):
                if self.cancel.is_set():
                    if process.poll() is None: process.terminate()
                    return
        self.reader = threading.Thread(target=drain, daemon=True); self.reader.start()
        self.watchdog = threading.Thread(target=watch, daemon=True); self.watchdog.start()
        self.number = number - 1
        self.image = None

    def get(self, number):
        if self.cancel.is_set(): raise core.EditorError("작업을 취소했습니다.")
        if number == self.number and self.image is not None: return self.image
        if self.process is None or number < self.number or number-self.number > 8:
            self._open(number)
        while self.number < number:
            count = self.width*self.height*3
            raw = bytearray()
            while len(raw) < count:
                data = self.process.stdout.read(count-len(raw))
                if not data:
                    if self.cancel.is_set(): raise core.EditorError("작업을 취소했습니다.")
                    raise core.EditorError("영상 프레임을 읽지 못했습니다. 원본을 다시 연결하세요.\n" + "".join(self.errors)[-500:])
                raw.extend(data)
            self.image = Image.frombytes("RGB", (self.width, self.height), bytes(raw))
            self.number += 1
        return self.image

    def close(self):
        self.finished.set()
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try: self.process.wait(2)
                except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
            if self.watchdog: self.watchdog.join(1)
            if self.reader: self.reader.join(1)
            self.process.stdout.close(); self.process.stderr.close()
            self.process = None
        self.image = None


class VideoFrameProvider:
    def __init__(self, assets, ffmpeg, cancel, preview=False):
        self.assets = {a["id"]: a for a in assets}
        self.ffmpeg, self.cancel, self.preview = ffmpeg, cancel, preview
        self.decoders = {}

    def begin_frame(self, clips):
        active = {c["id"] for c in clips if "source_in_frame" in c}
        for ident in list(self.decoders):
            if ident not in active: self.decoders.pop(ident).close()

    def get(self, clip, frame):
        asset = self.assets.get(clip["asset"])
        if not asset: raise core.EditorError("영상 원본을 찾을 수 없습니다.")
        if clip["id"] not in self.decoders:
            self.decoders[clip["id"]] = VideoDecoder(asset, self.ffmpeg, self.cancel, self.preview)
        number = clip["source_in_frame"] + ((clip.get("loop_offset_frame", 0) + frame - clip["start"])
                                           % (clip["source_out_frame"] - clip["source_in_frame"]))
        return self.decoders[clip["id"]].get(number)

    def __enter__(self): return self

    def __exit__(self, *error): self.close()

    def close(self):
        for decoder in self.decoders.values(): decoder.close()
        self.decoders.clear()
