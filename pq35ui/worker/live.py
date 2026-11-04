"""Live measuring-block logging and fault codes for the UI.

Both run as ordinary hardware jobs (one at a time, under the hardware lock).
Logging streams one {"event": "sample", ...} per group read until the UI
sends "stop"; it never writes to the module. Reading fault codes is
read-only; clearing them (KWP 14 FF 00) is the one write here.
"""
import argparse
import logging


def _args(adapter: dict):
    return argparse.Namespace(
        adapter=adapter["adapter"], dll=adapter.get("dll"), channel=adapter.get("channel"),
        serial=adapter.get("serial"), bus=int(adapter.get("bus") or 0),
        baud=int(adapter.get("baud") or 500000))


def _family(params):
    from flasher.controllers.registry import family_for_module, parse_module
    return family_for_module(parse_module(params["module"]))


def run_log(server, params, open_adapter, should_stop) -> dict:
    from flasher.vag_protocols.measuring import MeasuringSession
    family = _family(params)
    groups = sorted({int(g) for g in params.get("groups") or ()})
    if not groups:
        raise ValueError("Choose at least one value to log.")
    log = logging.getLogger("Logger").info

    def on_sample(sample):
        server.emit(dict(sample, event="sample", module=family.module))

    device = open_adapter(_args(params["adapter"]))
    try:
        session = MeasuringSession(device, family.module, groups,
                                   interval_ms=int(params.get("interval_ms") or 0),
                                   on_sample=on_sample, should_stop=should_stop, log_fn=log)
        summary = session.run()
    finally:
        device.close()
    return {"operation": "log", "exit_code": 0, "result": summary}


def run_dtc(server, params, open_adapter, clear=False) -> dict:
    from flasher.vag_protocols import dtc
    from flasher.vag_protocols.kwp import KWPClient
    from flasher.vag_protocols.tp2 import TP20Transport
    family = _family(params)
    log = logging.getLogger("FaultCodes").info
    device = open_adapter(_args(params["adapter"]))
    tp = None
    try:
        tp = TP20Transport(device, module=family.module, timeout=2.0, log_fn=log)
        kwp = KWPClient(tp, log_fn=log)
        try:
            kwp.session(0x89)  # diagnostic session; some modules answer fine without it
        except Exception as exc:
            log(f"Diagnostic session 0x89 not accepted ({exc}); continuing in the default one")
        result = {}
        if clear:
            result["clear"] = dtc.clear_dtcs(kwp)
        result.update(dtc.read_dtcs(kwp))
    finally:
        if tp is not None:
            try:
                tp.disconnect()
            except Exception:
                pass
        device.close()
    return {"operation": "dtc_clear" if clear else "dtc", "exit_code": 0, "result": result}
