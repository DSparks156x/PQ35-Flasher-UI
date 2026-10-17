"""Offline image checks and flash-range planning. Never touches CAN."""
import hashlib
import re
from pathlib import Path

from flasher.controllers.registry import get_family

HALDEX = "haldex-gen4"
EPS = "pq-eps"
BCM = "pq-bcm"

# (key, label, start, end, hint).
PRESETS = {
    HALDEX: (
        ("tables", "Tuning tables only (B0F6)", 0x30000, 0x3FFFF,
         "Only the calibration block. Anti-brick patches are not re-sent."),
        ("full", "Full application", 0x18000, 0x4FFFF,
         "All four blocks, with the anti-brick patches."),
    ),
    EPS: (  # in order of preference; the first one allowed is the default
        ("dataset", "0x5E steer dataset", 0x5E000, 0x5EFFF,
         "Steering assist dataset only."),
        ("config", "0x5D configuration block", 0x5D000, 0x5DFFF,
         "Where the HCA patch and most settings live."),
        ("full", "Full firmware", 0x0A000, 0x5FFFF,
         "Firmware, configuration and dataset."),
    ),
}
FULL = {HALDEX: (0x18000, 0x4FFFF), EPS: (0x0A000, 0x5FFFF), BCM: (0x780000, 0x7FBFFF)}
SIZES = {0x50000: HALDEX, 0x60000: EPS, 0x1000: EPS, 0x80000: BCM}
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


DATASET_CRC_MARKER = bytes.fromhex("78563412")


def inspect_image(path: str, family: str = None, ecu_part: str = None,
                  ecu_version: str = None) -> dict:
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

    if family == BCM:
        return _inspect_bcm(result, data, check, ecu_part)

    dataset_file = family == EPS and len(data) == 0x1000
    if dataset_file and data[-8:-4] == DATASET_CRC_MARKER:
        # Gen2 layout: the CRC marker ends the 0x5E block. Those racks (below
        # 3000) don't take 4 KiB datasets.
        check("dataset", False, "Dataset for firmware below 3000",
              "Gen2 steer datasets aren't flashed as a 4 KiB block. Datasets work on racks "
              "with firmware 3000 or newer.")
        return result
    start, end = FULL[family]
    if dataset_file:
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
        firmware = eps_firmware(data)
        ids = eps_dataset_ids(data)
        if firmware:
            result["firmware"] = {"part": firmware[0], "version": firmware[1]}
            check("firmware", True, f"Firmware {firmware[1]}", firmware[0], level="info")
        if ids:
            result["dataset_ids"] = ids
            if all(i == "000" for i in ids):
                check("dataset", True, "Placeholder dataset 000",
                      "Factory firmware without a real steer dataset.", level="warning")
            else:
                check("dataset", True, f"Dataset {ids[0]}", "", level="info")
        known = (result.get("metadata") or {}).get("known_patches") or {}
        for name, status in known.items():
            if status not in ("stock", "mismatch"):
                label = f"Patch: {name}" + ("" if status == "applied" else f" ({status})")
                check("patch:" + name, True, label, status, level="info")

    if dataset_file:
        # A dataset carries no part number; what matters is the rack's firmware.
        module = _version(ecu_version)
        if module is not None and module >= 3000:
            check("ecu", True, f"Fits this rack (firmware {module})", "Datasets need 3000+.")
        elif module is not None:
            check("ecu", False, "Needs firmware 3000 or newer",
                  f"This rack runs {module}; 0x5E datasets need 3000 or newer.")
        return result

    if ecu_part:
        _part_check(check, family, data, result["part_numbers"], ecu_part)
    return result


def _base(part: str) -> str:
    """'1K0909144R' -> '1K0909144': the hardware part without the version suffix."""
    return part[:9]


def _part_check(check, family, data, found, ecu_part):
    """Same base part number: fine (a suffix is just another version).
    Different base: a crossflash, which needs further steps and is blocked unless
    explicitly allowed on the Flash page."""
    ecu = ecu_part.replace(" ", "").upper()
    image = None
    if family == EPS:
        firmware = eps_firmware(data)  # the identity in the 0x5D block, not code strings
        image = firmware[0] if firmware else None
    if image is None:
        candidates = [p for p in found if _base(p) == _base(ecu)] or found
        image = candidates[0] if candidates else None
    if image is None:
        check("ecu", False, "Part number not found in image",
              "Can't confirm this image belongs to the connected ECU.", level="warning")
    elif image == ecu:
        check("ecu", True, "Matches connected ECU", ecu)
    elif _base(image) == _base(ecu):
        check("ecu", True, f"Same part, version {image[9:] or '-'} (ECU {ecu[9:] or '-'})",
              f"Image is {image}, the connected ECU is {ecu}. Same hardware, different "
              "software version.")
    else:
        check("crossflash", False, f"Crossflash: {image} onto {ecu}",
              f"This image is for {image}; the connected ECU is {ecu}. Flashing a different "
              "part number needs further steps and is blocked unless you allow a crossflash "
              "under advanced options.")


def _family_label(family: str) -> str:
    return {HALDEX: "Haldex Gen4", EPS: "EPS rack", BCM: "BCM"}.get(family, family)


def _bcm():
    from flasher.controllers.pq_bcm import flash as bcm_flash
    return bcm_flash


def _bcm_sectors_present(bcm, data: bytes) -> list:
    """Sector starts in the full-span image that hold non-blank (non-0xFF) data."""
    present = []
    for start in bcm.pflash_sector_starts():
        off = start - bcm.BCM_IMAGE_BASE
        if any(b != 0xFF for b in data[off:off + bcm.PFLASH_SECTOR]):
            present.append(start)
    return present


def _inspect_bcm(result, data, check, ecu_part) -> dict:
    """BCM images are full-span (0x780000..0x7FFFFF, 0x80000 bytes, boot-block
    padded). We can validate size/layout and the part number, but not recompute
    the module's CRC32 table here; the patch tooling owns that."""
    bcm = _bcm()
    present = _bcm_sectors_present(bcm, data)
    result["metadata"] = {
        "start_addr": bcm.PFLASH_REGION[0], "end_addr": bcm.PFLASH_REGION[1],
        "size": bcm.PFLASH_REGION[1] - bcm.PFLASH_REGION[0] + 1,
        "image_base": bcm.BCM_IMAGE_BASE, "image_size": bcm.BCM_IMAGE_SIZE,
        "present_sectors": ["0x%06X" % s for s in present],
        "sector_count": len(bcm.pflash_sector_starts()),
    }
    if present:
        check("sectors", True, f"{len(present)} programmable sector(s) with data",
              "Only the application P-Flash is written; the boot block is never touched.",
              level="info")
    parts = result["part_numbers"]
    if ecu_part:
        want = ecu_part.replace(" ", "").upper()
        if any(p.replace(" ", "").upper().startswith(want[:10]) for p in parts):
            check("ecu", True, "Matches connected BCM", ecu_part)
        elif parts:
            check("ecu", False, f"Image part {parts[0]} vs ECU {ecu_part}",
                  "Different part number; confirm this image belongs to this BCM.",
                  level="warning")
    return result


def _plan_bcm(data, reference) -> dict:
    """Plan a BCM flash by sector. The recommended option is "changed sectors
    only" when a reference (prior full-span image / backup) is supplied; the
    full application (all 31 sectors) is always offered. The boot block is never
    programmed."""
    bcm = _bcm()
    options = []
    notes = ["Only application P-Flash sectors are written. The boot block "
             "(0x7FC000..0x7FFFFF) is protected by the loader and never touched."]
    changes = None
    reference_path = None

    ref = Path(reference).read_bytes() if reference and Path(reference).is_file() else None
    if ref is not None and len(ref) == len(data):
        reference_path = str(reference)
        changed = bcm.changed_sectors(data, ref, base=bcm.BCM_IMAGE_BASE)
        changes = [{"start": s, "end": s + bcm.PFLASH_SECTOR - 1} for s in changed]
        if changed:
            lo, hi = changed[0], changed[-1] + bcm.PFLASH_SECTOR - 1
            options.append({
                "key": "changed", "label": "Changed sectors only",
                "start": lo, "end": hi, "size": len(changed) * bcm.PFLASH_SECTOR,
                "hint": f"Just the {len(changed)} sector(s) that differ from your backup.",
                "valid": True, "sectors": list(changed),
                "metadata": {"sectors": ["0x%06X" % s for s in changed]}})

    # We never flash padding. The boot block is refused outright; blank (all-0xFF)
    # sectors are never written (that would just erase the module). Normal use
    # writes only the pages the patch changed. "Every data sector" exists only to
    # restore a blank/bricked module from a known-good full image, and is never
    # recommended because it rewrites unchanged pages too.
    present = _bcm_sectors_present(bcm, data)
    recovery = {
        "key": "recovery", "label": f"Full recovery — every data sector ({len(present)})",
        "start": present[0] if present else bcm.PFLASH_REGION[0],
        "end": (present[-1] + bcm.PFLASH_SECTOR - 1) if present else bcm.PFLASH_REGION[1],
        "size": len(present) * bcm.PFLASH_SECTOR,
        "hint": "Only for restoring a blank/bricked module from a known-good full image. "
                "Rewrites unchanged pages too; blank padding and the boot block are still skipped.",
        "valid": bool(present), "sectors": present, "recovery_only": True,
        "metadata": {"sectors": [f"0x{s:06X}" for s in present]}}

    if any(o["key"] == "changed" for o in options):
        recommended, reason = "changed", (
            f"Writes only the {len(changes)} page(s) that differ from the stock reference.")
        options.append(recovery)
    elif changes == []:
        recommended, reason = None, "This image is identical to the reference; flashing changes nothing."
        recovery.update(valid=False, reason="No sectors differ from the reference.")
        options.append(recovery)
    else:
        # No reference: never auto-pick a whole-image write. Patched pages only.
        recommended = None
        reason = ("Choose the stock full-span image the BCM currently runs as the reference, and "
                  "only the pages your patch changed are written. No reference? Pick the patched "
                  "pages yourself with the command line (--bcm-sector). Padding is never flashed.")
        options.append(recovery)
        notes.append("The BCM can't be read back, so there's no on-module diff. Normal flashing "
                     "writes only changed pages; 'Full recovery' is just for a blank module.")
    return {"family": BCM, "options": options, "reference": reference_path,
            "changes": changes, "recommended": recommended, "reason": reason,
            "notes": notes, "needs_dataset": False}


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


def known_patches(data: bytes):
    if len(data) != 0x60000:
        return {}
    return get_family(EPS).patches().known_patches(data)


def eps_firmware(data: bytes):
    """(part, version) a full EPS image was built for, from the identity string
    in its 0x5D block (code region as a fallback). None for a dataset file."""
    if len(data) != 0x60000:
        return None
    pattern = rb"([0-9A-Z]{3}909144[A-Z]{0,2}) ?\x00?(\d{4})"
    match = re.search(pattern, data[0x5D000:0x5E000]) or re.search(pattern, data)
    return (match.group(1).decode(), match.group(2).decode()) if match else None


def eps_dataset_ids(data: bytes):
    """The three dataset ID strings of a gen3 0x5E dataset ("000" = factory placeholder)."""
    offset = 0x794 if len(data) == 0x1000 else 0x5E794 if len(data) == 0x60000 else None
    if offset is None:
        return None
    ids = data[offset:offset + 12].split(b"\x00")[:3]
    if len(ids) != 3 or not all(len(i) == 3 and i.isdigit() for i in ids):
        return None
    return [i.decode() for i in ids]


def _version(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _option(family, data, preset, correct_checksum, simulator_mode):
    key, label, start, end, hint = preset
    option = {"key": key, "label": label, "start": start, "end": end, "hint": hint,
              "size": end - start + 1}
    try:
        prepared = _prepare(family, data, start, end, correct_checksum=correct_checksum,
                            simulator_mode=simulator_mode)
        option.update(valid=True, metadata=_summary(prepared["metadata"]))
    except ValueError as exc:
        option.update(valid=False, reason=str(exc))
    return option


def plan_flash(path: str, family: str, reference: str = None, correct_checksum=False,
               simulator_mode=False, ecu_version=None) -> dict:
    data = Path(path).read_bytes()
    if family == BCM:
        return _plan_bcm(data, reference)
    if family == EPS:
        return _plan_eps(data, ecu_version, correct_checksum)
    return _plan_haldex(data, reference, correct_checksum, simulator_mode)


def _plan_eps(data, ecu_version, correct_checksum) -> dict:
    """EPS racks can't be read back, so there is no diff. Which blocks may be
    written depends on the module's firmware:
      0x5E dataset   firmware 3000+, and not the factory placeholder dataset
      0x5D config    firmware 3000+, and only onto the exact version it was built for
      full firmware  modules on 2901 or newer (older upgrades aren't understood yet)
    """
    module = _version(ecu_version)
    firmware = eps_firmware(data)
    image_version = _version(firmware[1]) if firmware else None
    ids = eps_dataset_ids(data)
    placeholder = bool(ids) and all(i == "000" for i in ids)
    presets = {p[0]: p for p in PRESETS[EPS]}
    options = []
    notes = []

    dataset = _option(EPS, data, presets["dataset"], correct_checksum, False)
    if dataset["valid"]:
        if module is None:
            dataset.update(valid=False, reason="The module's firmware is unknown (not identified).")
        elif module < 3000:
            dataset.update(valid=False, reason=f"0x5E datasets need firmware 3000 or newer; "
                                               f"this module runs {module}.")
        elif placeholder:
            dataset.update(valid=False, reason="This image's dataset is the factory placeholder "
                                               "(000), not a real steer dataset.")
    if ids and not placeholder:
        dataset["hint"] += f" Dataset {ids[0]}."
    options.append(dataset)

    if len(data) == 0x60000:
        config = _option(EPS, data, presets["config"], correct_checksum, False)
        if config["valid"]:
            if image_version is None:
                config.update(valid=False, reason="Can't tell which firmware this image's 0x5D "
                                                  "block was built for.")
            elif module is None:
                config.update(valid=False, reason="The module's firmware is unknown (not identified).")
            elif module < 3000:
                # The flasher refuses partial-region flashes below 3000.
                config.update(valid=False, reason=(
                    f"This module runs {module}; below 3000 only full firmware can be flashed."))
            elif module != image_version:
                config.update(valid=False, reason=(
                    f"0x5D blocks only fit the firmware they were built for: this image is "
                    f"{image_version}, the module runs {module}."))
        options.append(config)

        full = _option(EPS, data, presets["full"], correct_checksum, False)
        if full["valid"] and module is not None and module < 2901:
            full.update(valid=False, reason=(
                f"This module runs {module}. Upgrading from firmware below 2901 needs more "
                "investigation and isn't supported yet."))
        if full["valid"] and module is None:
            full["hint"] += " The module's current firmware is unknown (recovery)."
        options.append(full)
        if full["valid"] and placeholder:
            notes.append("This is a factory firmware with the placeholder 000 dataset. After a "
                         "full flash the rack still needs a real steer dataset: flash one straight "
                         "after (a 4 KiB dataset file, or the 0x5E block of a full image).")

    recommended = next((o for o in options if o["valid"]), None)
    applied = [name for name, status in (known_patches(data) or {}).items() if status == "applied"]
    config_option = next((o for o in options if o["key"] == "config"), None)
    if applied and config_option and config_option["valid"]:
        # A 0x5D patch (e.g. HCA) is only delivered by writing the 0x5D block.
        recommended = config_option
    if recommended is None:
        reason = "Nothing in this image can be written to this module."
    elif recommended["key"] == "dataset":
        reason = "Only the steer dataset is written. Firmware and configuration stay as they are."
    elif recommended["key"] == "config" and applied:
        reason = (f"This image has {', '.join(applied)} applied in its 0x5D block, so that "
                  "block is written.")
    elif recommended["key"] == "config":
        reason = "Same firmware as the module, so only the 0x5D configuration block is written."
    else:
        reason = "The whole firmware is written."
    return {"family": EPS, "options": options, "reference": None, "changes": None,
            "recommended": recommended["key"] if recommended else None, "reason": reason,
            "notes": notes,
            "needs_dataset": placeholder and any(o["key"] == "full" and o["valid"] for o in options),
            "firmware": {"part": firmware[0], "version": firmware[1]} if firmware else None,
            "dataset_ids": ids}


def _plan_haldex(data, reference, correct_checksum, simulator_mode) -> dict:
    """The full application is always the default. Smaller ranges are offered
    (under advanced options in the UI) only when they cover every byte that
    differs from the module's last known image."""
    options = [_option(HALDEX, data, p, correct_checksum, simulator_mode)
               for p in PRESETS[HALDEX]]
    full = next(o for o in options if o["key"] == "full")
    plan = {"family": HALDEX, "options": options, "reference": None, "changes": None,
            "notes": [], "needs_dataset": False}
    reason = "The full application is written."
    ref_data = Path(reference).read_bytes() if reference and Path(reference).is_file() else None
    if ref_data is not None and len(ref_data) == len(data):
        low, high = FULL[HALDEX]
        new = data
        if full["valid"]:
            new = _prepare(HALDEX, data, low, high, correct_checksum=correct_checksum,
                           simulator_mode=simulator_mode)["image"]
        changes = _changed_ranges(new, ref_data, low, high)
        plan["reference"] = str(reference)
        plan["changes"] = [{"start": a, "end": b} for a, b in changes]
        if not changes:
            reason = "This image is identical to what's on the module. Flashing it changes nothing."
        else:
            first, last = changes[0][0], changes[-1][1]
            lo = max(a for a, _ in HALDEX_BLOCKS if a <= first)
            hi = min(b for _, b in HALDEX_BLOCKS if b >= last)
            if not any(o["start"] == lo and o["end"] == hi for o in options):
                options.insert(0, _option(
                    HALDEX, data, ("minimal", "Changed blocks only", lo, hi,
                                   "Just the blocks that differ from your backup."),
                    correct_checksum, simulator_mode))
            for option in options:
                if option["valid"] and not (option["start"] <= first and last <= option["end"]):
                    option.update(valid=False, reason=(
                        "Doesn't cover every change "
                        f"(changes span 0x{first:05X}-0x{last:05X})."))
            total = sum(b - a + 1 for a, b in changes)
            reason = (f"{total} byte(s) differ from your backup, inside "
                      f"0x{first:05X}-0x{last:05X}. The full application is written.")
    plan["recommended"] = "full" if full["valid"] else None
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
    if family == BCM:
        # The BCM can't be read back and has no patches module; there is no
        # full-image on-module state to compose.
        return {"written": False, "reason": "BCM has no full-image state to compose"}
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
