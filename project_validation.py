"""Validation at the JSON boundary, before a project replaces editor state."""
import math
import re


def validate_shape(data):
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in (1, 2, 3):
        raise ValueError("지원하지 않는 프로젝트 형식")
    required = ("assets", "images", "texts")
    if data["version"] != 1:
        required += ("audio_assets", "audio_clips")
    for key in required + ("video_assets", "videos"):
        items = data.get(key, [] if key not in required else None)
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError(f"{key}: 객체 목록이 필요합니다.")
    for key in ("assets", "audio_assets", "video_assets"):
        for item in data.get(key, []):
            if not isinstance(item.get("path"), str) or not item["path"] or "\0" in item["path"]:
                raise ValueError(f"{key}: 미디어 경로가 잘못되었습니다.")
    if data.get("audio") and not isinstance(data["audio"], str):
        raise ValueError("음원 경로가 잘못되었습니다.")


def validate_project(data):
    validate_shape(data)

    def number(item, key, default=None, minimum=None, integer=False):
        value = item.get(key, default)
        if (type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value)
                or (minimum is not None and value < minimum)):
            raise ValueError(f"{key}: 숫자 범위가 잘못되었습니다.")
        return value

    collections = ("assets", "audio_assets", "video_assets", "images", "videos", "texts")
    ids = {}
    for key in collections:
        seen = set()
        for item in data.get(key, []):
            ident = item.get("id")
            if not isinstance(ident, str) or not ident or ident in seen:
                raise ValueError(f"{key}: 비어 있거나 중복된 ID입니다.")
            seen.add(ident)
        ids[key] = seen

    for asset in data.get("audio_assets", []):
        number(asset, "samples", minimum=0, integer=True)
    for asset in data.get("video_assets", []):
        number(asset, "frames", 0, minimum=0, integer=True)
        for key in ("width", "height"):
            if key in asset: number(asset, key, minimum=1, integer=True)

    def visual(clip, assets, nested=False):
        if clip.get("asset") not in assets:
            raise ValueError("화면 클립의 미디어 참조를 찾지 못했습니다.")
        start = number(clip, "start", minimum=0, integer=True)
        if number(clip, "end", minimum=1, integer=True) <= start:
            raise ValueError("화면 클립의 끝은 시작보다 뒤여야 합니다.")
        for key in ("from", "to"):
            if key in clip:
                state = clip[key]
                if not isinstance(state, dict): raise ValueError("화면 위치 형식이 잘못되었습니다.")
                for field in ("x", "y", "zoom"): number(state, field)
        if "motion" in clip and type(clip["motion"]) is not bool:
            raise ValueError("움직임 설정이 잘못되었습니다.")
        number(clip, "rotation", 0, integer=True)
        number(clip, "order", 0)
        if "easing_range" in clip:
            values = clip["easing_range"]
            if (not isinstance(values, (list, tuple)) or len(values) != 2
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                    or not 0 <= values[0] <= values[1] <= 1):
                raise ValueError("움직임 구간이 잘못되었습니다.")
        transition = clip.get("transition", {})
        if not isinstance(transition, dict) or transition.get("type", "cut") not in ("cut", "dissolve", "fade_black", "slide"):
            raise ValueError("화면 전환 형식이 잘못되었습니다.")
        number(transition, "frames", 15, minimum=0, integer=True)
        number(transition, "offset_frames", 0, minimum=0, integer=True)
        if "source" in transition:
            if nested or not isinstance(transition["source"], dict):
                raise ValueError("화면 전환 원본이 잘못되었습니다.")
            visual(transition["source"], ids["assets"], nested=True)

    for clip in data["images"]: visual(clip, ids["assets"])
    for clip in data.get("videos", []): visual(clip, ids["video_assets"])
    audio_ids = set()
    assets = {a["id"]: a for a in data.get("audio_assets", [])}
    for clip in data.get("audio_clips", []):
        ident = clip.get("id")
        if not isinstance(ident, str) or not ident or ident in audio_ids:
            raise ValueError("음원 클립 ID가 잘못되었습니다.")
        audio_ids.add(ident)
        asset = assets.get(clip.get("asset"))
        if asset is None: raise ValueError("음원 클립의 미디어 참조를 찾지 못했습니다.")
        number(clip, "start_sample", minimum=0, integer=True)
        first = number(clip, "source_in", minimum=0, integer=True)
        last = number(clip, "source_out", minimum=0, integer=True)
        pending_legacy = data.get("_migrated_v1") and first == last == asset["samples"] == 0
        if not pending_legacy and not first < last <= asset["samples"]:
            raise ValueError("음원 클립의 원본 범위가 잘못되었습니다.")
        number(clip, "duration_samples", max(1, last-first), minimum=1, integer=True)
        offset = number(clip, "loop_offset_samples", 0, minimum=0, integer=True)
        if offset >= max(1, last-first) or (offset and "duration_samples" not in clip):
            raise ValueError("음원 반복 위치가 잘못되었습니다.")
        gain = number(clip, "gain", 1, minimum=0)
        if gain > 2: raise ValueError("음량은 0~200% 범위여야 합니다.")

    for clip in data.get("audio_clips", []) + data.get("videos", []):
        for key in ("fade_in_samples", "fade_out_samples", "envelope_offset_samples"):
            number(clip, key, 0, minimum=0, integer=True)
        if "envelope_length_samples" in clip:
            number(clip, "envelope_length_samples", minimum=1, integer=True)

    for item in data["texts"]:
        start = number(item, "start", minimum=0, integer=True)
        if number(item, "end", minimum=1, integer=True) <= start:
            raise ValueError("문구의 끝은 시작보다 뒤여야 합니다.")
        if not isinstance(item.get("text"), str) or len(item["text"]) > 100000:
            raise ValueError("문구는 100,000자 이내여야 합니다.")
        for key in ("x", "y", "width", "size"): number(item, key)
        if not 1 <= item["size"] <= 1024 or item["width"] <= 0:
            raise ValueError("문구 크기가 잘못되었습니다.")
        for key, default in (("color", "#ffffff"), ("background", "#000000")):
            value = item.get(key, default)
            if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                raise ValueError("문구 색상 형식이 잘못되었습니다.")
        if not isinstance(item.get("font", "malgun.ttf"), str): raise ValueError("글꼴 형식이 잘못되었습니다.")
        if item.get("align", "center") not in ("left", "center", "right"): raise ValueError("문구 정렬이 잘못되었습니다.")
        if number(item, "outline", 2, minimum=0, integer=True) > 20: raise ValueError("외곽선이 너무 큽니다.")
        if number(item, "background_alpha", 160, minimum=0, integer=True) > 255: raise ValueError("배경 투명도가 잘못되었습니다.")
        number(item, "order", 0)
