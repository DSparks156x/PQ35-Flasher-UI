"""Which version is installed, whether GitHub has a newer one, and updating.

Updating runs the installer in update mode (PQ35_UPDATE=1): it waits for this
process to exit, pulls (or re-downloads a zip install), re-syncs the
environment and reopens the app. The app itself never rewrites its own files.
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .. import __version__
from .storage import REPO_ROOT, logs_dir

REPO = "DSparks156x/PQ35-Flasher-UI"
API = f"https://api.github.com/repos/{REPO}"


def _git_dir(root: Path):
    marker = root / ".git"
    if marker.is_file():  # submodule / worktree: "gitdir: <path>"
        return (root / marker.read_text().split(":", 1)[1].strip()).resolve()
    return marker if marker.is_dir() else None


def _head(git_dir: Path):
    try:
        ref = (git_dir / "HEAD").read_text().strip()
        if not ref.startswith("ref:"):
            return ref
        name = ref.split(" ", 1)[1]
        path = git_dir / name
        if path.is_file():
            return path.read_text().strip()
        for line in (git_dir / "packed-refs").read_text().splitlines():
            if line.endswith(" " + name):
                return line.split(" ", 1)[0]
    except OSError:
        pass
    return None


def local_version() -> dict:
    """Read straight from .git so it works without git on PATH."""
    git_dir = _git_dir(REPO_ROOT)
    commit = _head(git_dir) if git_dir else None
    source = "git" if git_dir else "zip"
    if commit is None:
        marker = REPO_ROOT / ".install-commit"
        commit = marker.read_text().strip() if marker.is_file() else None
    flasher_git = _git_dir(REPO_ROOT / "flasher")
    return {"version": __version__, "commit": commit, "source": source,
            "flasher_commit": _head(flasher_git) if flasher_git else None}


def _get(url: str) -> dict:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "pq35-flasher-ui"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def check_remote(local: dict) -> dict:
    """Compare the installed commit with main on GitHub."""
    commit = local.get("commit")
    if not commit:
        latest = _get(f"{API}/commits/main")
        return {"status": "unknown_local", "latest": latest["sha"], "behind_by": None, "changes": []}
    try:
        compare = _get(f"{API}/compare/{commit}...main")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # a local commit GitHub has never seen
            return {"status": "local_only", "behind_by": None, "changes": []}
        raise
    # GitHub describes main relative to the installed commit ("ahead" = main has
    # newer commits); turn that around into what the installed copy is.
    status = {"ahead": "behind", "behind": "ahead"}.get(compare["status"], compare["status"])
    changes = [c["commit"]["message"].splitlines()[0] for c in compare.get("commits", [])]
    return {"status": status,  # identical / behind (update available) / ahead / diverged
            "behind_by": compare.get("ahead_by", 0), "ahead_by": compare.get("behind_by", 0),
            "changes": list(reversed(changes))}


class UpdateChecker(QObject):
    finished = Signal(dict)

    def start(self, local: dict):
        def run():
            try:
                result = check_remote(local)
            except Exception as exc:  # offline, rate-limited, ...
                result = {"status": "error", "error": str(exc)}
            self.finished.emit(result)
        threading.Thread(target=run, daemon=True).start()


def launch_update() -> str:
    """Start the installer in update mode, detached. The caller then quits."""
    env = dict(os.environ, PQ35_UPDATE="1", PQ35_WAIT_PID=str(os.getpid()))
    env.pop("PQ35_DEMO", None)
    if sys.platform == "win32":
        script = REPO_ROOT / "install.ps1"
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(REPO_ROOT), env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return "A window shows the update; the app reopens when it's done."
    log = logs_dir() / "update.log"
    with open(log, "ab") as out:
        subprocess.Popen(["sh", str(REPO_ROOT / "install.sh")], cwd=str(REPO_ROOT), env=env,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    return f"The app reopens when the update is done (log: {log})."
