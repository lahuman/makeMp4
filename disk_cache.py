"""Best-effort cache budgets; never evict files used by another app instance."""
import atexit
from contextlib import contextmanager
import os
from pathlib import Path
import re
import threading
import time
import uuid

from media_runtime import EditorError

CACHE_BUDGET = 8 * 1024**3
CACHE_MAX_AGE = 30 * 86400
ARTIFACT = re.compile(r"([0-9a-f]{64})\.(f32le|peaks|mp4|png|json)$")
_managers = {}
_mutex = threading.RLock()


class FileLease:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def acquire(self):
        self.file = self.path.open("a+b")
        if not os.fstat(self.file.fileno()).st_size:
            self.file.write(b"0"); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close(); self.file = None
            return False

    def close(self):
        if self.file:
            self.file.close(); self.file = None


class DiskCache:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.keys = set()
        self.lease = FileLease(self.root / (".session-" + uuid.uuid4().hex + ".lock"))
        if not self.lease.acquire(): raise OSError("cache session busy")

    def pin(self, key, budget=CACHE_BUDGET, max_age=CACHE_MAX_AGE):
        if key in self.keys: return
        maintenance = FileLease(self.root / ".maintenance.lock")
        deadline = time.monotonic() + 30
        while not maintenance.acquire():
            if time.monotonic() >= deadline: raise EditorError("캐시 정리를 기다리는 시간이 초과되었습니다.")
            time.sleep(.05)
        self.keys.add(key)
        try:
            for path in self.root.glob(".session-*.lock"):
                if path == self.lease.path: continue
                other = FileLease(path)
                if not other.acquire(): return  # Another live instance: defer eviction.
                other.close()
                path.unlink(missing_ok=True)
            groups = {}
            for path in self.root.iterdir():
                match = ARTIFACT.fullmatch(path.name)
                if match and path.is_file() and not path.is_symlink():
                    stat = path.stat()
                    groups.setdefault(match[1], []).append((path, stat.st_size, stat.st_mtime))
            total = sum(size for entries in groups.values() for _, size, _ in entries)
            now = time.time()
            for ident, entries in sorted(groups.items(), key=lambda pair: max(x[2] for x in pair[1])):
                if ident in self.keys: continue
                if total <= budget and now-max(x[2] for x in entries) <= max_age: continue
                # Delete manifest first so a partial cleanup cannot be reused as complete.
                for path, size, _ in sorted(entries, key=lambda x: x[0].suffix != ".json"):
                    path.unlink(missing_ok=True); total -= size
            for path, _, _ in groups.get(key, []): path.touch()
        finally:
            maintenance.close()

    def close(self):
        self.lease.close()
        try: self.lease.path.unlink(missing_ok=True)
        except OSError: pass


def pin_cache(root, key):
    # Keep all files referenced by this process pinned, including undo history.
    with _mutex:
        try:
            root = str(Path(root).resolve())
            manager = _managers.get(root)
            if manager is None:
                manager = _managers[root] = DiskCache(root)
            manager.pin(key)
        except OSError as error:
            raise EditorError("미디어 캐시에 접근할 수 없습니다. 권한과 여유 공간을 확인하세요.") from error


@contextmanager
def cache_write_lock(root, key, cancel):
    """Serialize publication of a cache bundle across threads and instances."""
    if cancel.is_set(): raise EditorError("미디어 분석을 취소했습니다.")
    lease = FileLease(Path(root) / ("." + key + ".write.lock"))
    deadline = time.monotonic() + 30
    while not lease.acquire():
        if cancel.wait(.1):
            raise EditorError("미디어 분석을 취소했습니다.")
        if time.monotonic() >= deadline: raise EditorError("다른 미디어 작업의 캐시 사용을 기다리는 시간이 초과되었습니다.")
    try:
        if cancel.is_set(): raise EditorError("미디어 분석을 취소했습니다.")
        yield
    finally:
        lease.close()


def close_caches():
    with _mutex:
        for manager in _managers.values(): manager.close()
        _managers.clear()


atexit.register(close_caches)
