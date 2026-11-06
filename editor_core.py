"""Project data, audio analysis, shared rendering, and MP4 export."""
from __future__ import annotations

import copy
from array import array
from collections import OrderedDict, deque
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import math
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid
import wave

from PIL import Image, ImageCms, ImageDraw, ImageFont, ImageOps
from project_validation import validate_shape, validate_project as validate_project_data
import media_runtime as runtime
from media_runtime import EditorError, EncoderError
from disk_cache import pin_cache, cache_write_lock

FPS = 30
SIZE = (1920, 1080)
VIDEO_SIZES = {"16:9 · 1920×1080": (1920, 1080), "9:16 · 1080×1920": (1080, 1920)}
RATE = 48000
AUDIO_EXTS = {".flac", ".wav", ".mp3"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
MAX_WAVEFORM_BINS = 60000
MAX_COMPOSITE_FRAMES = 60
EXPORT_QUALITIES = {"빠르게": ("veryfast", "23"), "고화질 (권장)": ("fast", "18"),
                    "최고화질": ("medium", "16")}
DEFAULT_EXPORT_QUALITY = "고화질 (권장)"
EXPORT_ACCELERATIONS = {"CPU (품질 우선)": "cpu", "GPU 자동 (지원 시)": "auto"}
EXPORT_AUDIO_MODES = {"원래 음량": "original", "피크 보호 (권장)": "limit",
                      "음량 정규화 · -16 LUFS": "normalize"}


def video_codec_args(encoder, quality):
    preset, value = EXPORT_QUALITIES[quality]
    if encoder == "libx264": return ["-c:v", encoder, "-preset", preset, "-crf", value]
    if encoder == "h264_qsv":
        return ["-c:v", encoder, "-preset", {"23":"fast", "18":"medium", "16":"slow"}[value], "-global_quality", value]
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", {"23":"p4", "18":"p5", "16":"p6"}[value],
                "-rc", "vbr", "-cq", value, "-b:v", "0"]
    if encoder == "h264_amf":
        return ["-c:v", encoder, "-quality", "quality", "-rc", "cqp", "-qp_i", value, "-qp_p", value]
    raise EditorError("지원하지 않는 인코더입니다.")


def probe_gpu_encoder(ffmpeg, size, quality, cancel):
    """Check actual MP4 encoding at the selected resolution, with a deadline."""
    with tempfile.TemporaryDirectory(prefix="seonyuldam_gpu_probe_") as temporary:
        for encoder in ("h264_nvenc", "h264_qsv", "h264_amf"):
            if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
            output = Path(temporary)/(encoder+".mp4")
            command = [ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                       f"color=size={size[0]}x{size[1]}:rate=30", "-frames:v", "4",
                       *video_codec_args(encoder, quality), "-pix_fmt", "yuv420p", str(output)]
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError:
                return None
            try:
                deadline = time.monotonic()+10
                while process.poll() is None:
                    if cancel.wait(.05): raise EditorError("작업을 취소했습니다.")
                    if time.monotonic() >= deadline: break
                if process.poll() == 0 and output.is_file() and output.stat().st_size > 1024:
                    return encoder
            finally:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(2)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()
    return None


def uid():
    return uuid.uuid4().hex[:12]


def fresh():
    return {"version": 3, "audio_assets": [], "audio_clips": [], "assets": [], "images": [], "texts": [],
            "video_assets": [], "videos": [],
            "settings": {"width": 1920, "height": 1080, "fps": 30, "audio_bitrate": "320k"}}


def project_size(project):
    settings = project.get("settings", {})
    if not isinstance(settings, dict):
        raise EditorError("프로젝트의 화면 해상도 설정이 잘못되었습니다.")
    size = (settings.get("width", 1920), settings.get("height", 1080))
    if any(type(value) is not int for value in size) or size not in VIDEO_SIZES.values():
        raise EditorError("화면 해상도는 1920×1080 또는 1080×1920을 선택하세요.")
    return size


def set_project_size(project, size):
    if size not in VIDEO_SIZES.values():
        raise EditorError("지원하지 않는 화면 해상도입니다.")
    old_width, old_height = project_size(project)
    if size == (old_width, old_height): return
    width, height = size
    cues = project["images"] + project.get("videos", [])
    cues += [c["transition"]["source"] for c in list(cues) if isinstance(c.get("transition", {}).get("source"), dict)]
    for cue in cues:
        for key in ("from", "to"):
            state = cue.get(key)
            if state is not None:
                state["x"] *= width / old_width
                state["y"] *= height / old_height
    for item in project["texts"]:
        item["x"] = round(item["x"] * width / old_width)
        item["y"] = round(item["y"] * height / old_height)
        item["width"] = max(80, min(width - 20, round(item["width"] * width / old_width)))
    project.setdefault("settings", {}).update(width=width, height=height)


def image_defaults(asset, start, end, size=None):
    width, height = size or SIZE
    return {"id": uid(), "asset": asset, "start": int(start), "end": int(end),
            "motion": False, "easing": "smooth", "from": {"x": width / 2, "y": height / 2, "zoom": 100},
            "to": {"x": width / 2, "y": height / 2, "zoom": 100},
            "transition": {"type": "cut", "frames": 15}}


def audio_start(clip):
    return int(clip["start_sample"])


def audio_defaults(asset, start, samples):
    return {"id": uid(), "asset": asset, "start_sample": int(start),
            "source_in": 0, "source_out": int(samples),
            "fade_in_samples": RATE, "fade_out_samples": RATE}


def audio_end(clip):
    return audio_start(clip) + clip.get("duration_samples", int(clip["source_out"]) - int(clip["source_in"]))


def video_defaults(asset, start, frames, size=None):
    clip = image_defaults(asset, start, start + frames, size)
    clip.update(source_in_frame=0, source_out_frame=int(frames), audio_enabled=False,
                audio_gain=1.0, fade_in_samples=0, fade_out_samples=0)
    return clip


def video_end(clip):
    return clip["start"] + clip.get("duration_frames", clip["source_out_frame"] - clip["source_in_frame"])


def valid_video(project, clip):
    asset = next((a for a in project.get("video_assets", []) if a["id"] == clip["asset"]), None)
    return bool(asset and all(type(clip.get(k)) is int for k in ("start", "end", "source_in_frame", "source_out_frame"))
                and 0 <= clip["source_in_frame"] < clip["source_out_frame"] <= asset.get("frames", 0)
                and type(clip.get("duration_frames", 1)) is int and clip.get("duration_frames", 1) > 0
                and type(clip.get("loop_offset_frame", 0)) is int
                and ("duration_frames" in clip or clip.get("loop_offset_frame", 0) == 0)
                and 0 <= clip.get("loop_offset_frame", 0) < clip["source_out_frame"] - clip["source_in_frame"]
                and clip["start"] >= 0 and clip["end"] == video_end(clip)
                and math.isfinite(clip.get("audio_gain", 1)) and 0 <= clip.get("audio_gain", 1) <= 2)


def visual_clips(project):
    images = project["images"]
    videos = project.get("videos", [])
    return [clip for _, clip in sorted(enumerate(images + videos),
                                      key=lambda pair: pair[1].get("order", pair[0] if pair[0] < len(images) else len(images)))]


def next_visual_order(project):
    return max((c.get("order", i) for i, c in enumerate(visual_clips(project))), default=-1) + 1


def timeline_audio(project):
    """Derive linked video audio so trimming never creates a second source of truth."""
    clips = list(project["audio_clips"])
    assets = {a["id"]: a for a in project.get("video_assets", [])}
    for video in project.get("videos", []):
        asset = assets.get(video["asset"])
        if video.get("audio_enabled") and asset and asset.get("has_audio"):
            linked = {"id": video["id"], "asset": "video:" + video["asset"],
                          "start_sample": video["start"] * (RATE // FPS),
                          "source_in": video["source_in_frame"] * (RATE // FPS),
                          "source_out": video["source_out_frame"] * (RATE // FPS),
                          "gain": video.get("audio_gain", 1.0),
                          "fade_in_samples": video.get("fade_in_samples", 0),
                          "fade_out_samples": video.get("fade_out_samples", 0)}
            if "duration_frames" in video:
                linked.update(duration_samples=video["duration_frames"] * (RATE // FPS),
                              loop_offset_samples=video.get("loop_offset_frame", 0) * (RATE // FPS))
            for key in ("envelope_length_samples", "envelope_offset_samples"):
                if key in video: linked[key] = video[key]
            clips.append(linked)
    return clips


def duration_samples(project):
    return max([0] + [audio_end(c) for c in project["audio_clips"]]
               + [video_end(c) * (RATE // FPS) for c in project.get("videos", [])])


def duration_seconds(project):
    return duration_samples(project) / RATE


def valid_interval(items, start, end, exclude=None, start_key="start", end_key="end"):
    return start >= 0 and end > start and all(
        item["id"] == exclude or end <= item[start_key] or start >= item[end_key]
        for item in items)


def valid_audio(project, clip, exclude=None):
    return (audio_start(clip) >= 0 and clip["source_in"] >= 0
            and clip["source_out"] > clip["source_in"]
            and math.isfinite(clip.get("gain", 1)) and 0 <= clip.get("gain", 1) <= 2)


def split_clip(project, kind, clip, frame):
    """Return independent halves while retaining source, motion and fade phase."""
    audio = kind == "audio"
    unit = RATE // FPS
    point = int(frame) * unit if audio else int(frame)
    start = audio_start(clip) if audio else clip["start"]
    end = audio_end(clip) if audio else clip["end"]
    if not start < point < end:
        raise EditorError("재생 헤드를 선택한 클립의 시작과 끝 사이에 놓으세요.")
    left, right = copy.deepcopy(clip), copy.deepcopy(clip)
    right["id"] = uid()
    offset = point - start
    if audio or kind == "video":
        suffix = "samples" if audio else "frames"
        source_in = "source_in" if audio else "source_in_frame"
        source_out = "source_out" if audio else "source_out_frame"
        duration_key = "duration_" + suffix
        offset_key = "loop_offset_" + ("samples" if audio else "frame")
        if duration_key in clip:
            cycle = clip[source_out] - clip[source_in]
            left[duration_key], right[duration_key] = offset, end - point
            right[offset_key] = (clip.get(offset_key, 0) + offset) % cycle
        else:
            left[source_out] = clip[source_in] + offset
            right[source_in] = clip[source_in] + offset
        scale = 1 if audio else unit
        for part, advance in ((left, 0), (right, offset)):
            part["envelope_length_samples"] = clip.get("envelope_length_samples", (end-start)*scale)
            part["envelope_offset_samples"] = clip.get("envelope_offset_samples", 0) + advance*scale
    if audio:
        right["start_sample"] = point
    else:
        left["end"], right["start"] = point, point
        if clip.get("motion"):
            a, b = clip.get("easing_range", (0, 1))
            for part, first, last in ((left, start, point-1), (right, point, end-1)):
                part["from"] = dict(transform_at(clip, first))
                part["to"] = dict(transform_at(clip, last))
                part["easing_range"] = [a+(b-a)*(first-start)/(end-start-1),
                                        a+(b-a)*(last-start)/(end-start-1)]
        if kind == "image":
            effect, length, previous = transition_info(project, clip)
            if effect != "cut":
                transition = clip.get("transition", {})
                phase = transition.get("offset_frames", 0)
                full = transition.get("frames", length) if "source" in transition else length
                for part, advance in ((left, 0), (right, offset)):
                    part["transition"] = dict(type=effect, frames=full, offset_frames=phase+advance,
                                               source=copy.deepcopy({k:v for k,v in previous.items() if k != "transition"}))
    return left, right


def empty_gap_at(project, frame):
    """Find a frame-aligned interval empty across all tracks at the playhead."""
    unit = RATE // FPS
    intervals = [(audio_start(c), audio_end(c)) for c in project["audio_clips"]]
    intervals += [(c["start"]*unit, c["end"]*unit)
                  for c in visual_clips(project) + project["texts"]]
    cursor = 0
    for start, end in sorted(intervals):
        begin_frame, end_frame = (cursor+unit-1)//unit, start//unit
        if begin_frame <= frame < end_frame:
            return begin_frame, end_frame
        cursor = max(cursor, end)
    return None


def validate_project(project):
    try:
        validate_project_data(project)
        project_size(project)
        if any(not valid_video(project, clip) for clip in project.get("videos", [])):
            raise ValueError("영상 클립의 원본 범위 또는 시간이 잘못되었습니다.")
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        raise EditorError("프로젝트 형식이 잘못되었습니다.\n" + str(error)) from error


def paths_in(project, project_file):
    base = Path(project_file).parent
    data = copy.deepcopy(project)
    for asset in data["assets"] + data.get("audio_assets", []) + data.get("video_assets", []):
        asset["path"] = str((base / asset["path"]).resolve())
    if data.get("audio"):
        data["audio"] = str((base / data["audio"]).resolve())
    return data


def save_project(project, path):
    validate_project(project)
    path = Path(path)
    transient = {"bins", "proxy", "proxy_width", "proxy_height", "pcm", "thumbnail"}
    data = copy.deepcopy(dict(project, video_assets=[{k:v for k,v in asset.items() if k not in transient}
                                                     for asset in project.get("video_assets", [])]))
    data.pop("_migrated", None)
    data.pop("_migrated_v1", None)
    def relative(value):
        try:
            return os.path.relpath(Path(value).resolve(), path.parent.resolve())
        except ValueError:
            return str(Path(value).resolve())
    if data.get("audio"):
        data["audio"] = relative(data["audio"])
    for asset in data["assets"] + data.get("audio_assets", []) + data.get("video_assets", []):
        asset["path"] = relative(asset["path"])
    temp = path.with_name("." + path.name + "." + uid() + ".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    except OSError as e:
        raise EditorError(f"프로젝트 저장 실패: 폴더 권한과 여유 공간을 확인하세요.\n{e}") from e
    finally:
        temp.unlink(missing_ok=True)


def load_project(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        validate_shape(data)
        data = paths_in(data, path)
        project_size(data)
        if data["version"] == 1:
            migrate_v1(data)
            data["_migrated_v1"] = True
        if data["version"] == 2:
            data["version"] = 3
            data["_migrated"] = True
        data.setdefault("video_assets", [])
        data.setdefault("videos", [])
        for index, clip in enumerate(data["images"]):
            clip.setdefault("order", index)
        validate_project(data)
        return data
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as e:
        raise EditorError(f"프로젝트를 열 수 없습니다. JSON 파일 형식을 확인하세요.\n{e}") from e


def missing_media(project):
    result = []
    if project.get("audio") and not Path(project["audio"]).is_file():
        result.append(("audio", None, project["audio"]))
    for a in project.get("audio_assets", []):
        if not Path(a["path"]).is_file():
            result.append(("audio", a["id"], a["path"]))
    for a in project["assets"]:
        if not Path(a["path"]).is_file():
            result.append(("image", a["id"], a["path"]))
    for a in project.get("video_assets", []):
        if not Path(a["path"]).is_file():
            result.append(("video", a["id"], a["path"]))
    return result


def migrate_v1(project):
    """Keep v1 timing and its native-size (never enlarged) image display."""
    old_audio = project.pop("audio", "")
    project["audio_assets"] = []
    project["audio_clips"] = []
    duration = 0.0
    if old_audio:
        try:
            # The precise decoded sample count is filled by the UI after analysis.
            duration = float(project.get("duration", 0))
        except (TypeError, ValueError):
            pass
        ident = uid()
        project["audio_assets"].append({"id": ident, "path": old_audio, "samples": int(duration * RATE)})
        project["audio_clips"].append({"id": uid(), "asset": ident, "start_sample": 0,
                                       "source_in": 0, "source_out": int(duration * RATE)})
    cues = sorted(project["images"], key=lambda c: c["start"])
    fallback_end = max((t["end"] for t in project["texts"]), default=1)
    if duration:
        fallback_end = max(fallback_end, total_frames(duration))
    for i, cue in enumerate(cues):
        cue["end"] = cues[i+1]["start"] if i+1 < len(cues) else fallback_end
        cue.update({k: v for k, v in image_defaults(cue["asset"], cue["start"], cue["end"]).items()
                    if k not in cue and k != "id"})
        cue["legacy_native"] = True
    project["version"] = 2
    project["_migrated"] = True


def probe_audio(path, ffprobe, cancel=None):
    command = [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
               "format=duration:stream=codec_name,sample_rate,channels", "-of", "json", str(path)]
    try:
        if cancel and cancel.is_set(): raise EditorError("음악 분석을 취소했습니다.")
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as p:
            stdout, _ = runtime.communicate_probe(p, cancel)
    except OSError as e:
        raise EditorError("FFprobe를 실행할 수 없습니다. 배포 폴더의 실행 파일을 확인하세요.") from e
    if p.returncode:
        raise EditorError("음악을 읽을 수 없습니다. 손상 여부와 FLAC/WAV/MP3 형식을 확인하세요.")
    try:
        info = json.loads(stdout)
        duration = float(info["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0: raise ValueError("invalid duration")
        return duration
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise EditorError("음악 길이를 확인할 수 없습니다.") from e


def analyze_audio(path, ffmpeg, cancel, report=None):
    """Decode once to bounded-disk PCM; keep 10 ms min/max waveform bins."""
    try:
        fd, name = tempfile.mkstemp(suffix=".wav", prefix="music_preview_")
    except OSError as e:
        raise EditorError("음악 분석용 임시 파일을 만들지 못했습니다. 디스크 여유 공간과 권한을 확인하세요.") from e
    os.close(fd)
    Path(name).unlink(missing_ok=True)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
           "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", "-y", name]
    try:
        _run_encoder(cmd, cancel)
        bins = []
        with wave.open(name, "rb") as w:
            count, rate = w.getnframes(), w.getframerate()
            chunk_frames = rate // 100
            while True:
                if cancel.is_set():
                    raise EditorError("음악 분석을 취소했습니다.")
                raw = w.readframes(chunk_frames)
                if not raw: break
                from array import array
                samples = array("h"); samples.frombytes(raw)
                peak = max((abs(x) for x in samples), default=0) / 32768
                bins.append(peak)
                if report and len(bins) % 100 == 0:
                    report(len(bins) / 100, count / rate)
        return name, bins, count / rate
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise


def waveform_stride(samples):
    return max(1, math.ceil(samples / ((RATE//100) * MAX_WAVEFORM_BINS))) * (RATE//100)


def collect_waveform(path, samples, cancel, progress=None):
    """Keep bounded float32 peaks; PCM reads never exceed 128 KiB."""
    bins = array("f")
    stride = waveform_stride(samples)
    with Path(path).open("rb") as stream:
        for start in range(0, samples, stride):
            remaining, peak = min(stride, samples-start), 0.0
            while remaining:
                if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
                count = min(remaining, 16384)
                raw = stream.read(count*8)
                if len(raw) != count*8: raise EditorError("오디오 캐시가 짧습니다. 다시 분석하세요.")
                values = array("f"); values.frombytes(raw)
                peak = max(peak, max((abs(v) for v in values), default=0.0))
                remaining -= count
            bins.append(peak)
            if progress: progress(min(samples, start+stride)/samples)
    return bins


def analyze_audio_asset(path, ffmpeg, cancel, cache_dir=None, duration=None):
    """Decode PCM to disk and retain at most 240 KiB of waveform peaks."""
    if cancel.is_set(): raise EditorError("음악 분석을 취소했습니다.")
    source = Path(path)
    stat = source.stat()
    key = hashlib.sha256(f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()
    if cache_dir:
        folder = Path(cache_dir)
    else:
        cache_root = Path(tempfile.gettempdir())
        new_folder = cache_root / "Seonyuldam_PCM"
        legacy_folder = cache_root / "MusicToVideo_PCM"
        folder = new_folder if new_folder.exists() or not legacy_folder.exists() else legacy_folder
    folder.mkdir(parents=True, exist_ok=True)
    if cache_dir is None: pin_cache(folder, key)
    with cache_write_lock(folder, key, cancel):
        pcm = folder / (key + ".f32le")
        bins_path = folder / (key + ".peaks")
        if pcm.is_file() and pcm.stat().st_size > 0 and pcm.stat().st_size % 8 == 0:
            count = pcm.stat().st_size // 8
            try:
                if bins_path.stat().st_size != math.ceil(count/waveform_stride(count))*4:
                    raise ValueError("waveform cache size")
                bins = array("f"); bins.frombytes(bins_path.read_bytes())
                return str(pcm), bins, count
            except (OSError, ValueError):
                bins = collect_waveform(pcm, count, cancel)
                part = bins_path.with_name(bins_path.name + "." + uid() + ".part")
                try:
                    part.write_bytes(bins.tobytes()); os.replace(part, bins_path)
                finally: part.unlink(missing_ok=True)
                return str(pcm), bins, count
        temp = folder / (key + "." + uid() + ".part")
        if duration is None:
            companion = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
            ffprobe = str(companion) if companion.is_file() else shutil.which("ffprobe")
            if not ffprobe: raise EditorError("음원 공간 확인에 필요한 FFprobe를 찾지 못했습니다.")
            duration = probe_audio(source, ffprobe, cancel)
        if not math.isfinite(duration) or duration <= 0: raise EditorError("음원 길이가 잘못되었습니다.")
        runtime.require_space(folder, math.ceil(duration*RATE)*8)
        command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
                   "-vn", "-ac", "2", "-ar", str(RATE), "-f", "f32le", "-c:a", "pcm_f32le", "-y", str(temp)]
        try:
            _run_encoder(command, cancel)
            if not temp.is_file() or temp.stat().st_size == 0 or temp.stat().st_size % 8:
                raise EditorError("음악 분석 실패: 오디오 길이가 올바르지 않습니다.")
            bins = collect_waveform(temp, temp.stat().st_size//8, cancel)
            os.replace(temp, pcm)
            temp.write_bytes(bins.tobytes()); os.replace(temp, bins_path)
            return str(pcm), bins, pcm.stat().st_size // 8
        finally:
            temp.unlink(missing_ok=True)


def total_frames(duration):
    return max(1, math.ceil(duration * FPS - 1e-9))


def active_image(project, frame):
    return next((c for c in reversed(project["images"])
                 if c["start"] <= frame < c["end"]), None)


def active_images(project, frame):
    """Image list order is the layer order, from back to front."""
    return [c for c in project["images"] if c["start"] <= frame < c["end"]]


def active_visuals(project, frame):
    return [c for c in visual_clips(project) if c["start"] <= frame < c["end"]]


def transform_at(cue, frame):
    first = cue.get("from", {"x": 960, "y": 540, "zoom": 100})
    if not cue.get("motion") or cue["end"] - cue["start"] <= 1:
        return first
    last = cue.get("to", first)
    fraction = max(0, min(1, (frame - cue["start"]) / (cue["end"] - cue["start"] - 1)))
    if cue.get("easing", "smooth") == "smooth":
        a, b = cue.get("easing_range", (0, 1))
        smooth = lambda value: value * value * (3 - 2 * value)
        denominator = smooth(b) - smooth(a)
        fraction = (smooth(a+(b-a)*fraction)-smooth(a))/denominator if denominator else 0
    return {key: first[key] + (last[key] - first[key]) * fraction for key in ("x", "y", "zoom")}


class LayerCache:
    """Small LRU of output-size layers, never a collection of original images."""
    def __init__(self, max_bytes=32*1024*1024):
        self.max_bytes, self.bytes = max_bytes, 0
        self.items = OrderedDict()

    def get(self, key):
        image = self.items.get(key)
        if image is not None: self.items.move_to_end(key)
        return image

    def put(self, key, image):
        size = image.width*image.height*len(image.getbands())
        if size > self.max_bytes: return
        if key in self.items:
            old = self.items.pop(key); self.bytes -= old.width*old.height*len(old.getbands())
        while self.items and self.bytes+size > self.max_bytes:
            old = self.items.popitem(last=False)[1]
            self.bytes -= old.width*old.height*len(old.getbands())
        self.items[key] = image; self.bytes += size


def color_managed_image(source, mode="RGBA"):
    """Convert embedded ICC colors to sRGB and retain the original alpha channel."""
    profile = source.info.get("icc_profile")
    if not profile: return source.convert(mode)
    try:
        working = source if source.mode in ("RGB", "CMYK", "LAB", "L") else source.convert("RGB")
        image = ImageCms.profileToProfile(working, ImageCms.ImageCmsProfile(BytesIO(profile)),
                                         ImageCms.createProfile("sRGB"), outputMode="RGB",
                                         renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
        if mode == "RGBA":
            image = image.convert("RGBA")
            if "A" in source.getbands() or "transparency" in source.info:
                image.putalpha(source.convert("RGBA").getchannel("A"))
        return image.convert(mode)
    except (OSError, ValueError, ImageCms.PyCMSError) as e:
        raise EditorError("이미지 색상 프로필을 읽지 못했습니다. sRGB 이미지로 다시 저장하세요.") from e


def thumbnail_image(path, size, mode="RGBA"):
    with Image.open(path) as source:
        rotated = source.getexif().get(274, 1) in (5,6,7,8)
        source.draft(source.mode, size[::-1] if rotated else size)
        ImageOps.exif_transpose(source, in_place=True)
        source.thumbnail(size, Image.Resampling.LANCZOS)
        return color_managed_image(source, mode)


def image_layer(project, cue, frame, video_frames=None, cache=None):
    size = project_size(project)
    if "source_in_frame" in cue:
        if video_frames is None: raise EditorError("영상 디코더가 준비되지 않았습니다.")
        image = video_frames.get(cue, frame)
        return picture_layer(image, cue, frame, size)
    asset = next((a for a in project["assets"] if a["id"] == cue["asset"]), None)
    if not asset: raise EditorError("이미지 클립의 원본 참조를 찾지 못했습니다.")
    try:
        key = None
        source_key = None
        image = None
        if cache is not None and cue.get("motion"):
            stat = Path(asset["path"]).stat()
            source_key = ("source", asset["path"], stat.st_mtime_ns, stat.st_size)
            image = cache.get(source_key)
        if cache is not None and not cue.get("motion"):
            stat = Path(asset["path"]).stat()
            state = transform_at(cue, frame)
            key = ("image", asset["path"], stat.st_mtime_ns, stat.st_size, size,
                   state["x"], state["y"], state["zoom"], cue.get("legacy_native", False),
                   cue.get("rotation", 0), cue.get("flip_h", False), cue.get("flip_v", False))
            cached = cache.get(key)
            if cached is not None: return cached
        if image is None:
            with Image.open(asset["path"]) as src:
                ImageOps.exif_transpose(src, in_place=True)
                image = color_managed_image(src)
            if source_key is not None: cache.put(source_key, image)
    except EditorError as e:
        raise EditorError(f"이미지 색상 처리 실패: {asset['path']}\n{e}") from e
    except (OSError, ValueError) as e:
        raise EditorError(f"이미지를 읽을 수 없습니다: {asset['path']}\n{e}") from e
    layer = picture_layer(image, cue, frame, size)
    if key is not None: cache.put(key, layer)
    return layer


def picture_layer(image, cue, frame, size=None):
    size = size or SIZE
    rotation = cue.get("rotation", 0) % 360
    if rotation in (90, 180, 270):
        image = image.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                                 270: Image.Transpose.ROTATE_90}[rotation])
    if cue.get("flip_h"): image = ImageOps.mirror(image)
    if cue.get("flip_v"): image = ImageOps.flip(image)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    fitted = min(size[0] / image.width, size[1] / image.height)
    if cue.get("legacy_native"): fitted = min(1, fitted)
    state = transform_at(cue, frame)
    factor = fitted * max(10, min(500, float(state["zoom"]))) / 100
    width, height = max(1, round(image.width * factor)), max(1, round(image.height * factor))
    x, y = round(state["x"] - width / 2), round(state["y"] - height / 2)
    left, top = max(0, x), max(0, y)
    right, bottom = min(size[0], x + width), min(size[1], y + height)
    if right <= left or bottom <= top:
        canvas.info["composite_bounds"] = None
        return canvas
    # Resize only the visible source window, including Lanczos filter support.
    box = ((left-x)*image.width/width, (top-y)*image.height/height,
           (right-x)*image.width/width, (bottom-y)*image.height/height)
    image = image.resize((right-left, bottom-top), Image.Resampling.LANCZOS, box=box)
    canvas.paste(image, (left, top))
    canvas.info["composite_bounds"] = (left, top, right, bottom)
    canvas.info["composite_opaque"] = image.mode == "RGB" or image.getextrema()[3] == (255, 255)
    return canvas


def image_bounds(project, cue, frame):
    size = project_size(project)
    if "source_in_frame" in cue:
        asset = next((a for a in project.get("video_assets", []) if a["id"] == cue["asset"]), None)
        if not asset: return None
        width, height = asset["width"], asset["height"]
        if cue.get("rotation", 0) % 180 == 90: width, height = height, width
        state = transform_at(cue, frame)
        factor = min(size[0] / width, size[1] / height) * max(10, min(500, state["zoom"])) / 100
        return (state["x"]-width*factor/2, state["y"]-height*factor/2,
                state["x"]+width*factor/2, state["y"]+height*factor/2)
    asset = next((a for a in project["assets"] if a["id"] == cue["asset"]), None)
    if not asset: return None
    with Image.open(asset["path"]) as source:
        oriented = ImageOps.exif_transpose(source)
        width, height = oriented.size
    if cue.get("rotation", 0) % 180 == 90: width, height = height, width
    fitted = min(size[0] / width, size[1] / height)
    if cue.get("legacy_native"): fitted = min(1, fitted)
    state = transform_at(cue, frame)
    factor = fitted * max(10, min(500, float(state["zoom"]))) / 100
    w,h=max(1,round(width*factor)),max(1,round(height*factor))
    return (state["x"]-w/2,state["y"]-h/2,state["x"]+w/2,state["y"]+h/2)


def transition_info(project, cue):
    if "source_in_frame" in cue: return "cut", 0, None
    value = cue.get("transition", {})
    effect = value.get("type", "cut")
    previous = value.get("source") or next((other for other in project["images"]
                     if other["id"] != cue["id"] and other["end"] == cue["start"]), None)
    length = min(int(value.get("frames", 15))-value.get("offset_frames", 0), cue["end"] - cue["start"])
    if not previous or length <= (0 if "source" in value else 1): effect = "cut"
    return effect, max(0, length), previous


def dynamic_frame(project, frame):
    if any(c["start"] <= frame < c["end"] for c in project.get("videos", [])): return True
    for cue in active_images(project, frame):
        if cue.get("motion"): return True
        effect, length, _ = transition_info(project, cue)
        if effect != "cut" and frame < cue["start"] + length: return True
    return False


def font_file(name):
    path = Path(name)
    if path.is_file():
        return str(path), False
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidate = fonts / name
    if candidate.is_file():
        return str(candidate), False
    fallback = fonts / "malgun.ttf"
    if fallback.is_file():
        return str(fallback), True
    return "DejaVuSans.ttf", True


def wrap_lines(draw, text, font, max_width):
    result = []
    for paragraph in text.split("\n"):
        line = ""
        for char in paragraph:
            test = line + char
            if line and draw.textbbox((0, 0), test, font=font)[2] > max_width:
                result.append(line)
                line = char
            else:
                line = test
        result.append(line)
    return result


def text_layout(item, size=None):
    """Return the shared font, wrapped lines, and output-space bounds."""
    path, _ = font_file(item.get("font", "malgun.ttf"))
    font = ImageFont.truetype(path, int(item["size"]))
    width = max(80, min((size or SIZE)[0] - 20, int(item["width"])))
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = wrap_lines(draw, item["text"], font, width - 28)
    ascent, descent = font.getmetrics()
    spacing = max(4, int(item["size"] * .2))
    height = max(1, len(lines)) * (ascent + descent + spacing) + 18
    left = int(item["x"] - width / 2)
    top = int(item["y"])
    return font, lines, spacing, (left, top, left + width, top + height)


def text_box(item, size=None):
    return text_layout(item, size)[3]


def composite_layer(canvas, layer):
    """Composite only touched pixels; cached layers retain their alpha bounds."""
    if "composite_bounds" not in layer.info:
        layer.info["composite_bounds"] = layer.getbbox(alpha_only=True)
    bounds = layer.info["composite_bounds"]
    if bounds is None: return canvas
    if layer.info.get("composite_opaque"):
        if bounds == (0, 0, *canvas.size): canvas.paste(layer)
        else: canvas.paste(layer.crop(bounds), bounds[:2])
    elif bounds == (0, 0, *canvas.size):
        canvas.alpha_composite(layer)
    else:
        canvas.alpha_composite(layer.crop(bounds), dest=bounds[:2])
    return canvas


def render_scene(project, frame, warn=None, video_frames=None, cache=None, visuals=None):
    """The one compositor used for the editor preview and exported stills."""
    size = project_size(project)
    canvas = Image.new("RGBA", size, (0, 0, 0, 255))
    visible_clips = active_visuals(project, frame) if visuals is None else visuals
    if video_frames is not None: video_frames.begin_frame(visible_clips)
    for cue in visible_clips:
        incoming = image_layer(project, cue, frame, video_frames, cache)
        effect, length, previous = transition_info(project, cue)
        if effect != "cut" and frame < cue["start"] + length:
            outgoing = image_layer(project, previous, previous["end"] - 1, cache=cache)
            progress = (frame - cue["start"] + 1) / length
            if "source" in cue.get("transition", {}):
                progress = (frame-cue["start"]+cue["transition"].get("offset_frames", 0)+1)/cue["transition"]["frames"]
            if effect == "dissolve":
                picture = Image.blend(outgoing, incoming, progress)
            elif effect == "fade_black":
                picture = outgoing.copy() if progress < .5 else incoming.copy()
                opacity = 1 - progress * 2 if progress < .5 else (progress - .5) * 2
                picture.putalpha(round(255 * max(0, min(1, opacity))))
            elif effect == "slide":
                picture = Image.new("RGBA", size)
                offset = round(size[0] * progress)
                picture.paste(outgoing, (-offset, 0))
                picture.paste(incoming, (size[0] - offset, 0))
            else: picture = incoming
            picture.info.pop("composite_bounds", None)
            picture.info.pop("composite_opaque", None)
            composite_layer(canvas, picture)
        else:
            composite_layer(canvas, incoming)
    visible = sorted((t for t in project["texts"] if t["start"] <= frame < t["end"]),
                     key=lambda t: t.get("order", 0))
    for item in visible:
        key = ("text", size, json.dumps(item, sort_keys=True))
        cached = cache.get(key) if cache is not None else None
        if cached is not None:
            composite_layer(canvas, cached)
            continue
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        _, fallback = font_file(item.get("font", "malgun.ttf"))
        if fallback and warn: warn(item.get("font", "malgun.ttf"))
        try:
            font, lines, spacing, (left, top, right, bottom) = text_layout(item, size)
        except OSError as e:
            raise EditorError("글꼴을 읽지 못했습니다. 다른 글꼴을 선택하세요.") from e
        width = right - left
        ascent, descent = font.getmetrics()
        height = bottom - top
        bg = item.get("background", "#000000")
        alpha = max(0, min(255, int(item.get("background_alpha", 160))))
        if alpha:
            rgb = tuple(int(bg.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
            draw.rounded_rectangle((left, top, left + width, top + height),
                                   radius=12, fill=rgb + (alpha,))
        align = item.get("align", "center")
        for index, line in enumerate(lines):
            text_width = draw.textbbox((0, 0), line or " ", font=font)[2]
            x = left + 14 if align == "left" else left + width - 14 - text_width if align == "right" else left + (width - text_width) / 2
            y = top + 9 + index * (ascent + descent + spacing)
            draw.text((x, y), line, font=font, fill=item.get("color", "#ffffff"),
                      stroke_width=max(0, int(item.get("outline", 2))), stroke_fill="#000000")
        if cache is not None: cache.put(key, layer)
        composite_layer(canvas, layer)
    return canvas.convert("RGB")


def scene_boundaries(project, frames):
    values = {0, frames}
    for cue in project.get("videos", []):
        values.add(max(0, min(frames, cue["start"])))
        values.add(max(0, min(frames, cue["end"])))
    for cue in project["images"]:
        values.add(max(0, min(frames, int(cue["start"]))))
        values.add(max(0, min(frames, int(cue["end"]))))
        effect, length, _ = transition_info(project, cue)
        if effect != "cut": values.add(max(0, min(frames, cue["start"] + length)))
    for item in project["texts"]:
        values.add(max(0, min(frames, int(item["start"]))))
        values.add(max(0, min(frames, int(item["end"]))))
    return sorted(values)


def concat_path(path):
    return str(Path(path).resolve()).replace("\\", "/").replace("'", "'\\''")


def export_visuals(project, frame):
    size = project_size(project)
    visible = active_visuals(project, frame)
    # A stationary video is opaque; fully covered lower layers need no decoding.
    for index in range(len(visible)-1, -1, -1):
        cue = visible[index]
        if "source_in_frame" not in cue or cue.get("motion"): continue
        bounds = image_bounds(project, cue, frame)
        if bounds and bounds[0] <= 0 and bounds[1] <= 0 and bounds[2] >= size[0] and bounds[3] >= size[1]:
            return visible[index:]
    return visible


def export_spans(project, frames):
    """Yield (start, end, repetitions) without caching decoded frames in RAM."""
    boundaries = scene_boundaries(project, frames)
    for begin, end in zip(boundaries, boundaries[1:]):
        clips = export_visuals(project, begin)
        videos = [c for c in clips if "source_in_frame" in c]
        period = 1
        reusable = bool(videos)
        for cue in clips:
            effect, length, _ = transition_info(project, cue)
            if cue.get("motion") or (effect != "cut" and begin < cue["start"] + length):
                reusable = False
                break
        if reusable:
            for cue in videos:
                period = math.lcm(period, cue["source_out_frame"] - cue["source_in_frame"])
                if period > (end-begin)//2:
                    reusable = False
                    break
        if not reusable:
            yield begin, end, 1
            continue
        repeats, remaining = divmod(end-begin, period)
        yield begin, begin+period, repeats
        if remaining: yield end-remaining, end, 1


class AudioMixer:
    """Sweep the timeline and open only a bounded set of PCM source files."""
    def __init__(self, clips, paths, max_open=8, clip_output=True):
        self.order = {id(clip): index for index, clip in enumerate(clips)}
        self.clips = sorted(clips, key=audio_start)
        self.paths, self.max_open = paths, max_open
        self.streams = OrderedDict()
        self.active, self.index, self.cursor = [], 0, -1
        self.clip_output = clip_output

    def __getitem__(self, ident):
        if ident not in self.streams:
            if len(self.streams) >= self.max_open:
                self.streams.popitem(last=False)[1].close()
            self.streams[ident] = Path(self.paths[ident]).open("rb")
        self.streams.move_to_end(ident)
        return self.streams[ident]

    def read(self, cursor, count):
        if cursor < self.cursor: self.active, self.index = [], 0
        self.cursor = cursor
        self.active = [c for c in self.active if audio_end(c) > cursor]
        while self.index < len(self.clips) and audio_start(self.clips[self.index]) < cursor+count:
            clip = self.clips[self.index]; self.index += 1
            if audio_end(clip) > cursor: self.active.append(clip)
        self.active.sort(key=lambda clip: self.order[id(clip)])
        return mixed_audio_chunk(self.active, self, cursor, count, clip_output=self.clip_output)

    def __enter__(self): return self

    def __exit__(self, *error):
        for stream in self.streams.values(): stream.close()
        self.streams.clear()


def assemble_audio(project, pcm_paths, destination, cancel, clip_output=True):
    """Write a sample-accurate mix of every active clip in bounded chunks."""
    clips = timeline_audio(project)
    if any(not valid_audio(project, clip) for clip in clips):
        raise EditorError("음원 클립의 시간 범위가 올바르지 않습니다.")
    assets = {a["id"]: a for a in project["audio_assets"]}
    assets.update({"video:"+a["id"]: dict(a, samples=a["frames"]*(RATE//FPS))
                   for a in project.get("video_assets", [])})
    with AudioMixer(clips, pcm_paths, clip_output=clip_output) as mixer:
        for clip in clips:
            asset = assets.get(clip["asset"])
            if not asset: raise EditorError("음원 파일 참조를 찾지 못했습니다.")
            source = pcm_paths.get(clip["asset"])
            if not source or not Path(source).is_file():
                raise EditorError("음원 분석 캐시가 없습니다. 음원을 다시 분석하세요.")
            if clip["source_out"] > asset["samples"]:
                raise EditorError("음원 사용 구간이 원본 길이를 넘습니다. 다시 분석하세요.")
        final = duration_samples(project)
        with Path(destination).open("wb") as out:
            for cursor in range(0, final, 16384):
                if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
                out.write(mixer.read(cursor, min(16384, final-cursor)))
    return final


def mixed_audio_chunk(clips, streams, cursor, count, clip_output=True):
    """Mix interleaved stereo float32 PCM; clamp only the final sum."""
    active = sorted(((max(cursor, audio_start(clip)), min(cursor + count, audio_end(clip)), clip)
                     for clip in clips if audio_start(clip) < cursor + count and audio_end(clip) > cursor),
                    key=lambda span: span[0])
    latest = cursor
    overlapping = False
    for begin, end, _ in active:
        if begin < latest:
            overlapping = True
            break
        latest = max(latest, end)
    output = array("f", [0.0]) * (count * 2) if overlapping else bytearray(count * 8)
    for begin, end, clip in active:
        if clip.get("muted") or clip.get("gain", 1) == 0: continue
        source = streams[clip["asset"]]
        if "duration_samples" in clip:
            cycle = clip["source_out"] - clip["source_in"]
            offset = (clip.get("loop_offset_samples", 0) + begin - audio_start(clip)) % cycle
            raw = bytearray()
            remaining = end - begin
            while remaining:
                chunk = min(remaining, cycle - offset)
                source.seek((clip["source_in"] + offset) * 8)
                part = source.read(chunk * 8)
                if len(part) != chunk * 8:
                    raise EditorError("오디오 캐시가 짧습니다. 다시 분석하세요.")
                raw.extend(part)
                remaining -= chunk
                offset = 0
        else:
            source.seek((clip["source_in"] + begin - audio_start(clip)) * 8)
            raw = source.read((end - begin) * 8)
        if len(raw) != (end - begin) * 8:
            raise EditorError("오디오 캐시가 짧습니다. 다시 분석하세요.")
        length = clip.get("envelope_length_samples", audio_end(clip) - audio_start(clip))
        envelope_offset = clip.get("envelope_offset_samples", 0)
        fade_in = min(max(0, int(clip.get("fade_in_samples", 0))), length)
        fade_out = min(max(0, int(clip.get("fade_out_samples", 0))), length)
        clip_gain = 0.0 if clip.get("muted") else float(clip.get("gain", 1.0))
        fading = ((fade_in and begin-audio_start(clip)+envelope_offset < fade_in)
                  or (fade_out and end-audio_start(clip)+envelope_offset > length-fade_out))
        if fading or clip_gain != 1:
            samples = array("f")
            samples.frombytes(raw)
            for frame in range(end - begin):
                position = begin - audio_start(clip) + frame + envelope_offset
                gain = 1.0
                if fade_in:
                    gain = min(gain, position / max(1, fade_in - 1))
                if fade_out:
                    gain = min(gain, (length - 1 - position) / max(1, fade_out - 1))
                samples[frame * 2] *= gain * clip_gain
                samples[frame * 2 + 1] *= gain * clip_gain
            raw = samples.tobytes()
        if not overlapping:
            if clip_output and clip_gain > 1:
                samples = array("f"); samples.frombytes(raw)
                raw = array("f", (max(-1.0, min(1.0, v)) for v in samples)).tobytes()
            output[(begin - cursor) * 8:(end - cursor) * 8] = raw
            continue
        samples = array("f")
        samples.frombytes(raw)
        offset = (begin - cursor) * 2
        for index, value in enumerate(samples):
            output[offset + index] += value
    if overlapping:
        if clip_output:
            for index, value in enumerate(output):
                output[index] = max(-1.0, min(1.0, value))
        return output.tobytes()
    return bytes(output)


def _run_encoder(command, cancel, progress=None, input_frames=None, output_time=None, stderr_result=None):
    """Read both FFmpeg pipes so a full pipe cannot stall cancellation."""
    if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
    # Bound decoder/filter and output-encoder buffers, including video import.
    command = [command[0], "-threads", "2", "-filter_threads", "2",
               *command[1:-1], "-threads", "2", command[-1]]
    command = [command[0], "-progress", "pipe:1", "-nostats", *command[1:]]
    p = subprocess.Popen(command, stdin=subprocess.PIPE if input_frames is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    errors = []
    guard = runtime.ProcessGuard(p, cancel, runtime.ENCODER_STALL_TIMEOUT)
    started = time.monotonic()
    last_time = -1
    def drain(pipe, collect):
        nonlocal last_time
        while True:
            raw = pipe.readline()
            if not raw: break
            if collect:
                errors.append(raw.decode("utf-8", "replace"))
                if len(errors) > 60: del errors[:20]
            elif raw.startswith(b"out_time_us="):
                try: seconds = int(raw.partition(b"=")[2]) / 1_000_000
                except ValueError: continue
                if seconds > last_time:
                    last_time = seconds; guard.touch()
                if output_time is not None: output_time(max(0, seconds))
    readers = [threading.Thread(target=drain, args=(p.stdout, False), daemon=True),
               threading.Thread(target=drain, args=(p.stderr, True), daemon=True)]
    for t in readers: t.start()
    try:
        if input_frames is not None:
            for number, image in enumerate(input_frames):
                if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
                p.stdin.write(image.tobytes())
                guard.touch()
                if progress and number % 3 == 0: progress(number)
            p.stdin.close()
        while p.poll() is None:
            if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
            cancel.wait(.1)
        for t in readers: t.join(2)
        guard.check()
        if p.returncode:
            raise EncoderError("MP4 변환 실패: FFmpeg와 입력 파일, 디스크 공간을 확인하세요.\n" + "".join(errors)[-1000:])
        if stderr_result: stderr_result("".join(errors))
    except (BrokenPipeError, OSError) as e:
        guard.check()
        if cancel.is_set(): raise EditorError("작업을 취소했습니다.") from e
        raise EncoderError("MP4 변환 실패: 인코더 입력이 중단되었습니다.\n" + str(e) + "\n" + "".join(errors)[-700:]) from e
    finally:
        guard.close()
        runtime.logger.info("encoder ended code=%s seconds=%.2f stalled=%s", p.returncode, time.monotonic()-started, guard.expired.is_set())
        for reader in readers: reader.join(2)
        for pipe in (p.stdin, p.stdout, p.stderr):
            if pipe and not pipe.closed: pipe.close()


def pcm_peak_db(raw):
    values = array("f"); values.frombytes(raw)
    peak = max((abs(v) for v in values), default=0)
    return 20*math.log10(peak) if peak else -math.inf


def measure_loudness(path, ffmpeg, cancel):
    logs = []
    _run_encoder([ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-f", "f32le",
                  "-ar", str(RATE), "-ac", "2", "-i", str(path),
                  "-af", "loudnorm=I=-16:TP=-2:LRA=11:print_format=json", "-f", "null", os.devnull],
                 cancel, stderr_result=logs.append)
    try:
        text = logs[0]
        stats = json.loads(text[text.rfind("{"):text.rfind("}")+1])
        return {key: float(stats[key]) for key in
                ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (ValueError, KeyError, IndexError) as e:
        raise EditorError("음량 측정 결과를 읽지 못했습니다.") from e


def master_audio(source, destination, samples, ffmpeg, cancel, mode, on_audio=None):
    """Keep the float mix unclipped until a local lookahead limiter or loudnorm."""
    limiter = "aresample=192000,alimiter=limit=0.794328:level=false:latency=true,aresample=48000"
    if on_audio: on_audio("음량 정규화 분석 중…" if mode == "normalize" else "소리 피크 보호 처리 중…")
    try:
        filters = limiter
        short = False
        if mode == "normalize":
            stats = measure_loudness(source, ffmpeg, cancel)
            short = not math.isfinite(stats["input_i"])
            if not short:
                filters = "loudnorm=I=-16:TP=-2:LRA=11"
                if all(math.isfinite(v) for v in stats.values()) and -99 <= stats["input_i"] <= 0:
                    filters += (f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
                                f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
                                f":offset={stats['target_offset']}:linear=true")
                else: filters += ":linear=false"
        filters += f",aresample={RATE},apad,atrim=end_sample={samples},asetpts=PTS-STARTPTS"
        _run_encoder([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-f", "f32le",
                      "-ar", str(RATE), "-ac", "2", "-i", str(source), "-af", filters,
                      "-ar", str(RATE), "-ac", "2", "-c:a", "pcm_f32le", "-f", "f32le", str(destination)], cancel)
        if Path(destination).stat().st_size != samples*8:
            raise EditorError("음질 처리 후 오디오 길이가 일치하지 않습니다.")
        if on_audio:
            stats = measure_loudness(destination, ffmpeg, cancel)
            level = f"{stats['input_i']:.1f} LUFS" if math.isfinite(stats["input_i"]) else "무음·짧은 구간"
            peak = f"{stats['input_tp']:.1f} dBTP" if math.isfinite(stats["input_tp"]) else "-∞ dBTP"
            prefix = "정규화 측정 불가 · 피크 보호" if short else "음질 처리 완료"
            on_audio(f"{prefix}: {level} · {peak} (AAC 인코딩 전)")
    except EncoderError as e:
        raise EditorError("소리 처리 실패: FFmpeg 오디오 필터를 확인하세요.\n" + str(e)) from e


@contextmanager
def export_scene_layers(project, begin, end, ffmpeg, cancel, scratch, video_frames, cache):
    """Flatten dense video stacks losslessly, using at most two source decoders."""
    size = project_size(project)
    pending = deque(export_visuals(project, begin))
    background = None
    temporary = []
    ident = "render-" + uid()
    try:
        while sum("source_in_frame" in c for c in pending) + bool(background) > 2:
            group = [background] if background else []
            count = int(bool(background))
            while pending:
                video = "source_in_frame" in pending[0]
                if video and count == 2: break
                group.append(pending.popleft()); count += video
            path = Path(scratch) / (ident + "-" + uid() + ".mkv")
            temporary.append(path)
            video_frames.close()
            stage = dict(project, texts=[])
            command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "rawvideo",
                       "-pixel_format", "rgb24", "-video_size", f"{size[0]}x{size[1]}", "-framerate", "30",
                       "-i", "pipe:0", "-c:v", "ffv1", "-level", "3", "-pix_fmt", "bgr0",
                       "-frames:v", str(end-begin), "-an", str(path)]
            _run_encoder(command, cancel,
                         input_frames=(render_scene(stage, f, video_frames=video_frames, cache=cache, visuals=group)
                                       for f in range(begin, end)))
            video_frames.close()
            if len(temporary) > 1: temporary.pop(0).unlink()
            video_frames.assets[ident] = dict(id=ident, path=str(path), width=size[0], height=size[1],
                                               frames=end-begin, video_stream=0)
            background = video_defaults(ident, begin, end-begin, size)
        yield ([background] if background else []) + list(pending)
    finally:
        if temporary:
            video_frames.close()
            video_frames.assets.pop(ident, None)
            for path in temporary: path.unlink(missing_ok=True)


def export_video(project, duration, output, ffmpeg, cancel, report=None, pcm_paths=None,
                 quality=DEFAULT_EXPORT_QUALITY, acceleration="cpu", on_encoder=None,
                 audio_mode="original", on_audio=None):
    if quality not in EXPORT_QUALITIES or acceleration not in ("cpu", "auto"):
        raise EditorError("지원하지 않는 출력 품질 또는 가속 설정입니다.")
    if Path(output).exists(): raise EditorError("같은 이름의 출력 파일이 있습니다. 새 이름을 지정하세요.")
    if audio_mode not in EXPORT_AUDIO_MODES.values(): raise EditorError("지원하지 않는 음질 설정입니다.")
    audio_options = dict(audio_mode=audio_mode, on_audio=on_audio) if audio_mode != "original" or on_audio else {}
    encoder = probe_gpu_encoder(ffmpeg, project_size(project), quality, cancel) if acceleration == "auto" else None
    notify = on_encoder or (lambda message: None)
    if encoder:
        notify(f"GPU 인코딩: {encoder}")
        try:
            return _export_video(project, duration, output, ffmpeg, cancel, report, pcm_paths, quality, encoder, **audio_options)
        except EncoderError:
            if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
            notify("GPU 인코딩 실패 · CPU로 처음부터 다시 출력합니다.")
    else:
        notify("GPU를 사용할 수 없어 CPU로 출력합니다." if acceleration == "auto" else "CPU 인코딩: libx264")
    return _export_video(project, duration, output, ffmpeg, cancel, report, pcm_paths, quality, "libx264", **audio_options)


def _export_video(project, duration, output, ffmpeg, cancel, report=None, pcm_paths=None,
                  quality=DEFAULT_EXPORT_QUALITY, encoder="libx264", audio_mode="original", on_audio=None):
    """Encode static spans once and motion/transition spans as streamed frames."""
    size = project_size(project)
    if quality not in EXPORT_QUALITIES:
        raise EditorError("지원하지 않는 출력 품질입니다.")
    # Convert the RGB compositor output and declare the same matrix in every segment.
    color_tags = ["-colorspace", "bt709", "-color_primaries", "bt709",
                  "-color_trc", "bt709", "-color_range", "tv"]
    encoding = ["-vf", "scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p",
                *video_codec_args(encoder, quality)] + color_tags
    output = Path(output).resolve()
    if output.exists(): raise EditorError("같은 이름의 출력 파일이 있습니다. 새 이름을 지정하세요.")
    if not output.parent.is_dir(): raise EditorError("저장 폴더가 없습니다.")
    if not duration_samples(project): raise EditorError("타임라인에 음악 또는 영상을 배치하세요.")
    if any(not valid_video(project, c) for c in project.get("videos", [])):
        raise EditorError("영상 클립의 원본 범위가 잘못되었습니다.")
    if missing_media(project): raise EditorError("원본 미디어가 없습니다. 프로젝트에서 파일을 다시 연결하세요.")
    pcm_paths = pcm_paths or {}
    frames = total_frames(duration_seconds(project))
    if frames <= 0: raise EditorError("영상 구간이 없습니다.")
    samples = duration_samples(project)
    # PCM sizes are exact; reserve 32 Mbit/s for estimated video intermediates.
    scratch = Path(tempfile.gettempdir())
    scratch_bytes = samples*8*(2 if audio_mode != "original" else 1) + math.ceil(frames/FPS*4_000_000)
    output_bytes = math.ceil(frames/FPS*4_000_000)
    if scratch.stat().st_dev == output.parent.stat().st_dev:
        runtime.require_space(scratch, scratch_bytes + output_bytes)
    else:
        runtime.require_space(scratch, scratch_bytes)
        runtime.require_space(output.parent, output_bytes)
    try:
        fd, temp_name = tempfile.mkstemp(prefix="." + output.stem + "_", suffix=".tmp.mp4", dir=output.parent)
        os.close(fd); Path(temp_name).unlink(missing_ok=True)
    except OSError as e:
        raise EditorError("저장 폴더에 쓰기 실패: 권한과 디스크 공간을 확인하세요.\n" + str(e)) from e
    try:
        from video_media import VideoFrameProvider
        with tempfile.TemporaryDirectory(prefix="music_video_scenes_") as temporary, \
                VideoFrameProvider(project["video_assets"] if "video_assets" in project else [], ffmpeg, cancel) as video_frames:
            scratch = Path(temporary)
            cache = LayerCache()
            audio_path = scratch / "timeline.f32le"
            written = assemble_audio(project, pcm_paths, audio_path, cancel, clip_output=audio_mode == "original")
            if written != duration_samples(project): raise EditorError("음원 타임라인 길이가 일치하지 않습니다.")
            if audio_mode != "original":
                mastered = scratch / "mastered.f32le"
                master_audio(audio_path, mastered, written, ffmpeg, cancel, audio_mode, on_audio)
                audio_path = mastered
            if report: report(5)
            manifest = scratch / "scenes.ffconcat"
            manifest.write_text("ffconcat version 1.0\n", encoding="utf-8")
            for index, (span_begin, span_end, repeats) in enumerate(export_spans(project, frames)):
                if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
                if span_begin == span_end: continue
                span_count = span_end-span_begin
                dense = sum("source_in_frame" in c for c in export_visuals(project, span_begin)) > 2
                step = MAX_COMPOSITE_FRAMES if dense else span_count
                chunks = range(span_begin, span_end, step)
                for chunk, begin in enumerate(chunks):
                    if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
                    end = min(begin+step, span_end)
                    count = end-begin
                    segment = scratch / f"segment_{index:06d}_{chunk:06d}.mp4"
                    dynamic = any(dynamic_frame(project, frame) for frame in (begin, end-1))
                    if dynamic:
                        command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "rawvideo",
                                   "-pixel_format", "rgb24", "-video_size", f"{size[0]}x{size[1]}", "-framerate", "30",
                                   "-i", "pipe:0"] + encoding + ["-frames:v", str(count), "-an", str(segment)]
                        scene = dict(project, texts=[t for t in project["texts"] if t["start"] <= begin < t["end"]])
                        with export_scene_layers(scene, begin, end, ffmpeg, cancel, scratch, video_frames, cache) as visuals:
                            _run_encoder(command, cancel, lambda n: report(5 + int(75*(begin+n)/frames)) if report else None,
                                         (render_scene(scene, f, video_frames=video_frames, cache=cache, visuals=visuals)
                                          for f in range(begin, end)))
                    else:
                        image_path = scratch / f"scene_{index:06d}.png"
                        render_scene(project, begin, video_frames=video_frames, cache=cache).save(image_path)
                        command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loop", "1", "-framerate", "30",
                                   "-i", str(image_path)] + encoding + (["-tune", "stillimage"] if encoder == "libx264" else []) + ["-r", "30",
                                   "-frames:v", str(count), "-an", str(segment)]
                        _run_encoder(command, cancel)
                        image_path.unlink()
                with manifest.open("a", encoding="utf-8") as entries:
                    for _ in range(repeats):
                        for chunk in range(len(chunks)):
                            if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
                            segment = scratch / f"segment_{index:06d}_{chunk:06d}.mp4"
                            entries.write(f"file '{concat_path(segment)}'\n")
                if report: report(5 + int(75 * (span_begin + span_count*repeats) / frames))
            video_frames.close()
            command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                       "-i", str(manifest), "-f", "f32le", "-ar", "48000", "-ac", "2", "-i", str(audio_path),
                       "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-frames:v", str(frames),
                       "-c:a", "aac", "-b:a", "320k"] + color_tags + ["-movflags", "+faststart", temp_name]
            _run_encoder(command, cancel)
            if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
            if not Path(temp_name).is_file() or Path(temp_name).stat().st_size < 1024:
                raise EditorError("MP4 결과 파일이 비어 있습니다. 디스크 공간을 확인하세요.")
            os.rename(temp_name, output)
            if report: report(100)
            return output
    except FileExistsError as e:
        raise EditorError("같은 이름의 출력 파일이 생겼습니다. 새 이름을 지정하세요.") from e
    except OSError as e:
        raise EditorError("저장 실패: 쓰기 권한과 디스크 공간을 확인하세요.\n" + str(e)) from e
    finally:
        Path(temp_name).unlink(missing_ok=True)
