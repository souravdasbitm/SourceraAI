"""JSON plan store. One file per suite under .openqa/plans/.

The public methods are get, put, record_failure, invalidate, list, and clear.
Swap the class later if a SQLite backend is needed; callers only use this interface.
"""
import copy
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def entry_usable(entry: dict | None, max_failures: int, ttl_days: int, now: datetime | None = None) -> bool:
    """True when a stored plan may be replayed."""
    if not entry:
        return False
    if int(entry.get("failure_count") or 0) >= max_failures:
        return False
    stamp = entry.get("last_pass_at") or entry.get("created_at")
    if not stamp:
        return True
    try:
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if ttl_days <= 0:
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - seen <= timedelta(days=ttl_days)


def _safe_suite(suite: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (suite or "default"))
    return cleaned or "default"


class PlanStore:
    def __init__(self, suite: str, root: Path | str | None = None, app_version: str | None = None):
        self.suite = _safe_suite(suite)
        self.root = Path(root) if root is not None else Path(".openqa") / "plans"
        self.path = self.root / f"{self.suite}.json"
        if app_version is None:
            app_version = os.getenv("APP_VERSION", "")
        self.app_version = app_version
        self.root.mkdir(parents=True, exist_ok=True)
        self._sync_app_version()

    def get(self, key: str) -> dict | None:
        def read(data):
            entry = data["entries"].get(key)
            return copy.deepcopy(entry) if entry else None
        return self._locked(read, write=False)

    def put(self, key: str, entry: dict) -> None:
        def write(data):
            stored = copy.deepcopy(entry)
            stored["key"] = key
            data["entries"][key] = stored
            if self.app_version:
                data["app_version"] = self.app_version
        self._locked(write, write=True)

    def record_failure(self, key: str) -> None:
        def write(data):
            entry = data["entries"].get(key)
            if not entry:
                return False
            entry["failure_count"] = int(entry.get("failure_count") or 0) + 1
            return True
        self._locked(write, write=True)

    def invalidate(self, key: str) -> None:
        def write(data):
            data["entries"].pop(key, None)
        self._locked(write, write=True)

    def list(self) -> list:
        def read(data):
            return copy.deepcopy(list(data["entries"].values()))
        return self._locked(read, write=False)

    def clear(self) -> None:
        def write(data):
            data["entries"] = {}
        self._locked(write, write=True)

    def _sync_app_version(self) -> None:
        """Drop every entry when APP_VERSION changes from the value stored in the file."""
        def write(data):
            stored = str(data.get("app_version") or "")
            current = str(self.app_version or "")
            if current and stored != current:
                data["entries"] = {}
                data["app_version"] = current
                return True
            if current and not stored:
                data["app_version"] = current
                return True
            return False
        self._locked(write, write=True)

    def _empty(self) -> dict:
        return {"app_version": self.app_version, "entries": {}}

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("app_version", "")
        if not isinstance(data.get("entries"), dict):
            data["entries"] = {}
        return data

    def _write_unlocked(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    def _acquire(self) -> int:
        lock = self.path.with_suffix(".lock")
        stale_s = 30
        for _ in range(100):
            try:
                return os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > stale_s:
                        lock.unlink()
                        continue
                except OSError:
                    pass
                time.sleep(0.05)
        raise TimeoutError(f"Could not lock plan store {self.path}")

    def _locked(self, fn, write: bool):
        self.root.mkdir(parents=True, exist_ok=True)
        fd = self._acquire()
        lock = self.path.with_suffix(".lock")
        try:
            data = self._read_unlocked()
            result = fn(data)
            # record_failure returns False when the key is absent — skip the write
            should_write = write if result is not False else False
            if write and result is None:
                should_write = True
            if should_write:
                self._write_unlocked(data)
            return result
        finally:
            os.close(fd)
            try:
                lock.unlink()
            except OSError:
                pass
