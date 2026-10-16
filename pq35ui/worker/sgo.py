"""Convert an EPS SGO (SGML Object File) into a 384 KiB CPU-linear image.

Container layout and decoding follow unpack_firmware_sgos.py from
pq-flasher (MIT License, Copyright (c) 2021 Willem Melching): a header,
a metadata block, then sections of (24-bit address, 24-bit length, payload)
whose payload is XORed with 0xFF and optionally run-length coded ("BCB").
Areas no section covers stay 0xFF, exactly as that converter produces.

Only PQ35 EPS racks are accepted. Other controllers' SGOs (for example
5N1909144 PQ46 racks, whose sections lie above 0x60000) are refused.
"""
import re
import struct
from pathlib import Path

MAGIC = b"SGML Object File"
EPS_IMAGE_SIZE = 0x60000
PQ35_EPS_PART = re.compile(rb"(?:1K0|8J0|1K1|1T0|3C0)909144[A-Z]{0,2}")


def is_sgo(path) -> bool:
    try:
        with open(path, "rb") as stream:
            return stream.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def _w32(data, pos):
    return struct.unpack_from("<I", data, pos)[0]


def _w24(data, pos):
    return int.from_bytes(data[pos:pos + 3], "big")


def _bcb(payload: bytes, length: int) -> bytes:
    start = payload.find(b"\x1A\x01")
    if start < 0:
        return payload[:length]
    pos = start + 2
    out = bytearray()
    while pos + 2 <= len(payload):
        word = struct.unpack_from(">H", payload, pos)[0]
        pos += 2
        kind, count = word >> 14, word & 0x3FFF
        if kind == 0:  # literal run
            out += payload[pos:pos + count]
            pos += count
        elif kind == 1:  # repeated byte
            out += payload[pos:pos + 1] * count
            pos += 1
        elif kind == 3:  # end
            break
    return bytes(out[:length])


def sections(data: bytes) -> list:
    if not data.startswith(MAGIC):
        raise ValueError("Not an SGO file")
    meta_start = _w32(data, 0x29)
    total = _w32(data, 0x2D)
    pos = meta_start + _w32(data, meta_start) + 4
    result = []
    while pos < min(total, len(data)):
        address, length, packed = _w24(data, pos), _w24(data, pos + 4), data[pos + 3]
        size = _w32(data, pos + 0x15)
        blob = bytes(b ^ 0xFF for b in data[pos + 0x19:pos + 0x19 + size])
        result.append((address, _bcb(blob, length) if packed == 1 else blob[:length]))
        pos += 0x19 + size
        if len(result) > 512:
            raise ValueError("Implausible SGO: too many sections")
    return result


def header_name(data: bytes) -> str:
    text = bytes(b ^ 0xFF for b in data[:0x200])
    names = re.findall(rb"[\x20-\x7e]{6,}", text)
    return names[0].decode() if names else ""


def convert_eps(path: str, out: str) -> dict:
    data = Path(path).read_bytes()
    parts = sections(data)
    image = bytearray(b"\xFF" * EPS_IMAGE_SIZE)
    for address, payload in parts:
        if address + len(payload) > EPS_IMAGE_SIZE:
            raise ValueError(
                f"This SGO writes to 0x{address:06X}, outside a PQ35 EPS rack's 384 KiB. "
                "It's for a different controller.")
        image[address:address + len(payload)] = payload
    part = PQ35_EPS_PART.search(bytes(image))
    if not part:
        raise ValueError("This SGO doesn't contain a PQ35 EPS rack (1K0/8J0 909 144) firmware.")
    Path(out).write_bytes(bytes(image))
    return {"out": str(Path(out).resolve()), "sections": len(parts),
            "source_name": header_name(data), "part": part.group().decode()}
