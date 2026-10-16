"""Where things live.

Your files (module backups, copies of flashed images, patched images, logs)
go in "PQ35 Flasher" in your Documents folder on every OS, so they're easy to
find. Settings and lock files stay in the per-user app-data folder.
"""
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


def user_dir() -> Path:
    override = os.environ.get("PQ35_USER_DIR")
    if override:
        base = Path(override)
    elif os.environ.get("PQ35_DATA_DIR"):
        base = Path(os.environ["PQ35_DATA_DIR"]) / "user"  # tests and portable setups
    else:
        documents = ""
        try:
            from PySide6.QtCore import QStandardPaths
            documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        except Exception:
            pass
        base = Path(documents or Path.home() / "Documents") / "PQ35 Flasher"
    if os.environ.get("PQ35_DEMO"):
        base = base / "Demo"
    for name in ("Modules", "Images", "Patched", "Logs"):
        (base / name).mkdir(parents=True, exist_ok=True)
    return base


def modules_dir() -> Path:
    return user_dir() / "Modules"


def images_dir() -> Path:
    return user_dir() / "Images"


def patched_dir() -> Path:
    return user_dir() / "Patched"


def logs_dir() -> Path:
    return user_dir() / "Logs"


def migrate_old_library():
    """Move module records from the old app-data location to Documents."""
    old = data_dir() / "library"
    if not old.is_dir():
        return
    for child in old.iterdir():
        target = modules_dir() / child.name
        if not target.exists():
            try:
                shutil.move(str(child), str(target))
            except OSError:
                pass
    try:
        old.rmdir()
    except OSError:
        pass


def prune_logs(keep: int = 20):
    logs = sorted(logs_dir().glob("session_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in logs[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


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
    """Folder name for one physical module. Not the software version: that
    changes every time you flash, and the backups must stay with the module."""
    part = identity.get("software_part_number") or identity.get("part_number") or "unknown"
    raw = f"{family}_{part}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")


class EcuRecord:
    """Backups, flash history and the best-known current image for one ECU.

    `state.bin` is what we believe the ECU holds right now: a fresh readout,
    or the previous state with the last flashed range written over it. It is
    what new images are diffed against to pick the smallest flash range.
    """

    def __init__(self, key: str):
        self.key = key
        self.root = modules_dir() / key
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_path = self.root / "history.json"

    @property
    def state_path(self) -> Path:
        return self.root / "state.bin"

    def state(self):
        return self.state_path if self.state_path.is_file() else None

    def new_backup_dir(self) -> Path:
        path = self.root / "Backups" / timestamp()
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

    def keep_flashed(self, image: str) -> str:
        """Keep a copy of every image flashed to this module."""
        target = self.root / "Flashed" / f"{timestamp()}_{Path(image).name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image, target)
        return str(target)

    def adopt_readout(self, image: str):
        shutil.copyfile(image, self.state_path)

    def forget_state(self):
        if self.state_path.exists():
            self.state_path.unlink()
