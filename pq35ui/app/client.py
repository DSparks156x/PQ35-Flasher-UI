"""Talks to worker processes over JSON lines (see pq35ui/worker/protocol.py)."""
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from .storage import REPO_ROOT, data_dir


def _windowless(python: str) -> str:
    """Use pythonw.exe for workers on Windows so no console window flashes up."""
    path = Path(python)
    if sys.platform == "win32" and path.name.lower() == "python.exe":
        candidate = path.with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return python


def find_python32(settings) -> str | None:
    """A 32-bit CPython for J2534 (vendor PassThru DLLs are 32-bit)."""
    candidates = [settings.get("python32"), os.environ.get("PQ35_PYTHON32")]
    marker = REPO_ROOT / ".python32"
    if marker.is_file():
        candidates.append(marker.read_text(encoding="utf-8").strip())
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    launcher = shutil.which("py")
    if launcher:
        try:
            listing = subprocess.run([launcher, "-0p"], capture_output=True, text=True,
                                     timeout=10, creationflags=flags).stdout
            for line in listing.splitlines():  # " -V:3.11-32   C:\...\python.exe"
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith("-32") and Path(parts[-1]).is_file():
                    return parts[-1]
        except (OSError, subprocess.SubprocessError):
            pass
    uv = shutil.which("uv")
    if uv:
        try:
            found = subprocess.run([uv, "python", "find", "cpython-3.12-windows-x86"],
                                   capture_output=True, text=True, timeout=20,
                                   creationflags=flags).stdout.strip()
            if found and Path(found).is_file():
                return found
        except (OSError, subprocess.SubprocessError):
            pass
    return None


class WorkerClient(QObject):
    event = Signal(dict)
    exited = Signal(str)

    def __init__(self, python: str, label: str, parent=None):
        super().__init__(parent)
        self.python = python
        self.label = label
        self.next_id = 1
        self.pending = {}
        self.buffer = b""
        self.process = QProcess(self)
        self.process.setProgram(_windowless(python))
        self.process.setArguments(["-u", "-m", "pq35ui.worker"])
        self.process.setWorkingDirectory(str(REPO_ROOT))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", str(REPO_ROOT))
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PQ35_DATA_DIR", str(data_dir()))
        env.remove("PYTHONHOME")
        self.process.setProcessEnvironment(env)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)

    def running(self) -> bool:
        return self.process.state() != QProcess.ProcessState.NotRunning

    def start(self):
        if not self.running():
            self.buffer = b""
            self.process.start()

    def call(self, method, params=None, on_result=None, on_error=None):
        self.start()
        request_id = self.next_id
        self.next_id += 1
        self.pending[request_id] = (method, on_result, on_error)
        message = {"id": request_id, "method": method, "params": params or {}}
        self.process.write((json.dumps(message) + "\n").encode("utf-8"))
        return request_id

    def stop(self):
        if self.running():
            self.process.write(b'{"id":0,"method":"shutdown"}\n')
            if not self.process.waitForFinished(3000):
                self.process.kill()
                self.process.waitForFinished(1000)

    def _read(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.event.emit({"event": "log", "level": "warning", "logger": self.label,
                                 "message": line.decode("utf-8", "replace")})
                continue
            if "id" in message and message["id"] in self.pending:
                method, on_result, on_error = self.pending.pop(message["id"])
                if "error" in message:
                    error = message["error"]
                    self.event.emit({"event": "log", "level": "error", "logger": self.label,
                                     "message": f"{method}: {error.get('message')}"})
                    if error.get("traceback"):
                        self.event.emit({"event": "log", "level": "debug", "logger": self.label,
                                         "message": error["traceback"]})
                    if on_error:
                        on_error(error)
                elif on_result:
                    on_result(message.get("result"))
            elif "event" in message:
                self.event.emit(message)

    def _read_stderr(self):
        text = bytes(self.process.readAllStandardError()).decode("utf-8", "replace").strip()
        if text:
            self.event.emit({"event": "log", "level": "warning", "logger": self.label,
                             "message": text})

    def _fail_pending(self, reason):
        pending, self.pending = self.pending, {}
        for method, _, on_error in pending.values():
            if on_error:
                on_error({"type": "WorkerDied", "message": reason})

    def _finished(self, code, _status):
        self._fail_pending(f"The {self.label} worker stopped (exit code {code}).")
        self.exited.emit(f"exit code {code}")

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._fail_pending(f"Couldn't start {self.python}.")
            self.exited.emit("failed to start")


class WorkerPool(QObject):
    """One native worker for offline work and non-J2534 adapters, plus a
    32-bit worker for J2534 when this UI runs on 64-bit Python."""
    event = Signal(dict)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.clients = {}

    def _client(self, python, label):
        client = self.clients.get(python)
        if client is None:
            client = WorkerClient(python, label, self)
            client.event.connect(self.event)
            self.clients[python] = client
        return client

    def native(self) -> WorkerClient:
        return self._client(sys.executable, "worker")

    def for_adapter(self, adapter: dict) -> WorkerClient:
        if adapter.get("adapter") == "demo":
            return self.native()
        if adapter.get("adapter") == "j2534" and struct.calcsize("P") != 4:
            python = find_python32(self.settings)
            if python is None:
                raise RuntimeError(
                    "J2534 adapters need a 32-bit Python, and none was found. "
                    "Re-run the installer, or set its path under Settings.")
            return self._client(python, "worker32")
        return self.native()

    def stop(self):
        for client in self.clients.values():
            client.stop()
