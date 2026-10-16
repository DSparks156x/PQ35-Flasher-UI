"""Where the app keeps settings, logs and the per-ECU backup library."""
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    override = os.environ.get("PQ35_DATA_DIR")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        path = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PQ35-Flasher"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "PQ35-Flasher"
    else:
        path = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "pq35-flasher"
    if os.environ.get("PQ35_DEMO"):
        path = path / "demo"  # keep the pretend module's backups out of the real library
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


class Settings:
    def __init__(self):
        self.path = data_dir() / "settings.json"
        try:
            self.values = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        try:
            self.path.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
        except OSError:
            pass


def ecu_key(family: str, identity: dict) -> str:
    part = identity.get("software_part_number") or identity.get("part_number") or "unknown"
    version = identity.get("sw_version") or identity.get("firmware_revision") or ""
    raw = f"{family}_{part}_{version}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")


class EcuRecord:
    """Backups, flash history and the best-known current image for one ECU.

    `state.bin` is what we believe the ECU holds right now: a fresh readout,
    or the previous state with the last flashed range written over it. It is
    what new images are diffed against to pick the smallest flash range.
    """

    def __init__(self, key: str):
        self.key = key
        self.root = data_dir() / "library" / key
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_path = self.root / "history.json"

    @property
    def state_path(self) -> Path:
        return self.root / "state.bin"

    def state(self):
        return self.state_path if self.state_path.is_file() else None

    def new_backup_dir(self) -> Path:
        path = self.root / "backups" / timestamp()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def history(self) -> list:
        try:
            return json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def add(self, entry: dict):
        entries = self.history()
        entries.append(dict(entry, time=time.strftime("%Y-%m-%d %H:%M:%S")))
        self.history_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def backups(self) -> list:
        return [e for e in self.history() if e.get("kind") == "backup" and e.get("image")]

    def has_backup(self) -> bool:
        return any(Path(e["image"]).is_file() for e in self.backups())

    def adopt_readout(self, image: str):
        shutil.copyfile(image, self.state_path)

    def forget_state(self):
        if self.state_path.exists():
            self.state_path.unlink()
