"""Ask every known module ID whether something answers, and what it is.

Read-only: opens a TP2 channel per module, reads the 0x9B/0x9C
identification, closes the channel. Anything that opens a channel is
reported, even when it can't be identified or isn't what we expect there.
"""
import argparse
import logging
import re

from flasher.controllers.registry import FAMILIES
from flasher.engine import parse_vag_identification
from flasher.vag_protocols.kwp import KWPClient
from flasher.vag_protocols.tp2 import TP20Transport

# Application channels answer on these TP2 addresses; anything else after a
# successful channel setup means a loader is talking (see HaldexFlasher._ident).
HALDEX_APP_TX = 0x764


def _in_bootloader(family, info, tp, raw_ident: bytes) -> bool:
    if family.name == "haldex-gen4":
        return tp.tx_addr != HALDEX_APP_TX or b"B_111545" in raw_ident[2:]
    if family.name == "pq-eps":
        return "EPS_ZFLS BB" in str(info.get("system_desc", "")).upper()
    return False


def _real_application(info) -> bool:
    """A proper VAG part number plus a version: some other controller's application."""
    part = str(info.get("software_part_number") or info.get("part_number") or "").strip()
    version = str(info.get("sw_version", "")).strip()
    return bool(re.fullmatch(r"[0-9][A-Z0-9]{2}[0-9]{6}[A-Z]{0,3}", part)) and bool(version)


def scan(adapter: dict, progress, open_adapter) -> list:
    args = argparse.Namespace(
        adapter=adapter["adapter"], dll=adapter.get("dll"), channel=adapter.get("channel"),
        serial=adapter.get("serial"), bus=int(adapter.get("bus") or 0),
        baud=int(adapter.get("baud") or 500000))
    log = logging.getLogger("ModuleScan").info
    results = []
    device = open_adapter(args)
    try:
        for index, family in enumerate(FAMILIES):
            progress("SCAN", 100 * index / len(FAMILIES),
                     f"Looking for {family.name} at 0x{family.module:02X}")
            entry = {"family": family.name, "module": family.module, "status": "absent"}
            results.append(entry)
            try:
                tp = TP20Transport(device, module=family.module, timeout=1.0, log_fn=log)
            except Exception as exc:  # no channel setup reply: nothing at this ID
                entry["detail"] = str(exc)
                continue
            entry.update(status="responding", tp_tx=tp.tx_addr, tp_rx=tp.rx_addr)
            try:
                kwp = KWPClient(tp, log_fn=log)
                raw_ident = kwp.read_ecu_ident(0x9B)
                raw_status = kwp.read_ecu_ident(0x9C)
                info = parse_vag_identification(raw_ident, raw_status)
                decorate = getattr(family.protocol(), "decorate_identification", None)
                if decorate is not None:
                    info = decorate(info)
                info["in_bootloader"] = _in_bootloader(family, info, tp, raw_ident)
                info.update(connected=True, module=family.module, controller_family=family.name)
                entry["identity"] = info
                if info["in_bootloader"]:
                    entry["status"] = "bootloader"
                elif family.matches_identification(info) and str(info.get("sw_version", "")).strip():
                    entry["status"] = "ok"
                elif _real_application(info):
                    entry["status"] = "unexpected"
                else:
                    # Loader markers vary; a half-answer at a module address is
                    # treated as "stuck" so recovery is offered.
                    entry["status"] = "bootloader"
            except Exception as exc:
                # Opened a channel but wouldn't identify: typically a loader.
                entry["detail"] = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    tp.disconnect()
                except Exception:
                    pass
        progress("SCAN", 100, "Done")
    finally:
        device.close()
    return results
