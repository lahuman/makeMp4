"""Project data, audio analysis, shared rendering, and MP4 export."""
from __future__ import annotations

import copy
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import uuid
import wave

from PIL import Image, ImageDraw, ImageFont, ImageOps

FPS = 30
SIZE = (1920, 1080)
RATE = 48000
AUDIO_EXTS = {".flac", ".wav", ".mp3"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}


class EditorError(Exception):
    pass


def uid():
    return uuid.uuid4().hex[:12]


def fresh():
    return {"version": 3, "audio_assets": [], "audio_clips": [], "assets": [], "images": [], "texts": [],
            "video_assets": [], "videos": [],
            "settings": {"width": 1920, "height": 1080, "fps": 30, "audio_bitrate": "320k"}}


def image_defaults(asset, start, end):
    return {"id": uid(), "asset": asset, "start": int(start), "end": int(end),
            "motion": False, "easing": "smooth", "from": {"x": 960, "y": 540, "zoom": 100},
            "to": {"x": 960, "y": 540, "zoom": 100},
            "transition": {"type": "cut", "frames": 15}}


def audio_start(clip):
    return int(clip["start_sample"])


def audio_defaults(asset, start, samples):
    return {"id": uid(), "asset": asset, "start_sample": int(start),
            "source_in": 0, "source_out": int(samples),
            "fade_in_samples": RATE, "fade_out_samples": RATE}


def audio_end(clip):
    return audio_start(clip) + int(clip["source_out"]) - int(clip["source_in"])


def video_defaults(asset, start, frames):
    clip = image_defaults(asset, start, start + frames)
    clip.update(source_in_frame=0, source_out_frame=int(frames), audio_enabled=False,
                audio_gain=1.0, fade_in_samples=0, fade_out_samples=0)
    return clip


def video_end(clip):
    return clip["start"] + clip["source_out_frame"] - clip["source_in_frame"]


def valid_video(project, clip):
    asset = next((a for a in project.get("video_assets", []) if a["id"] == clip["asset"]), None)
    return bool(asset and all(type(clip.get(k)) is int for k in ("start", "end", "source_in_frame", "source_out_frame"))
                and 0 <= clip["source_in_frame"] < clip["source_out_frame"] <= asset.get("frames", 0)
                and clip["start"] >= 0 and clip["end"] == video_end(clip)
                and math.isfinite(clip.get("audio_gain", 1)) and 0 <= clip.get("audio_gain", 1) <= 2)


def visual_clips(project):
    images = project["images"]
    videos = project.get("videos", [])
    return sorted(images + videos, key=lambda c: c.get("order", images.index(c) if c in images else len(images)))


def next_visual_order(project):
    return max((c.get("order", i) for i, c in enumerate(visual_clips(project))), default=-1) + 1


def timeline_audio(project):
    """Derive linked video audio so trimming never creates a second source of truth."""
    clips = list(project["audio_clips"])
    assets = {a["id"]: a for a in project.get("video_assets", [])}
    for video in project.get("videos", []):
        asset = assets.get(video["asset"])
        if video.get("audio_enabled") and asset and asset.get("has_audio"):
            clips.append({"id": video["id"], "asset": "video:" + video["asset"],
                          "start_sample": video["start"] * (RATE // FPS),
                          "source_in": video["source_in_frame"] * (RATE // FPS),
                          "source_out": video["source_out_frame"] * (RATE // FPS),
                          "gain": video.get("audio_gain", 1.0),
                          "fade_in_samples": video.get("fade_in_samples", 0),
                          "fade_out_samples": video.get("fade_out_samples", 0)})
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
            and clip["source_out"] > clip["source_in"])


def paths_in(project, project_file):
    base = Path(project_file).parent
    data = copy.deepcopy(project)
    for asset in data["assets"] + data.get("audio_assets", []) + data.get("video_assets", []):
        asset["path"] = str((base / asset["path"]).resolve())
    if data.get("audio"):
        data["audio"] = str((base / data["audio"]).resolve())
    return data


def save_project(project, path):
    path = Path(path)
    data = copy.deepcopy(project)
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
        if data.get("version") not in (1, 2, 3) or not all(k in data for k in ("assets", "images", "texts")):
            raise ValueError("지원하지 않는 프로젝트 형식")
        data = paths_in(data, path)
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
        for clip in data["videos"]:
            if not valid_video(data, clip): raise ValueError("영상 클립의 원본 범위 또는 시간이 잘못되었습니다.")
        return data
    except (OSError, ValueError, TypeError, KeyError) as e:
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


def probe_audio(path, ffprobe):
    command = [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
               "format=duration:stream=codec_name,sample_rate,channels", "-of", "json", str(path)]
    try:
        p = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        raise EditorError("FFprobe를 실행할 수 없습니다. 배포 폴더의 실행 파일을 확인하세요.") from e
    if p.returncode:
        raise EditorError("음악을 읽을 수 없습니다. 손상 여부와 FLAC/WAV/MP3 형식을 확인하세요.")
    try:
        info = json.loads(p.stdout)
        return float(info["format"]["duration"])
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
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while p.poll() is None:
            if cancel.is_set():
                p.terminate()
                try: p.wait(2)
                except subprocess.TimeoutExpired: p.kill(); p.wait()
                raise EditorError("음악 분석을 취소했습니다.")
            cancel.wait(.1)
        error = p.stderr.read().decode("utf-8", "replace")[-500:]
        if p.returncode:
            raise EditorError("음악 분석에 실패했습니다. 파일 손상을 확인하세요.\n" + error)
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


def analyze_audio_asset(path, ffmpeg, cancel, cache_dir=None):
    """Decode to interleaved float32 PCM on disk and collect 10 ms peak bins."""
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
    pcm = folder / (key + ".f32le")
    bins_path = folder / (key + ".json")
    if pcm.is_file() and bins_path.is_file():
        try:
            bins = json.loads(bins_path.read_text(encoding="utf-8"))
            if pcm.stat().st_size % 8 == 0:
                return str(pcm), bins, pcm.stat().st_size // 8
        except (OSError, ValueError):
            pass
    temp = folder / (key + "." + uid() + ".part")
    command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
               "-vn", "-ac", "2", "-ar", str(RATE), "-f", "f32le", "-c:a", "pcm_f32le", "-y", str(temp)]
    try:
        p = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while p.poll() is None:
            if cancel.is_set():
                p.terminate()
                try: p.wait(2)
                except subprocess.TimeoutExpired: p.kill(); p.wait()
                raise EditorError("음악 분석을 취소했습니다.")
            cancel.wait(.1)
        error = p.stderr.read().decode("utf-8", "replace")[-500:]
        if p.returncode or not temp.is_file() or temp.stat().st_size == 0:
            raise EditorError("음악 분석 실패: 파일 손상 또는 지원하지 않는 오디오를 확인하세요.\n" + error)
        bins = []
        block = RATE // 100
        with temp.open("rb") as f:
            while True:
                if cancel.is_set(): raise EditorError("음악 분석을 취소했습니다.")
                raw = f.read(block * 8)
                if not raw: break
                samples = array("f"); samples.frombytes(raw)
                bins.append(max((abs(v) for v in samples), default=0.0))
        os.replace(temp, pcm)
        bins_path.write_text(json.dumps(bins), encoding="utf-8")
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
        fraction = fraction * fraction * (3 - 2 * fraction)
    return {key: first[key] + (last[key] - first[key]) * fraction for key in ("x", "y", "zoom")}


def image_layer(project, cue, frame, video_frames=None):
    canvas = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    if "source_in_frame" in cue:
        if video_frames is None: raise EditorError("영상 디코더가 준비되지 않았습니다.")
        image = video_frames.get(cue, frame).convert("RGBA")
        return picture_layer(image, cue, frame)
    asset = next((a for a in project["assets"] if a["id"] == cue["asset"]), None)
    if not asset: return canvas
    try:
        with Image.open(asset["path"]) as src:
            image = ImageOps.exif_transpose(src).convert("RGBA")
    except (OSError, ValueError) as e:
        raise EditorError(f"이미지를 읽을 수 없습니다: {asset['path']}\n{e}") from e
    return picture_layer(image, cue, frame)


def picture_layer(image, cue, frame):
    canvas = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    fitted = min(SIZE[0] / image.width, SIZE[1] / image.height)
    if cue.get("legacy_native"): fitted = min(1, fitted)
    state = transform_at(cue, frame)
    factor = fitted * max(10, min(500, float(state["zoom"]))) / 100
    width, height = max(1, round(image.width * factor)), max(1, round(image.height * factor))
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    x, y = round(state["x"] - width / 2), round(state["y"] - height / 2)
    canvas.paste(image, (x, y))
    return canvas


def image_bounds(project, cue, frame):
    if "source_in_frame" in cue:
        asset = next((a for a in project.get("video_assets", []) if a["id"] == cue["asset"]), None)
        if not asset: return None
        width, height = asset["width"], asset["height"]
        state = transform_at(cue, frame)
        factor = min(SIZE[0] / width, SIZE[1] / height) * max(10, min(500, state["zoom"])) / 100
        return (state["x"]-width*factor/2, state["y"]-height*factor/2,
                state["x"]+width*factor/2, state["y"]+height*factor/2)
    asset = next((a for a in project["assets"] if a["id"] == cue["asset"]), None)
    if not asset: return None
    with Image.open(asset["path"]) as source:
        oriented = ImageOps.exif_transpose(source)
        width, height = oriented.size
    fitted = min(SIZE[0] / width, SIZE[1] / height)
    if cue.get("legacy_native"): fitted = min(1, fitted)
    state = transform_at(cue, frame)
    factor = fitted * max(10, min(500, float(state["zoom"]))) / 100
    w,h=max(1,round(width*factor)),max(1,round(height*factor))
    return (state["x"]-w/2,state["y"]-h/2,state["x"]+w/2,state["y"]+h/2)


def transition_info(project, cue):
    if "source_in_frame" in cue: return "cut", 0, None
    value = cue.get("transition", {})
    effect = value.get("type", "cut")
    previous = next((other for other in project["images"]
                     if other["id"] != cue["id"] and other["end"] == cue["start"]), None)
    length = min(int(value.get("frames", 15)), cue["end"] - cue["start"])
    if not previous or length <= 1: effect = "cut"
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


def text_layout(item):
    """Return the shared font, wrapped lines, and output-space bounds."""
    path, _ = font_file(item.get("font", "malgun.ttf"))
    font = ImageFont.truetype(path, int(item["size"]))
    width = max(80, min(1900, int(item["width"])))
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = wrap_lines(draw, item["text"], font, width - 28)
    ascent, descent = font.getmetrics()
    spacing = max(4, int(item["size"] * .2))
    height = max(1, len(lines)) * (ascent + descent + spacing) + 18
    left = int(item["x"] - width / 2)
    top = int(item["y"])
    return font, lines, spacing, (left, top, left + width, top + height)


def text_box(item):
    return text_layout(item)[3]


def render_scene(project, frame, warn=None, video_frames=None):
    """The one compositor used for the editor preview and exported stills."""
    canvas = Image.new("RGBA", SIZE, (0, 0, 0, 255))
    visible_clips = active_visuals(project, frame)
    if video_frames is not None: video_frames.begin_frame(visible_clips)
    for cue in visible_clips:
        incoming = image_layer(project, cue, frame, video_frames)
        effect, length, previous = transition_info(project, cue)
        if effect != "cut" and frame < cue["start"] + length:
            outgoing = image_layer(project, previous, previous["end"] - 1)
            progress = (frame - cue["start"] + 1) / length
            if effect == "dissolve":
                picture = Image.blend(outgoing, incoming, progress)
            elif effect == "fade_black":
                picture = outgoing.copy() if progress < .5 else incoming.copy()
                opacity = 1 - progress * 2 if progress < .5 else (progress - .5) * 2
                picture.putalpha(round(255 * max(0, min(1, opacity))))
            elif effect == "slide":
                picture = Image.new("RGBA", SIZE)
                offset = round(SIZE[0] * progress)
                picture.paste(outgoing, (-offset, 0))
                picture.paste(incoming, (SIZE[0] - offset, 0))
            else: picture = incoming
            canvas = Image.alpha_composite(canvas, picture)
        else:
            canvas = Image.alpha_composite(canvas, incoming)
    visible = sorted((t for t in project["texts"] if t["start"] <= frame < t["end"]),
                     key=lambda t: t.get("order", 0))
    for item in visible:
        layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        _, fallback = font_file(item.get("font", "malgun.ttf"))
        if fallback and warn: warn(item.get("font", "malgun.ttf"))
        try:
            font, lines, spacing, (left, top, right, bottom) = text_layout(item)
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
        canvas = Image.alpha_composite(canvas, layer)
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


def assemble_audio(project, pcm_paths, destination, cancel):
    """Write a sample-accurate mix of every active clip in bounded chunks."""
    from contextlib import ExitStack
    clips = timeline_audio(project)
    if any(not valid_audio(project, clip) for clip in clips):
        raise EditorError("음원 클립의 시간 범위가 올바르지 않습니다.")
    with ExitStack() as stack:
        streams = {}
        for clip in clips:
            asset = next((a for a in project["audio_assets"] if a["id"] == clip["asset"]), None)
            if clip["asset"].startswith("video:"):
                asset = next((dict(a, samples=a["frames"]*(RATE//FPS))
                              for a in project.get("video_assets", []) if "video:"+a["id"] == clip["asset"]), None)
            if not asset: raise EditorError("음원 파일 참조를 찾지 못했습니다.")
            source = pcm_paths.get(clip["asset"])
            if not source or not Path(source).is_file():
                raise EditorError("음원 분석 캐시가 없습니다. 음원을 다시 분석하세요.")
            if clip["source_out"] > asset["samples"]:
                raise EditorError("음원 사용 구간이 원본 길이를 넘습니다. 다시 분석하세요.")
            if clip["asset"] not in streams:
                streams[clip["asset"]] = stack.enter_context(Path(source).open("rb"))
        final = duration_samples(project)
        with Path(destination).open("wb") as out:
            for cursor in range(0, final, 16384):
                if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
                out.write(mixed_audio_chunk(clips, streams, cursor, min(16384, final-cursor)))
    return final


def mixed_audio_chunk(clips, streams, cursor, count):
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
        source = streams[clip["asset"]]
        source.seek((clip["source_in"] + begin - audio_start(clip)) * 8)
        raw = source.read((end - begin) * 8)
        if len(raw) != (end - begin) * 8:
            raise EditorError("오디오 캐시가 짧습니다. 다시 분석하세요.")
        length = audio_end(clip) - audio_start(clip)
        fade_in = min(max(0, int(clip.get("fade_in_samples", 0))), length)
        fade_out = min(max(0, int(clip.get("fade_out_samples", 0))), length)
        clip_gain = float(clip.get("gain", 1.0))
        if fade_in or fade_out or clip_gain != 1:
            samples = array("f")
            samples.frombytes(raw)
            for frame in range(end - begin):
                position = begin - audio_start(clip) + frame
                gain = 1.0
                if fade_in:
                    gain = min(gain, position / max(1, fade_in - 1))
                if fade_out:
                    gain = min(gain, (length - 1 - position) / max(1, fade_out - 1))
                samples[frame * 2] *= gain * clip_gain
                samples[frame * 2 + 1] *= gain * clip_gain
            raw = samples.tobytes()
        if not overlapping:
            if clip_gain > 1:
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
        for index, value in enumerate(output):
            output[index] = max(-1.0, min(1.0, value))
        return output.tobytes()
    return bytes(output)


def _run_encoder(command, cancel, progress=None, input_frames=None):
    """Read both FFmpeg pipes so a full pipe cannot stall cancellation."""
    if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
    p = subprocess.Popen(command, stdin=subprocess.PIPE if input_frames is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    errors = []
    def drain(pipe, collect):
        while True:
            raw = pipe.readline()
            if not raw: break
            if collect:
                errors.append(raw.decode("utf-8", "replace"))
                if len(errors) > 60: del errors[:20]
    readers = [threading.Thread(target=drain, args=(p.stdout, False), daemon=True),
               threading.Thread(target=drain, args=(p.stderr, True), daemon=True)]
    for t in readers: t.start()
    finished = threading.Event()
    def interrupt_on_cancel():
        while not finished.is_set() and p.poll() is None:
            if cancel.wait(.05):
                if not finished.is_set() and p.poll() is None: p.terminate()
                return
    watchdog = threading.Thread(target=interrupt_on_cancel, daemon=True)
    watchdog.start()
    try:
        if input_frames is not None:
            for number, image in enumerate(input_frames):
                if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
                p.stdin.write(image.tobytes())
                if progress and number % 3 == 0: progress(number)
            p.stdin.close()
        while p.poll() is None:
            if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
            cancel.wait(.1)
        for t in readers: t.join(2)
        if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
        if p.returncode:
            raise EditorError("MP4 변환 실패: FFmpeg와 입력 파일, 디스크 공간을 확인하세요.\n" + "".join(errors)[-1000:])
    except (BrokenPipeError, OSError) as e:
        if cancel.is_set(): raise EditorError("작업을 취소했습니다.") from e
        raise EditorError("MP4 변환 실패: 인코더 입력이 중단되었습니다.\n" + str(e) + "\n" + "".join(errors)[-700:]) from e
    finally:
        finished.set()
        if p.poll() is None:
            p.terminate()
            try: p.wait(2)
            except subprocess.TimeoutExpired: p.kill(); p.wait()
        watchdog.join(1)
        for reader in readers: reader.join(2)
        for pipe in (p.stdin, p.stdout, p.stderr):
            if pipe and not pipe.closed: pipe.close()


def export_video(project, duration, output, ffmpeg, cancel, report=None, pcm_paths=None):
    """Encode static spans once and motion/transition spans as streamed frames."""
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
            audio_path = scratch / "timeline.f32le"
            written = assemble_audio(project, pcm_paths, audio_path, cancel)
            if written != duration_samples(project): raise EditorError("음원 타임라인 길이가 일치하지 않습니다.")
            if report: report(5)
            boundaries = scene_boundaries(project, frames)
            manifest_lines = ["ffconcat version 1.0"]
            for index, (begin, end) in enumerate(zip(boundaries, boundaries[1:])):
                if cancel.is_set(): raise EditorError("작업을 취소했습니다.")
                if begin == end: continue
                count = end - begin
                segment = scratch / f"segment_{index:06d}.mp4"
                dynamic = any(dynamic_frame(project, frame) for frame in (begin, end-1))
                if dynamic:
                    command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "rawvideo",
                               "-pixel_format", "rgb24", "-video_size", "1920x1080", "-framerate", "30",
                               "-i", "pipe:0", "-vf", "format=yuv420p", "-c:v", "libx264",
                               "-preset", "medium", "-crf", "23", "-frames:v", str(count), "-an", str(segment)]
                    _run_encoder(command, cancel, lambda n: report(5 + int(75*(begin+n)/frames)) if report else None,
                                 (render_scene(project, f, video_frames=video_frames) for f in range(begin, end)))
                else:
                    image_path = scratch / f"scene_{index:06d}.png"
                    render_scene(project, begin, video_frames=video_frames).save(image_path)
                    command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loop", "1", "-framerate", "30",
                               "-i", str(image_path), "-vf", "format=yuv420p", "-c:v", "libx264",
                               "-preset", "medium", "-tune", "stillimage", "-crf", "23", "-r", "30",
                               "-frames:v", str(count), "-an", str(segment)]
                    _run_encoder(command, cancel)
                    image_path.unlink()
                manifest_lines.append(f"file '{concat_path(segment)}'")
                if report: report(5 + int(75 * end / frames))
            manifest = scratch / "scenes.ffconcat"
            manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
            command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                       "-i", str(manifest), "-f", "f32le", "-ar", "48000", "-ac", "2", "-i", str(audio_path),
                       "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-frames:v", str(frames),
                       "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart", temp_name]
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
