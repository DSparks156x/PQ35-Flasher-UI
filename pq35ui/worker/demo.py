"""Demo adapter: a pretend module for trying the UI with no hardware.

Every operation is simulated. Images still go through the real flasher
checks (prepare_image), so a file the demo accepts is one the real flash
would accept too. The pretend module remembers what was "flashed" to it in
<data dir>/demo/<family>.bin, so backups and range planning behave like the
real thing. Seed it with a real image via PQ35_DEMO_HALDEX / PQ35_DEMO_EPS.
"""
import hashlib
import json
import os
import random
import time
from pathlib import Path

from flasher.controllers.registry import get_family, parse_module, family_for_module

HALDEX, EPS, BCM = "haldex-gen4", "pq-eps", "pq-bcm"
DEFAULT_IDENTITY = {
    HALDEX: ("0BR907554A", "6816", "Haldex 4Motion"),
    EPS: ("1K0909144R", "3501", "EPS_ZFLS Kl.3"),
    BCM: ("8P0907063E", "0040", "Bordnetz-SG"),
}
SEED_ENV = {HALDEX: "PQ35_DEMO_HALDEX", EPS: "PQ35_DEMO_EPS"}


def adapter_entry():
    return {"adapter": "demo", "label": "Demo · pretend module (no hardware)", "present": True}


class DemoModule:
    def __init__(self, server, data_dir: Path):
        self.server = server
        self.root = data_dir / "demo"
        self.root.mkdir(parents=True, exist_ok=True)

    # --- pretend ECU contents ---

    def image_path(self, family) -> Path:
        return self.root / f"{family}.bin"

    def image(self, family) -> bytes:
        path = self.image_path(family)
        if not path.is_file():
            seed = os.environ.get(SEED_ENV.get(family, ""), "")
            data = Path(seed).read_bytes() if seed and Path(seed).is_file() else self._synthetic(family)
            path.write_bytes(data)
        return path.read_bytes()

    def _synthetic(self, family) -> bytes:
        if family == HALDEX:
            patches = get_family(HALDEX).patches()
            data = bytearray(b"\xff" * patches.IMAGE_SIZE)
            for start, size in patches.APP_BLOCKS:
                checksum = patches.calculate_app_checksum(bytes(data[start:start + size]))
                data[start + size - 2:start + size] = checksum.to_bytes(2, "little")
            return bytes(data)
        if family == EPS:
            patches = get_family(EPS).patches()
            data = bytearray(b"\xff" * patches.IMAGE_SIZE)
            data[0x5DFF8:0x5DFFC] = patches.CRC_MARKER
            return patches.update_checksums(bytes(data))
        return b"\xff" * 0x1000

    def identity(self, family) -> dict:
        part, version, desc = DEFAULT_IDENTITY[family]
        if family in SEED_ENV:
            from .images import _part_numbers
            found = _part_numbers(self.image(family))
            prefixes = get_family(family).part_prefixes
            found = [p for p in found if p.startswith(prefixes)]
            part = found[-1] if found else part
            if family == EPS:
                from .images import eps_firmware
                firmware = eps_firmware(self.image(family))
                if firmware:
                    part, version = firmware
        history = self._history(family)
        return {
            "software_part_number": part, "part_number": part, "sw_version": version,
            "firmware_revision": version, "system_desc": desc + " (demo)",
            "flash_status": 0, "flash_attempts": history["attempts"],
            "flash_counter": history["successes"], "flash_date": history["date"],
            "connected": True, "controller_family": family, "demo": True,
        }

    def _history(self, family):
        path = self.root / f"{family}.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {"attempts": 0, "successes": 0, "date": None}

    def _save_history(self, family, history):
        (self.root / f"{family}.json").write_text(json.dumps(history))

    # --- operations ---

    def _sleep(self, seconds):
        time.sleep(seconds)

    def _progress(self, stage, start, end, seconds, detail=lambda f: "", steps=25):
        for step in range(steps + 1):
            fraction = step / steps
            self.server.progress(stage, start + (end - start) * fraction, detail(fraction))
            self._sleep(seconds / steps)

    def _transfer(self, stage, start, end, total, seconds, steps=60):
        """Byte-level progress with speed and ETA, like the real readout/flash."""
        began = time.monotonic()
        for step in range(steps + 1):
            done = total * step // steps
            elapsed = max(time.monotonic() - began, 1e-3)
            speed = done / elapsed if step else 0.0
            eta = (total - done) / speed if speed else 0.0
            self.server.progress(stage, start + (end - start) * step / steps,
                                 f"{done}/{total} bytes", speed, eta)
            if step < steps:
                self._sleep(seconds / steps)

    def log(self, message):
        self.server.emit({"event": "log", "level": "info", "logger": "demo", "message": message})

    def run(self, params) -> dict:
        family = family_for_module(parse_module(params["module"])).name
        operation = params["operation"]
        started = time.monotonic()
        result = {"operation": operation, "exit_code": 0, "argv": ["(demo)"], "out": params.get("out"),
                  "demo": True}
        self.log(f"DEMO {operation} on {family}: no CAN traffic")
        self._sleep(0.6)
        if operation == "identify":
            result["result"] = self.identity(family)
        elif operation == "readout":
            result.update(self._readout(family, params))
        elif operation == "flash":
            result.update(self._flash(family, params))
        elif operation == "eeprom":
            out = Path(params.get("out") or self.root / "eeprom.bin")
            out.write_bytes(bytes(random.Random(1).randrange(256) for _ in range(1024)))
            result.update(out=str(out), result={"status": "ok", "size": 1024, "path": str(out)})
        else:
            result["result"] = self._diagnostic(operation, params)
        result["elapsed_sec"] = round(time.monotonic() - started, 1)
        return result

    def _readout(self, family, params):
        data = self.image(family)
        out = Path(params.get("out") or self.root / f"readout-{int(time.time())}")
        out.mkdir(parents=True, exist_ok=False)
        # The real Haldex readout covers the application range only.
        total = 0x38000 if family == HALDEX else len(data)
        self._transfer("READ", 0, 100, total, 10.0)
        name = "pass1.bin" if family == HALDEX else "kwp_readout.bin"
        (out / name).write_bytes(data)
        status = {HALDEX: "captured_checksums_valid", EPS: "captured_complete"}.get(family, "captured_complete")
        report = {"operation": "readout", "controller_family": family, "status": status,
                  "controller": self.identity(family), "demo": True,
                  "sha256": hashlib.sha256(data).hexdigest().upper()}
        (out / "report.json").write_text(json.dumps(report, indent=2))
        return {"out": str(out), "result": report, "image": str(out / name)}

    def _flash(self, family, params):
        patches = get_family(family).patches()
        prepared = patches.prepare_image(
            params["input"], params["start"], params["end"],
            simulator_mode=params.get("simulator_mode", False),
            correct_checksums=params.get("correct_checksum", False))
        metadata = prepared["metadata"]
        if params.get("dry_run"):
            return {"result": dict(metadata, status="validated", dry_run=True)}
        history = self._history(family)
        history["attempts"] += 1
        self._save_history(family, history)
        size = len(prepared["region"])
        for stage, percent, detail in (("CONNECTING", 5, "Opening exclusive diagnostic session"),
                                       ("SECURITY", 15, "Seed/key accepted"),
                                       ("PROGRAMMING", 22, "Entered programming session")):
            self.server.progress(stage, percent, detail)
            self._sleep(0.8)
        self._progress("ERASING", 25, 35, 1.5, lambda f: "Erasing selected regions", steps=5)
        speed = 3400.0
        seconds = min(12.0, max(3.0, size / speed / 4))
        self._transfer("WRITING", 35, 90, size, seconds)
        self.server.progress("VERIFYING", 93, "Checking transferred image checksum")
        self._sleep(1.0)
        self.server.progress("REBOOTING", 96, "Committing; fresh application verification required")
        self._sleep(1.5)
        image = bytearray(self.image(family))
        if len(image) == len(prepared["image"]):
            image[params["start"]:params["end"] + 1] = prepared["region"]
        self.image_path(family).write_bytes(bytes(image))
        history["successes"] += 1
        history["date"] = time.strftime("%Y-%m-%d")
        self._save_history(family, history)
        self.server.progress("COMPLETE", 100, "Transfer/checksum, commit and fresh application boot verified")
        return {"result": dict(metadata, status="ok", checksum_verified=True, boot_verified=True,
                               commit_outcome="acknowledged", recovery_required=False,
                               recovery_mode=params.get("recovery", False),
                               application=self.identity(family), demo=True)}

    def _diagnostic(self, operation, params):
        rng = random.Random()
        samples = int(params.get("samples") or 1)
        if operation == "faults":
            return {"status": "ok", "demo": True, "fault_count": 1, "faults": [
                {"slot": 0, "code": "00573", "text": "Terminal 30 voltage too low (demo)",
                 "snapshot": {"voltage_v": 10.8, "speed_kph": 0, "steering_angle_deg": -3.5}}]}
        if operation == "supply":
            rows = [{"terminal_30_v": round(rng.uniform(12.3, 12.7), 2),
                     "motor_supply_v": round(rng.uniform(12.1, 12.6), 2)} for _ in range(samples)]
        elif operation == "assist":
            rows = [{"assist_torque_nm": round(rng.uniform(-0.4, 0.4), 2),
                     "driver_torque_nm": round(rng.uniform(-0.2, 0.2), 2)} for _ in range(samples)]
        elif operation == "motion":
            rows = [{"steering_angle_deg": round(rng.uniform(-5, 5), 1),
                     "rack_position": rng.randrange(-40, 40)} for _ in range(samples)]
        else:
            rows = [{"hca_enabled": True, "gateway_seen": True, "messages": ["0x0D0", "0x3D0"]}]
        return {"status": "ok", "demo": True, "operation": operation, "samples": rows}
