"""Find CAN interfaces without opening them. Stdlib only; optional packages
are probed lazily and simply contribute nothing when missing."""
import importlib.util
import os
import struct
import sys
from pathlib import Path

# candleLight / gs_usb firmware USB IDs (CANable 2, candleLight, CANtact, ...).
GS_USB_IDS = {
    (0x1D50, 0x606F): "candleLight",
    (0x1209, 0x2323): "candleLight (pid.codes)",
    (0x1CD2, 0x606F): "CES CANext FD",
    (0x16D0, 0x10B8): "ABE CANdebugger FD",
}


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def backends() -> dict:
    return {
        "j2534": sys.platform == "win32",
        "j2534_native": sys.platform == "win32" and struct.calcsize("P") == 4,
        "python_can": has_module("can"),
        "gs_usb": has_module("can") and has_module("gs_usb") and has_module("usb"),
        "socketcan": sys.platform.startswith("linux") and has_module("can"),
        "panda": has_module("panda"),
    }


def j2534_devices() -> list:
    """Every registered PassThru 04.04 device, from both registry views."""
    if sys.platform != "win32":
        return []
    import winreg
    found = {}
    roots = (r"SOFTWARE\WOW6432Node\PassThruSupport.04.04", r"SOFTWARE\PassThruSupport.04.04")
    for root in roots:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as key:
                index = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(key, sub) as device:
                            dll, _ = winreg.QueryValueEx(device, "FunctionLibrary")
                            try:
                                name, _ = winreg.QueryValueEx(device, "Name")
                            except OSError:
                                name = sub
                    except OSError:
                        continue
                    if dll and dll.lower() not in found:
                        found[dll.lower()] = {
                            "adapter": "j2534", "dll": dll, "label": f"J2534 · {name}",
                            "present": os.path.isfile(dll),
                        }
        except OSError:
            continue
    return list(found.values())


def gs_usb_devices() -> list:
    if not (has_module("usb") and has_module("gs_usb")):
        return []
    try:
        import usb.core
        backend = None
        try:
            import libusb_package
            from usb.backend import libusb1
            backend = libusb1.get_backend(find_library=libusb_package.find_library)
        except ImportError:
            pass
        devices = list(usb.core.find(find_all=True, backend=backend,
                                     custom_match=lambda d: (d.idVendor, d.idProduct) in GS_USB_IDS))
    except Exception as exc:  # no libusb backend, permissions, ...
        return [{"adapter": "gs_usb", "channel": 0, "label": "gs_usb (scan failed)",
                 "present": False, "problem": str(exc)}]
    result = []
    for index, device in enumerate(devices):
        kind = GS_USB_IDS[(device.idVendor, device.idProduct)]
        result.append({"adapter": "gs_usb", "channel": index,
                       "label": f"gs_usb · {kind} #{index}", "present": True})
    return result


def socketcan_devices() -> list:
    if not sys.platform.startswith("linux"):
        return []
    result = []
    for entry in sorted(Path("/sys/class/net").glob("*")):
        try:
            if (entry / "type").read_text().strip() != "280":  # ARPHRD_CAN
                continue
            up = (entry / "operstate").read_text().strip() in ("up", "unknown")
        except OSError:
            continue
        result.append({"adapter": "socketcan", "channel": entry.name,
                       "label": f"SocketCAN · {entry.name}" + ("" if up else " (down)"),
                       "present": True, "up": up})
    return result


def panda_devices() -> list:
    if not has_module("panda"):
        return []
    try:
        from panda import Panda
        serials = Panda.list()
    except Exception as exc:
        return [{"adapter": "panda", "label": "Panda (scan failed)", "present": False,
                 "problem": str(exc)}]
    return [{"adapter": "panda", "serial": serial, "bus": 0,
             "label": f"Panda · {serial}", "present": True} for serial in serials]


def list_interfaces() -> list:
    return j2534_devices() + gs_usb_devices() + socketcan_devices() + panda_devices()
