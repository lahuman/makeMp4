"""Resource limits shared by media workers without importing the editor UI."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

PROBE_TIMEOUT = 30
ENCODER_STALL_TIMEOUT = 300
DECODER_STALL_TIMEOUT = 120
SPACE_RESERVE = 64 * 1024 * 1024
logger = logging.getLogger("seonyuldam")
logger.addHandler(logging.NullHandler())
APP_VERSION = "2026.11.06"


def build_identity():
    import json
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    try:
        info = json.loads((root / "build-info.json").read_text(encoding="utf-8"))
        return {"version": APP_VERSION, "source_commit": info.get("source_commit"),
                "source_dirty": info.get("source_dirty"), "python": info.get("python") or sys.version,
                "toolchain": info.get("toolchain", {}).get("versions", {})}
    except (OSError, ValueError, AttributeError):
        return {"version": APP_VERSION, "source_commit": None, "python": sys.version,
                "mode": "source" if not getattr(sys, "frozen", False) else "manifest unavailable"}


class EditorError(Exception):
    pass


class EncoderError(EditorError):
    pass


def configure_logging(folder):
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers): return
    try:
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(folder / "app.log", maxBytes=1024*1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler); logger.setLevel(logging.INFO)
    except OSError:
        pass


def log_failure(stage, error):
    # File paths, media names, text and subprocess arguments can be private.
    logger.warning("%s failed type=%s", stage, type(error).__name__)


def require_space(path, required):
    folder = Path(path).resolve()
    while not folder.exists() and folder != folder.parent: folder = folder.parent
    try:
        free = shutil.disk_usage(folder).free
    except OSError as error:
        raise EditorError("작업 드라이브의 여유 공간을 확인할 수 없습니다.") from error
    if free < max(0, required) + SPACE_RESERVE:
        raise EditorError(f"디스크 여유 공간이 부족합니다. 최소 {(max(0, required)+SPACE_RESERVE)/1024**3:.2f} GiB가 필요합니다.")


def stop_process(process):
    if process.poll() is None:
        try: process.terminate()
        except OSError: pass
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()


def communicate_probe(process, cancel, timeout=None):
    deadline = time.monotonic() + (PROBE_TIMEOUT if timeout is None else timeout)
    try:
        while True:
            if cancel and cancel.is_set(): raise EditorError("미디어 분석을 취소했습니다.")
            try: return process.communicate(timeout=.1)
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise EditorError("미디어 정보 분석 제한 시간을 초과했습니다.")
    finally:
        if process.poll() is None:
            stop_process(process)
            process.communicate()


class ProcessGuard:
    """Interrupt blocked pipe reads/writes on cancellation or stalled progress."""
    def __init__(self, process, cancel, timeout):
        self.process, self.cancel, self.timeout = process, cancel, timeout
        self.finished = threading.Event()
        self.expired = threading.Event()
        self.activity = time.monotonic()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def touch(self):
        self.activity = time.monotonic()

    def _watch(self):
        while not self.finished.wait(.05) and self.process.poll() is None:
            if self.cancel.is_set():
                stop_process(self.process); return
            if time.monotonic() - self.activity >= self.timeout:
                self.expired.set()
                stop_process(self.process); return

    def check(self):
        if self.cancel.is_set(): raise EditorError("작업을 취소했습니다.")
        if self.expired.is_set(): raise EditorError("미디어 처리 진행이 멈춰 작업을 종료했습니다. 입력 파일과 드라이브를 확인하세요.")

    def close(self):
        self.finished.set()
        stop_process(self.process)
        self.thread.join(3)
