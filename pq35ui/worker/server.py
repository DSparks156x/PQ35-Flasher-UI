"""Headless worker: owns the CAN adapter and runs flasher operations for the UI.

Stdlib only (the J2534 worker is a bare 32-bit Python). One hardware job at a
time, guarded by a cross-process lock file so two UI windows can never drive
the adapter together. Offline requests are answered while a job runs.
"""
import io
import json
import logging
import os
import platform
import struct
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from . import discovery
from .protocol import PROTOCOL_VERSION, encode, json_objects

HARDWARE_OPERATIONS = {
    "identify": ["--ident-only"],
    "readout": ["--readout"],
    "flash": [],
    "eeprom": ["--read-eeprom"],
    "faults": ["--read-eps-faults"],
    "motion": ["--read-eps-motion"],
    "assist": ["--read-eps-assist"],
    "supply": ["--read-eps-supply"],
    "can_config": ["--read-eps-can-config"],
}
SAMPLE_FLAGS = {
    "motion": ("--motion-samples", "--motion-interval-ms"),
    "assist": ("--assist-samples", "--assist-interval-ms"),
    "supply": ("--supply-samples", "--supply-interval-ms"),
    "can_config": ("--can-config-samples", "--can-config-interval-ms"),
}
FAMILY_LABELS = {
    "haldex-gen4": "Haldex Gen4 (AWD)",
    "pq-eps": "EPS steering rack",
    "pq-bcm": "BCM (read-only)",
}


class WorkerError(Exception):
    pass


class HardwareLock:
    """Exclusive lock held for the duration of one hardware job."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise WorkerError("Another PQ35 Flasher window is using the CAN adapter. "
                              "Close it first.")
        self.handle = handle

    def release(self):
        if self.handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class Capture(io.TextIOBase):
    """Stands in for sys.stdout/stderr: flasher prints go to the UI log and
    are kept so the JSON it prints can be parsed afterwards."""

    def __init__(self, server, stream):
        self.server = server
        self.stream = stream
        self.parts = []
        self.pending = ""

    def writable(self):
        return True

    def write(self, text):
        self.parts.append(text)
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                self.server.emit({"event": "output", "stream": self.stream, "line": line})
        return len(text)

    def take(self) -> str:
        text = "".join(self.parts)
        self.parts = []
        return text


class EventHandler(logging.Handler):
    def __init__(self, server):
        super().__init__(logging.DEBUG)
        self.server = server

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        self.server.emit({"event": "log", "level": record.levelname.lower(),
                          "logger": record.name, "message": message})
        if message.startswith("FLASH RESULT "):
            try:
                self.server.last_flash_result = json.loads(message[len("FLASH RESULT "):])
            except ValueError:
                pass


class Server:
    def __init__(self, proto_out, requests_in, data_dir: Path):
        self.out = proto_out
        self.requests = requests_in
        self.data_dir = data_dir
        self.write_lock = threading.Lock()
        self.job = None
        self.lock = HardwareLock(data_dir / "hardware.lock")
        self.last_flash_result = None
        self.stdout = Capture(self, "stdout")
        self.stderr = Capture(self, "stderr")

    # --- plumbing ---

    def emit(self, message: dict):
        with self.write_lock:
            self.out.write(encode(message))
            self.out.flush()

    def serve(self):
        self.emit({"event": "ready", "protocol": PROTOCOL_VERSION, "pid": os.getpid()})
        for raw in self.requests:
            try:
                request = json.loads(raw)
            except ValueError:
                continue
            method = request.get("method", "")
            if method == "shutdown":
                self.emit({"id": request.get("id"), "result": True})
                break
            if method == "run":
                self.start_job(request)
                continue
            self.answer(request, lambda: self.dispatch(method, request.get("params") or {}))
        if self.job is not None:
            self.job.join()

    def answer(self, request, call):
        try:
            self.emit({"id": request.get("id"), "result": call()})
        except SystemExit as exc:
            self.emit({"id": request.get("id"), "error": {
                "type": "UsageError", "message": self.stderr.take().strip() or str(exc)}})
        except Exception as exc:
            self.emit({"id": request.get("id"), "error": {
                "type": type(exc).__name__, "message": str(exc),
                "traceback": traceback.format_exc()}})

    # --- offline methods ---

    def dispatch(self, method, params):
        if method == "hello":
            return self.hello()
        if method == "list_interfaces":
            if os.environ.get("PQ35_DEMO"):
                # Demo mode never offers real hardware, so it can't be driven by mistake.
                from .demo import adapter_entry
                return [adapter_entry()]
            return discovery.list_interfaces()
        if method == "families":
            return self.families()
        if method == "inspect_image":
            from . import images
            return images.inspect_image(params["path"], params.get("family"), params.get("ecu_part"))
        if method == "plan_flash":
            from . import images
            return images.plan_flash(params["path"], params["family"], params.get("reference"),
                                     params.get("correct_checksum", False),
                                     params.get("simulator_mode", False),
                                     params.get("ecu_version"))
        if method == "eps_patch_status":
            from . import images
            return images.eps_patch_status(params["path"])
        if method == "eps_patch":
            from . import images
            return images.eps_patch(params["path"], params["feature"], params["out"])
        if method == "compose_state":
            from . import images
            return images.compose_state(params["path"], params["family"], params["start"],
                                        params["end"], params["out"], params.get("reference"),
                                        params.get("correct_checksum", False),
                                        params.get("simulator_mode", False))
        if method == "convert_sgo":
            from . import sgo
            return sgo.convert_eps(params["path"], params["out"])
        if method == "haldex_checksums":
            return self.haldex_checksums(params["path"])
        if method == "busy":
            return self.job is not None
        raise WorkerError(f"Unknown method {method!r}")

    def hello(self):
        flasher_dir = Path(__import__("flasher").__file__).resolve().parent
        commit = None
        head = flasher_dir / ".git"
        try:
            if head.is_file():  # submodule: "gitdir: ../.git/modules/flasher"
                head = (flasher_dir / head.read_text().split(":", 1)[1].strip()).resolve()
            ref = (head / "HEAD").read_text().strip()
            if ref.startswith("ref:"):
                ref = (head / ref.split(" ", 1)[1]).read_text().strip()
            commit = ref[:10]
        except OSError:
            pass
        return {
            "python": platform.python_version(),
            "executable": sys.executable,
            "bits": struct.calcsize("P") * 8,
            "platform": sys.platform,
            "flasher_commit": commit,
            "backends": discovery.backends(),
            "demo": bool(os.environ.get("PQ35_DEMO")),
        }

    def families(self):
        from flasher.controllers.registry import FAMILIES
        return [{
            "name": family.name,
            "label": FAMILY_LABELS.get(family.name, family.name),
            "module": family.module,
            "alias": family.module_aliases[0],
            "operations": sorted(family.operations),
            "part_prefixes": list(family.part_prefixes),
            # The BCM flash path is an unfinished scaffold upstream.
            "can_flash": family.supports("flash") and family.name != "pq-bcm",
        } for family in FAMILIES]

    def haldex_checksums(self, path):
        from flasher.controllers.haldex_gen4.patches import application_checksums, IMAGE_SIZE
        data = Path(path).read_bytes()
        if len(data) != IMAGE_SIZE:
            raise WorkerError("Haldex images are exactly 320 KiB")
        return application_checksums(data[0x18000:0x50000], 0x18000)

    # --- hardware jobs ---

    def start_job(self, request):
        if self.job is not None:
            self.emit({"id": request.get("id"), "error": {
                "type": "Busy", "message": "A hardware operation is already running."}})
            return
        self.job = threading.Thread(target=self.run_job, args=(request,), daemon=True)
        self.job.start()

    def run_job(self, request):
        params = request.get("params") or {}
        self.emit({"event": "busy", "busy": True, "operation": params.get("operation")})
        try:
            self.answer(request, lambda: self.run_hardware(params))
        finally:
            self.job = None
            self.emit({"event": "busy", "busy": False})

    def run_hardware(self, params):
        import flasher.runner as runner
        operation = params["operation"]
        if operation not in HARDWARE_OPERATIONS and operation != "scan":
            raise WorkerError(f"Unknown operation {operation!r}")
        adapter = params["adapter"]
        if adapter["adapter"] == "demo":
            from .demo import DemoModule
            return DemoModule(self, self.data_dir).run(params)
        if operation == "scan":
            from .scan import scan
            self.lock.acquire()
            try:
                return {"operation": "scan", "exit_code": 0,
                        "result": scan(adapter, self.progress, runner.open_adapter)}
            finally:
                self.lock.release()
        argv = ["--module", params["module"], "--adapter", adapter["adapter"]]
        for key in ("dll", "channel", "serial", "bus", "baud"):
            if adapter.get(key) not in (None, ""):
                argv += [f"--{key}", str(adapter[key])]
        argv += HARDWARE_OPERATIONS[operation]
        out = params.get("out")
        if operation == "readout":
            out = out or tempfile.mkdtemp(prefix="pq35-readout-")
            argv += ["--out", out]
        elif operation in ("eeprom", "faults", "motion", "assist", "supply", "can_config"):
            out = out or os.path.join(tempfile.mkdtemp(prefix="pq35-diag-"),
                                      operation + (".bin" if operation == "eeprom" else ".json"))
            argv += ["--out", out]
            if operation in SAMPLE_FLAGS and params.get("samples"):
                count, interval = SAMPLE_FLAGS[operation]
                argv += [count, str(params["samples"]), interval, str(params.get("interval_ms", 100))]
        elif operation == "flash":
            argv += ["--input", params["input"], "--start", hex(params["start"]),
                     "--end", hex(params["end"]), "--yes"]
            for flag in ("correct_checksum", "simulator_mode", "recovery"):
                if params.get(flag):
                    argv.append("--" + flag.replace("_", "-"))
        if params.get("dry_run"):
            argv.append("--dry-run")
        if params.get("verbose"):
            argv.append("--verbose")

        self.emit({"event": "log", "level": "info", "logger": "worker",
                   "message": "flasher.runner " + " ".join(argv)})
        self.lock.acquire()
        self.last_flash_result = None
        self.stdout.take()
        started = time.monotonic()
        runner._progress = self.progress
        try:
            code = runner.main(argv)
            failure = None
        except SystemExit as exc:
            if exc.code in (0, None):
                code, failure = 0, None
            else:
                text = self.stderr.take().strip()
                raise WorkerError(text.splitlines()[-1] if text
                                  else f"flasher exited with {exc.code}") from None
        except Exception as exc:
            code, failure = 1, f"{type(exc).__name__}: {exc}"
        finally:
            self.lock.release()
        printed = json_objects(self.stdout.take())
        result = {
            "operation": operation, "exit_code": code, "argv": argv, "out": out,
            "elapsed_sec": round(time.monotonic() - started, 1),
            "result": printed[-1] if printed else None,
            "printed": printed,
        }
        if operation == "readout" and out and (Path(out) / "report.json").is_file():
            result["result"] = json.loads((Path(out) / "report.json").read_text(encoding="utf-8"))
            images = sorted(Path(out).glob("*.bin"), key=lambda p: (p.name != "pass1.bin", p.name))
            result["image"] = str(images[0]) if images else None
        if failure is not None:
            result["error"] = failure
            if operation == "flash" and self.last_flash_result is not None:
                result["result"] = self.last_flash_result
        return result

    def progress(self, stage, percent, detail="", speed=0.0, eta_sec=0.0):
        self.emit({"event": "progress", "stage": stage, "percent": round(float(percent), 1),
                   "detail": detail, "speed": speed, "eta_sec": eta_sec})


def main():
    proto_out = sys.stdout.buffer
    requests_in = sys.stdin.buffer
    data_dir = Path(os.environ.get("PQ35_DATA_DIR") or Path.home() / ".pq35-flasher")
    server = Server(proto_out, requests_in, data_dir)
    # Nothing but the protocol may reach the real stdout, and nothing may block on input().
    sys.stdout = server.stdout
    sys.stderr = server.stderr
    sys.stdin = io.StringIO("")
    root = logging.getLogger()
    root.handlers[:] = [EventHandler(server)]
    root.setLevel(logging.INFO)
    server.serve()
    return 0
