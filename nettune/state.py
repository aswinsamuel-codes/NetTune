"""Small JSON-file store for saved originals, profiles and history."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("NETTUNE_HOME")
    if not base:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or str(Path.home()), "NetTune")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


class Store:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else data_dir() / "state.json"
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self.path)

    def get(self, key: str, default=None):
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = copy.deepcopy(value)
            self._save()

    def update(self, key: str, default, fn):
        """Read-modify-write under the lock. ``fn`` receives and returns the value."""
        with self._lock:
            value = fn(copy.deepcopy(self._data.get(key, default)))
            self._data[key] = value
            self._save()
            return copy.deepcopy(value)
