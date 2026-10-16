"""Offline image checks and flash-range planning. Never touches CAN."""
import hashlib
import re
from pathlib import Path

from flasher.controllers.registry import get_family

HALDEX = "haldex-gen4"
EPS = "pq-eps"

# (key, label, start, end, hint). Order is smallest-first within a family so
# the planner can pick the first preset that covers every changed byte.
PRESETS = {
    HALDEX: (
        ("tables", "Tuning tables only (B0F6)", 0x30000, 0x3FFFF,
         "Only the calibration block. Anti-brick patches are not re-sent."),
        ("full", "Full application", 0x18000, 0x4FFFF,
         "All four blocks, with the anti-brick patches."),
    ),
    EPS: (
        ("config", "0x5D configuration block", 0x5D000, 0x5DFFF,
         "Where the HCA patch and most settings live."),
        ("dataset", "0x5E steer dataset", 0x5E000, 0x5EFFF,
         "Steering assist dataset (firmware 3000 and newer)."),
        ("full", "Full firmware", 0x0A000, 0x5FFFF,
         "Everything. Needed when the current contents are unknown."),
    ),
}
FULL = {HALDEX: (0x18000, 0x4FFFF), EPS: (0x0A000, 0x5FFFF)}
SIZES = {0x50000: HALDEX, 0x60000: EPS, 0x1000: EPS}
HALDEX_BLOCKS = ((0x18000, 0x1FFFF), (0x20000, 0x2FFFF), (0x30000, 0x3FFFF), (0x40000, 0x4FFFF))


def _prepare(family: str, data: bytes, start: int, end: int, *, correct_checksum=False,
             simulator_mode=False) -> dict:
    patches = get_family(family).patches()
    return patches.prepare_image(data, start, end, simulator_mode=simulator_mode,
                                 correct_checksums=correct_checksum)


def _summary(metadata: dict) -> dict:
    keep = ("start_addr", "end_addr", "size", "checksum", "source_kind", "end_marker",
            "known_patches", "selection_policy", "simulator_mode", "bench_only",
            "correct_checksums", "input_checksum_mismatches", "dataset_crcs", "sectors")
    summary = {key: metadata[key] for key in keep if key in metadata}
    summary["patches"] = metadata.get("patches", [])
    summary["prepared_sha256"] = metadata.get("prepared_sha256", "").upper()
    return summary


def _part_numbers(data: bytes) -> list:
    found = re.findall(rb"(?:0BR|0AY|0BS|1K0|8J0|8P0)[0-9]{6}[A-Z]{0,2}", data)
    return sorted({part.decode() for part in found})


def guess_family(data: bytes):
    return SIZES.get(len(data))


def inspect_image(path: str, family: str = None, ecu_part: str = None) -> dict:
    """What is this file, is it intact, and does it belong to the connected ECU?"""
    data = Path(path).read_bytes()
    guessed = guess_family(data)
    result = {
        "path": str(Path(path).resolve()),
        "name": Path(path).name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest().upper(),
        "guessed_family": guessed,
        "part_numbers": _part_numbers(data),
        "checks": [],
    }
    checks = result["checks"]

    def check(key, ok, label, detail="", level=None):
        checks.append({"key": key, "ok": ok, "label": label, "detail": detail,
                       "level": level or ("ok" if ok else "error")})

    if guessed is None:
        check("size", False, "Unknown file size",
              f"{len(data)} bytes doesn't match any supported module image.")
        return result
    check("size", True, "Size OK", f"{len(data) // 1024} KiB")
    if family and guessed != family:
        check("family", False, "Wrong module",
              f"This looks like a {_family_label(guessed)} image, not {_family_label(family)}.")
        return result
    family = family or guessed

    start, end = FULL[family]
    if family == EPS and len(data) == 0x1000:
        start, end = 0x5E000, 0x5EFFF
    try:
        prepared = _prepare(family, data, start, end)
        check("checksums", True, "Internal checksums valid")
        result["metadata"] = _summary(prepared["metadata"])
    except ValueError as exc:
        message = str(exc)
        if "checksum" in message.lower() or "crc" in message.lower():
            check("checksums", False, "Internal checksums wrong", message)
            try:
                prepared = _prepare(family, data, start, end, correct_checksum=True)
                result["metadata"] = _summary(prepared["metadata"])
                result["correctable"] = True
            except ValueError:
                pass
        else:
            check("checksums", False, "Image rejected", message)
    if family == EPS:
        known = (result.get("metadata") or {}).get("known_patches") or {}
        for name, status in known.items():
            if status not in ("stock", "mismatch"):
                check("patch:" + name, True, f"Patch: {name}", status, level="info")

    if ecu_part:
        parts = result["part_numbers"]
        bare = ecu_part.replace(" ", "")
        if any(bare.startswith(part) or part.startswith(bare[:10]) for part in parts):
            check("ecu", True, "Matches connected ECU", ecu_part)
        elif parts:
            check("ecu", False, "Different part number",
                  f"Image says {', '.join(parts)}; connected ECU is {ecu_part}.", level="warning")
        else:
            check("ecu", False, "Part number not found in image",
                  "Can't confirm this image belongs to the connected ECU.", level="warning")
    return result


def _family_label(family: str) -> str:
    return {HALDEX: "Haldex Gen4", EPS: "EPS rack"}.get(family, family)


def _changed_ranges(new: bytes, old: bytes, low: int, high: int) -> list:
    ranges = []
    index = low
    while index <= high:
        if new[index] == old[index]:
            index += 1
            continue
        first = index
        while index <= high and new[index] != old[index]:
            index += 1
        ranges.append((first, index - 1))
    return ranges


def plan_flash(path: str, family: str, reference: str = None, correct_checksum=False,
               simulator_mode=False) -> dict:
    """List flash targets, validate each, and recommend the smallest whole
    range that covers every byte that differs from what the ECU holds now.
    Without a trustworthy reference the recommendation is the full range."""
    data = Path(path).read_bytes()
    options = []
    presets = PRESETS[family]
    if family == EPS and len(data) == 0x1000:
        presets = tuple(p for p in presets if p[0] == "dataset")
    for key, label, start, end, hint in presets:
        option = {"key": key, "label": label, "start": start, "end": end, "hint": hint,
                  "size": end - start + 1}
        try:
            prepared = _prepare(family, data, start, end, correct_checksum=correct_checksum,
                                simulator_mode=simulator_mode)
            option.update(valid=True, metadata=_summary(prepared["metadata"]))
        except ValueError as exc:
            option.update(valid=False, reason=str(exc))
        options.append(option)

    plan = {"family": family, "options": options, "reference": None, "changes": None}
    recommended = next((o for o in reversed(options) if o["valid"]), None)
    reason = "No backup of this ECU to compare against, so everything gets written."

    ref_data = Path(reference).read_bytes() if reference and Path(reference).is_file() else None
    if ref_data is not None and len(ref_data) == len(data) and len(data) != 0x1000:
        low, high = FULL[family]
        full = next(o for o in options if o["key"] == "full")
        # Compare what would actually be sent (patched, checksummed) to the backup.
        new = data
        if full["valid"]:
            new = _prepare(family, data, low, high, correct_checksum=correct_checksum,
                           simulator_mode=simulator_mode)["image"]
        changes = _changed_ranges(new, ref_data, low, high)
        plan["reference"] = str(reference)
        plan["changes"] = [{"start": a, "end": b} for a, b in changes]
        if not changes:
            recommended = None
            reason = "This image is identical to your latest backup. Nothing to flash."
        else:
            first, last = changes[0][0], changes[-1][1]
            if family == HALDEX:
                # Smallest contiguous whole-block span (Layer-1 words are per block).
                lo = max(a for a, _ in HALDEX_BLOCKS if a <= first)
                hi = min(b for _, b in HALDEX_BLOCKS if b >= last)
                if not any(o["start"] == lo and o["end"] == hi for o in options):
                    option = {"key": "minimal", "label": "Changed blocks only", "start": lo,
                              "end": hi, "size": hi - lo + 1,
                              "hint": "Just the blocks that differ from your backup."}
                    try:
                        prepared = _prepare(family, data, lo, hi,
                                            correct_checksum=correct_checksum,
                                            simulator_mode=simulator_mode)
                        option.update(valid=True, metadata=_summary(prepared["metadata"]))
                    except ValueError as exc:
                        option.update(valid=False, reason=str(exc))
                    options.insert(0, option)
            for option in options:
                if option["valid"] and not (option["start"] <= first and last <= option["end"]):
                    option.update(valid=False, reason=(
                        "Doesn't cover every change "
                        f"(changes span 0x{first:05X}-0x{last:05X})."))
            covering = [o for o in options if o["valid"]]
            if covering:
                recommended = min(covering, key=lambda o: o["size"])
                total = sum(b - a + 1 for a, b in changes)
                reason = (f"{total} byte(s) differ from your backup, all inside "
                          f"0x{recommended['start']:05X}-0x{recommended['end']:05X}.")
    elif ref_data is not None:
        reason = "Your backup is a different size from this image, so everything gets written."
    elif family == EPS and len(data) == 0x1000:
        recommended = options[0] if options and options[0]["valid"] else None
        reason = "A 4 KiB dataset file can only go to the 0x5E block."

    plan["recommended"] = recommended["key"] if recommended else None
    plan["reason"] = reason
    return plan


def eps_patch(path: str, feature: str, out: str) -> dict:
    patches = get_family(EPS).patches()
    data = Path(path).read_bytes()
    patches.validate_image(data)
    patched = patches.require_patch_manifest(data, feature).apply(data)
    Path(out).write_bytes(patched)
    return {"out": str(Path(out).resolve()), "sha256": hashlib.sha256(patched).hexdigest().upper(),
            "known_patches": patches.known_patches(patched)}


def eps_patch_status(path: str) -> dict:
    patches = get_family(EPS).patches()
    data = Path(path).read_bytes()
    return {"features": list(patches.FEATURES), "known_patches": patches.known_patches(data)}


def compose_state(path: str, family: str, start: int, end: int, out: str, reference: str = None,
                  correct_checksum=False, simulator_mode=False) -> dict:
    """After a verified flash: the ECU now holds the previous state with the
    flashed range replaced. Without a previous state only a full flash tells
    us what is on the ECU."""
    data = Path(path).read_bytes()
    prepared = _prepare(family, data, start, end, correct_checksum=correct_checksum,
                        simulator_mode=simulator_mode)
    region = prepared["region"]
    if family == EPS and len(data) == 0x1000:
        if not (reference and Path(reference).is_file()):
            return {"written": False, "reason": "No previous state to place the dataset into"}
        base = bytearray(Path(reference).read_bytes())
    elif reference and Path(reference).is_file() and Path(reference).stat().st_size == len(data):
        base = bytearray(Path(reference).read_bytes())
    elif (start, end) == FULL[family]:
        base = bytearray(prepared["image"])
    else:
        return {"written": False, "reason": "Partial flash without a previous state"}
    base[start:end + 1] = region
    Path(out).write_bytes(bytes(base))
    return {"written": True, "out": str(out), "sha256": hashlib.sha256(base).hexdigest().upper()}
