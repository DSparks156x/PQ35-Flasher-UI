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
import re
import shutil
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
    # Read-only BCM boot/vector page (0x7FC000..0x7FFFFF). Nothing is written.
    "pageff": ["--read-bcm-pageff"],
    # Read-only BCM pre-flash backup bundle (identity, coding, DTCs, targets, page FF).
    "backup": ["--bcm-backup"],
    # Read-only BCM programming-condition check (supply, counter, flags). Nothing is written.
    "preconditions": ["--bcm-preconditions"],
}
PAGEFF_FLAG = "--read-bcm-pageff"
BACKUP_FLAG = "--bcm-backup"
PRECONDITIONS_FLAG = "--bcm-preconditions"
# "chunk 12/256" in whatever the page FF read prints or logs.
CHUNK = re.compile(r"chunk\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
# An absolute path ending in .bin, spaces allowed ("Saved C:\My Files\x.bin").
BIN_PATH = re.compile(r"((?:[A-Za-z]:)?[\\/][^\"'\n]*?\.bin)(?![\w.])")
SAMPLE_FLAGS = {
    "motion": ("--motion-samples", "--motion-interval-ms"),
    "assist": ("--assist-samples", "--assist-interval-ms"),
    "supply": ("--supply-samples", "--supply-interval-ms"),
    "can_config": ("--can-config-samples", "--can-config-interval-ms"),
}
FAMILY_LABELS = {
    "haldex-gen4": "Haldex Gen4 (AWD)",
    "pq-eps": "EPS steering rack",
    "pq-bcm": "BCM",
}


class WorkerError(Exception):
    pass


def flasher_has_flag(flag, module="bcm") -> bool:
    """Whether this flasher build knows ``flag`` (the submodule may predate it)."""
    try:
        import flasher.runner as runner
        from flasher.controllers.registry import family_for_module
        return flag in runner.build_parser(family_for_module(module))._option_string_actions
    except Exception:
        return False


def saved_bins(text: str, printed: list) -> list:
    """Existing .bin files named in flasher output, last mention last."""
    found = []
    for line in text.splitlines():
        found += BIN_PATH.findall(line)
        found += [token.strip("\"',;:()") for token in line.split() if token.lower().endswith(".bin")]

    def walk(value):
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and value.lower().endswith(".bin"):
            found.append(value)
    walk(printed)
    paths = []
    for candidate in found:
        path = Path(candidate.strip())
        if path.is_file() and path.resolve() not in paths:
            paths.append(path.resolve())
    return paths


def sidecar_for(image: Path):
    for candidate in (image.with_suffix(".json"), Path(str(image) + ".json")):
        if candidate.is_file():
            return candidate
    return None


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
                self.server.on_line(line)
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
        self.server.on_line(message)
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
        self.chunk_progress = False
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
            return images.inspect_image(params["path"], params.get("family"), params.get("ecu_part"),
                                        params.get("ecu_version"))
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
            "can_flash": family.supports("flash") and (
                family.name != "pq-bcm" or flasher_has_flag("--bcm-sector")),
            "read_pageff": family.name == "pq-bcm" and flasher_has_flag(PAGEFF_FLAG),
            "can_backup": family.name == "pq-bcm" and family.supports("backup")
                          and flasher_has_flag(BACKUP_FLAG),
            "can_check_preconditions": family.name == "pq-bcm" and flasher_has_flag(PRECONDITIONS_FLAG),
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
        if operation == "pageff" and not flasher_has_flag(PAGEFF_FLAG):
            raise WorkerError("This flasher build has no BCM page FF read (--read-bcm-pageff) yet. "
                              "Update the flasher submodule, then restart the app.")
        if operation == "backup" and not flasher_has_flag(BACKUP_FLAG):
            raise WorkerError("This flasher build has no BCM backup (--bcm-backup) yet. "
                              "Update the flasher submodule, then restart the app.")
        if operation == "preconditions" and not flasher_has_flag(PRECONDITIONS_FLAG):
            raise WorkerError("This flasher build has no BCM precondition check yet. "
                              "Update the flasher submodule, then restart the app.")
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
        elif operation == "backup":
            out = out or tempfile.mkdtemp(prefix="pq35-bcm-backup-")
            argv += ["--out", out]
        elif operation == "flash":
            argv += ["--input", params["input"]]
            sectors = params.get("sectors")
            if sectors:
                # BCM flashes by 16 KiB sector (non-contiguous allowed).
                for sector in sectors:
                    argv += ["--bcm-sector", hex(sector) if isinstance(sector, int) else str(sector)]
            else:
                argv += ["--start", hex(params["start"]), "--end", hex(params["end"])]
            argv.append("--yes")
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
        self.chunk_progress = operation == "pageff"
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
            self.chunk_progress = False
            self.lock.release()
        text = self.stdout.take()
        printed = json_objects(text)
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
        if operation == "pageff":
            result.update(self.collect_pageff(text, printed, params.get("out_dir")))
            if failure is None and code:
                failure = f"flasher exited with {code}"
            if failure is None and not result.get("image"):
                failure = "The page FF read finished but no saved .bin was reported."
        if operation == "backup":
            manifest = Path(out) / "backup_manifest.json" if out else None
            if manifest and manifest.is_file():
                result["result"] = json.loads(manifest.read_text(encoding="utf-8"))
            result["backup_dir"] = out
            if failure is None and code:
                failure = f"BCM backup finished with exit {code}; see the backup folder."
        if failure is not None:
            result["error"] = failure
            if operation == "flash" and self.last_flash_result is not None:
                result["result"] = self.last_flash_result
        return result

    def collect_pageff(self, text, printed, out_dir):
        """The dump and its JSON sidecar, moved into ``out_dir`` (the module's
        folder) when given. The flasher picks the file name."""
        images = saved_bins(text, printed)
        if not images:
            return {"image": None, "sidecar": None}
        image = images[-1]
        sidecar = sidecar_for(image)
        if out_dir:
            target = Path(out_dir)
            target.mkdir(parents=True, exist_ok=True)
            moved = []
            for path in (image, sidecar):
                if path is not None and path.parent.resolve() != target.resolve():
                    destination = target / path.name
                    shutil.move(str(path), str(destination))
                    path = destination
                moved.append(path)
            image, sidecar = moved
        return {"image": str(image), "sidecar": str(sidecar) if sidecar else None,
                "out": str(image.parent)}

    def on_line(self, line):
        """Turns "chunk x/256" output into progress when the flasher only prints it."""
        if not self.chunk_progress:
            return
        match = CHUNK.search(line)
        if match:
            done, total = int(match.group(1)), int(match.group(2))
            if total:
                self.progress("PAGE FF", 100.0 * done / total, f"chunk {done}/{total}")

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
