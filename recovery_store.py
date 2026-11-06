"""Session-specific recovery snapshots, separate from user project files."""
from pathlib import Path
import uuid

from disk_cache import FileLease
import editor_core as core


class RecoveryStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ident = uuid.uuid4().hex
        self.path = self.root / (self.ident + ".json")
        self.lease = FileLease(self.root / (self.ident + ".lock"))
        if not self.lease.acquire(): raise OSError("recovery session busy")

    def save(self, project):
        core.save_project(project, self.path)

    def candidates(self):
        for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path == self.path or not path.stem.isalnum() or len(path.stem) != 32: continue
            lease = FileLease(path.with_suffix(".lock"))
            if not lease.acquire(): continue
            try:
                # Keep the lock until the caller accepts or declines this snapshot.
                yield path
            finally:
                lease.close()

    def discard(self):
        self.path.unlink(missing_ok=True)

    def close(self, discard=False):
        if discard: self.discard()
        self.lease.close()
        self.lease.path.unlink(missing_ok=True)
