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
    BCM: ("8P0907063E", "0575", "Bordnetz-SG"),
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
        if family == BCM:
            # Full-span 0x780000..0x7FFFFF (0x80000), boot-block padded; part string embedded.
            data = bytearray(b"\xff" * 0x80000)
            data[0:15] = b"8P0907063E 0040"
            return bytes(data)
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

    def scan(self) -> list:
        """PQ35_DEMO_BOOTLOADER=awd,eps pretends those modules are stuck in their loader."""
        from flasher.controllers.registry import FAMILIES
        stuck = {a.strip() for a in os.environ.get("PQ35_DEMO_BOOTLOADER", "").split(",") if a.strip()}
        results = []
        for index, family in enumerate(FAMILIES):
            self.server.progress("SCAN", 100 * index / len(FAMILIES),
                                 f"Looking for {family.name} at 0x{family.module:02X}")
            self._sleep(0.5)
            entry = {"family": family.name, "module": family.module, "status": "absent"}
            if family.name == BCM:
                results.append(entry)
                continue
            identity = self.identity(family.name)
            if stuck & set(family.module_aliases):
                identity = dict(identity, in_bootloader=True, sw_version="",
                                system_desc="Bootloader (demo)")
                entry.update(status="bootloader", identity=identity)
            else:
                identity["in_bootloader"] = False
                entry.update(status="ok", identity=identity)
            results.append(entry)
        self.server.progress("SCAN", 100, "Done")
        return results

    def run(self, params) -> dict:
        if params["operation"] == "scan":
            self.log("DEMO scan: no CAN traffic")
            return {"operation": "scan", "exit_code": 0, "demo": True, "result": self.scan()}
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
        elif operation == "pageff":
            result.update(self._pageff(family, params))
        elif operation == "preconditions":
            result["result"] = self._preconditions(family, params)
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
        if family == BCM:
            return self._flash_bcm(family, params)
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
        if os.environ.get("PQ35_DEMO_AMBIGUOUS"):
            # Like a real bench Haldex: booted, but 0x82 StopCommunication unanswered.
            return {"error": "RuntimeError: Application boot observed but commit outcome remains ambiguous",
                    "result": dict(metadata, status="recovery_required", checksum_verified=True,
                                   boot_verified=True, commit_outcome="ambiguous",
                                   commit_responses={"20": "acknowledged", "82": "Timed out"},
                                   recovery_required=True, application=self.identity(family),
                                   demo=True)}
        self.server.progress("COMPLETE", 100, "Transfer/checksum, commit and fresh application boot verified")
        return {"result": dict(metadata, status="ok", checksum_verified=True, boot_verified=True,
                               commit_outcome="acknowledged", recovery_required=False,
                               recovery_mode=params.get("recovery", False),
                               application=self.identity(family), demo=True)}

    def _flash_bcm(self, family, params):
        """Pretend BCM flash: by 16 KiB sector, no patches module. Mirrors the real
        multi-segment path (one session, a cycle per sector-run)."""
        from flasher.controllers.pq_bcm import flash as bcm_flash
        if params.get("sectors"):
            segments = bcm_flash.plan_sectors(params["input"], list(params["sectors"]))
        else:
            segments = [bcm_flash.prepare_image(params["input"], params["start"], params["end"])]
        meta = dict(segments[0]["metadata"])
        plan = [{"start_addr": s["metadata"]["start_addr"], "end_addr": s["metadata"]["end_addr"],
                 "size": s["metadata"]["size"], "checksum": s["metadata"]["checksum"]}
                for s in segments]
        meta["segments"] = plan
        if params.get("dry_run"):
            return {"result": dict(meta, status="validated", dry_run=True)}
        history = self._history(family)
        history["attempts"] += 1
        self._save_history(family, history)
        for stage, percent, detail in (("CONNECTING", 5, "Opening exclusive diagnostic session"),
                                       ("SECURITY", 15, "Seed/key accepted (27 01/02)"),
                                       ("PROGRAMMING", 22, "Resident loader ready")):
            self.server.progress(stage, percent, detail)
            self._sleep(0.6)
        image = bytearray(self.image(family))
        for index, seg in enumerate(segments):
            md = seg["metadata"]
            lo = 25 + 65 * index // len(segments)
            hi = 25 + 65 * (index + 1) // len(segments)
            self.server.progress("ERASING", lo,
                                 f"Erasing 0x{md['start_addr']:06X}..0x{md['end_addr']:06X}")
            self._sleep(0.4)
            self._transfer("WRITING", lo, hi, md["size"], min(6.0, max(1.0, md["size"] / 3400 / 4)))
            off = md["start_addr"] - bcm_flash.BCM_IMAGE_BASE
            if 0 <= off and off + md["size"] <= len(image):
                image[off:off + md["size"]] = seg["region"]
        self.server.progress("VERIFYING", 93, "Checking transferred sector checksums (31 C5)")
        self._sleep(0.8)
        self.server.progress("REBOOTING", 96, "Committing; marker cleared, booting application")
        self._sleep(1.0)
        self.image_path(family).write_bytes(bytes(image))
        history["successes"] += 1
        history["date"] = time.strftime("%Y-%m-%d")
        self._save_history(family, history)
        self.server.progress("COMPLETE", 100, "Sectors, checksum, commit and application boot verified")
        return {"result": dict(meta, status="ok", checksum_verified=True, boot_verified=True,
                               commit_outcome="acknowledged", recovery_required=False,
                               recovery_mode=params.get("recovery", False),
                               application=self.identity(family), demo=True)}

    def _pageff(self, family, params):
        """Like --read-bcm-pageff: 256 uploads of 0x40 bytes, then a .bin and a sidecar."""
        identity = self.identity(family)
        out = Path(params.get("out_dir") or self.root)
        out.mkdir(parents=True, exist_ok=True)
        data = bytearray()
        for chunk in range(1, 257):
            data += bytes(random.Random(chunk).randrange(256) for _ in range(0x40))
            self.server.progress("PAGE FF", 100 * chunk / 256, f"chunk {chunk}/256")
            self._sleep(0.02)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        image = out / f"{identity['software_part_number']}_{identity['sw_version']}_pageFF_{stamp}.bin"
        image.write_bytes(bytes(data))
        sidecar = image.with_suffix(".json")
        report = {"operation": "read_bcm_pageff", "status": "ok", "start": "0x7FC000",
                  "end": "0x7FFFFF", "size": len(data), "chunks": 256,
                  "sha256": hashlib.sha256(data).hexdigest().upper(), "demo": True}
        sidecar.write_text(json.dumps(report, indent=2))
        return {"result": report, "image": str(image), "sidecar": str(sidecar), "out": str(out)}

    def _preconditions(self, family, params):
        """Like --bcm-preconditions: read-only check of the four gates a BCM flash's
        10 85 needs. PQ35_DEMO_PRECOND_FAIL=1 returns a blocked (low-supply) set so the
        UI's red path can be demoed."""
        scaling_note = ("voltage scaling (mV) is inferred, not read from the module; treat "
                        "the volts as approximate.")
        if os.environ.get("PQ35_DEMO_PRECOND_FAIL"):
            return {"supply_raw": 11800, "supply_volts": 11.8, "supply_ok": False,
                    "attempt_counter": 0, "counter_ok": True,
                    "flag_2d09": 0, "flag_2d09_ok": True, "flag_0068": 0, "flag_0068_ok": True,
                    "all_ok": False, "blocking": ["supply 11.80 V (needs >= 12.00 V)"],
                    "scaling_note": scaling_note, "demo": True}
        return {"supply_raw": 13200, "supply_volts": 13.2, "supply_ok": True,
                "attempt_counter": 0, "counter_ok": True,
                "flag_2d09": 0, "flag_2d09_ok": True, "flag_0068": 0, "flag_0068_ok": True,
                "all_ok": True, "blocking": [], "scaling_note": scaling_note, "demo": True}

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
