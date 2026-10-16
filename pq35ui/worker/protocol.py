"""Wire format shared by the UI and the worker: one JSON object per line.

Requests:  {"id": 7, "method": "inspect_image", "params": {...}}
Responses: {"id": 7, "result": ...} or {"id": 7, "error": {"type": ..., "message": ...}}
Events:    {"event": "progress" | "log" | "busy", ...}   (no id)

This module is stdlib-only: the J2534 worker runs under a bare 32-bit Python.
"""
import json

PROTOCOL_VERSION = 1


def encode(message: dict) -> bytes:
    return (json.dumps(message, separators=(",", ":"), default=_default) + "\n").encode("utf-8")


def _default(value):
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def json_objects(text: str) -> list:
    """Every top-level JSON object printed into ``text`` (flasher prints its
    plans and results as indented JSON between ordinary lines)."""
    decoder = json.JSONDecoder()
    found = []
    index = 0
    while True:
        index = text.find("{", index)
        if index < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        if isinstance(value, dict):
            found.append(value)
        index = end
