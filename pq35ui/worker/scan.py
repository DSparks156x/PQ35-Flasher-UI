"""Ask every known module ID whether something answers, and what it is.

Read-only: first asks the gateway (TP2 0x1F) for its installation list, which
maps workshop addresses to TP2 IDs (AWD 22 -> 0x0A); then opens a TP2 channel
per supported module, reads the 0x9B/0x9C identification and closes it.
Every supported module is probed even if the gateway doesn't list it: the
list is the gateway's coding, not a presence check. Anything that opens a
channel is reported, even when it can't be identified or isn't what we
expect there.
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


def read_gateway(device, log) -> dict:
    """The gateway's installation list, or why there isn't one (bench, no gateway)."""
    try:
        from flasher.vag_protocols.installation_list import GATEWAY_ID, read_installation_list
        from flasher.vag_protocols.kwp import KWPNegativeResponse
    except ImportError:  # older flasher submodule
        return {"available": False, "entries": [], "error": "not supported by this flasher build"}
    try:
        tp = TP20Transport(device, module=GATEWAY_ID, timeout=1.0, log_fn=log)
    except Exception as exc:
        return {"available": False, "entries": [], "error": f"no gateway answered ({exc})"}
    try:
        kwp = KWPClient(tp, log_fn=log)
        try:
            entries = read_installation_list(kwp)
        except KWPNegativeResponse:
            kwp.session(0x89)
            entries = read_installation_list(kwp)
        return {"available": True, "entries": entries}
    except Exception as exc:
        return {"available": False, "entries": [], "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            tp.disconnect()
        except Exception:
            pass


def annotate(results, gateway):
    """Workshop address per module, and whether the gateway lists it at all."""
    by_tp2 = {e["tp2"]: e for e in gateway.get("entries") or ()}
    for entry in results:
        workshop = by_tp2.get(entry["module"])
        entry["workshop"] = workshop
        entry["in_gateway"] = bool(workshop) if gateway.get("available") else None
    return results


def scan(adapter: dict, progress, open_adapter):
    """(module results, gateway installation list)."""
    args = argparse.Namespace(
        adapter=adapter["adapter"], dll=adapter.get("dll"), channel=adapter.get("channel"),
        serial=adapter.get("serial"), bus=int(adapter.get("bus") or 0),
        baud=int(adapter.get("baud") or 500000))
    log = logging.getLogger("ModuleScan").info
    results = []
    device = open_adapter(args)
    try:
        progress("SCAN", 0, "Asking the gateway what's installed")
        gateway = read_gateway(device, log)
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
                proto = family.protocol()
                ident_hook = getattr(proto, "read_identification", None)
                if ident_hook is not None:
                    # Families with a non-1A identity (e.g. the service-22 BCM,
                    # which NRC-11s on 1A 9B/9C and would otherwise look "stuck")
                    # identify through their own hook on the open TP2 channel.
                    info = ident_hook(tp)
                    raw_ident = b""
                else:
                    kwp = KWPClient(tp, log_fn=log)
                    raw_ident = kwp.read_ecu_ident(0x9B)
                    raw_status = kwp.read_ecu_ident(0x9C)
                    info = parse_vag_identification(raw_ident, raw_status)
                decorate = getattr(proto, "decorate_identification", None)
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
    return annotate(results, gateway), gateway
