"""The screens behind the left-hand navigation."""
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QFileDialog, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QListWidget, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
    QScrollArea, QSpinBox, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .storage import images_dir, logs_dir, patched_dir, prune_logs, timestamp
from .widgets import Badge, Banner, Card, DropZone, FlowRow, HoldButton, ProgressPanel, h1, kib, muted, restyle

HALDEX, EPS, BCM = "haldex-gen4", "pq-eps", "pq-bcm"
# Modules whose full firmware can be read back over OBD and diffed, so the Flash
# page can require and compare against a backup. EPS and BCM can't be read back
# in full, so flashing isn't gated on a backup for them.
BACKUP_FAMILIES = {HALDEX}
# Modules the Back up page can snapshot. The BCM snapshot is the boot page plus
# identity/coding (not the full firmware), kept before flashing.
BACKUP_CAPABLE = {HALDEX, BCM}


class Page(QScrollArea):
    title = ""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("page")
        self.setWidget(inner)
        self.layout_ = QVBoxLayout(inner)
        self.layout_.setContentsMargins(28, 22, 28, 22)
        self.layout_.setSpacing(14)
        self.layout_.addWidget(h1(self.title))
        state.changed.connect(self.refresh)
        state.busy_changed.connect(lambda _busy: self.refresh())

    def add(self, widget):
        self.layout_.addWidget(widget)
        return widget

    def finish(self):
        self.layout_.addStretch(1)

    def refresh(self):
        pass

    def error_text(self, error):
        return error.get("message", str(error)) if isinstance(error, dict) else str(error)


def convert_sgo_then(page, path, family_name, banner, then):
    """EPS SGOs become a 384 KiB .bin in Documents/PQ35 Flasher/Images first;
    everything after that (checks, patches, flashing) works on the .bin."""
    if not str(path).lower().endswith(".sgo"):
        then(path)
        return
    if family_name != EPS:
        banner.show_message("SGO files are supported for EPS racks only. Use a .bin image.", "error")
        return
    out = images_dir() / f"{Path(path).stem}.bin"
    if out.exists():
        out = images_dir() / f"{Path(path).stem}_{timestamp()}.bin"

    def done(result):
        banner.show_message(f"Converted the SGO ({result.get('source_name') or Path(path).name}, "
                            f"{result['part']}) to {Path(result['out']).name} in your Images "
                            "folder.", "info")
        then(result["out"])

    page.state.offline("convert_sgo", {"path": str(path), "out": str(out)}, done,
                       lambda e: banner.show_message(page.error_text(e), "error"))


def identity_rows(identity):
    rows = [("Part number", identity.get("software_part_number") or identity.get("part_number")),
            ("Software version", identity.get("sw_version")),
            ("Description", identity.get("system_desc")),
            ("Flash attempts / successes", f"{identity.get('flash_attempts')} / {identity.get('flash_counter')}"),
            ("Last flashed", identity.get("flash_date") or "never / unknown")]
    return [(k, str(v)) for k, v in rows if v not in (None, "")]


SCAN_TEXT = {
    "ok": ("Found", "ok"),
    "bootloader": ("In its bootloader", "warning"),
    "responding": ("Answers, but won't identify", "warning"),
    "unexpected": ("Answers as a different module", "error"),
    "absent": ("Not found", "idle"),
    None: ("Not checked", "idle"),
}
# Cards are listed in this order; "absent" never gets a card.
STATUS_ORDER = {"ok": 0, "bootloader": 1, "responding": 2, "unexpected": 3, None: 4}


class ModuleCard(Card):
    """One module that answered (or that you picked): what it is and what to do."""

    def __init__(self, family, page):
        super().__init__()
        self.family = family
        self.page = page
        self.entry = None
        head = QHBoxLayout()
        title = QLabel(family["label"])
        title.setStyleSheet("font-weight: 600;")
        head.addWidget(title)
        head.addWidget(muted(f"0x{family['module']:02X}"))
        head.addStretch(1)
        self.badge = Badge("", "idle")
        head.addWidget(self.badge)
        self.add(head)
        self.detail = self.add(muted(""))
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(24)
        self.add(self.grid)
        row = QHBoxLayout()
        self.use = QPushButton("Use this module")
        self.use.setObjectName("primary")
        self.use.clicked.connect(lambda: page.choose(self.family, self.entry))
        self.identify = QPushButton("Identify again")
        self.identify.clicked.connect(page.identify)
        self.recover = QPushButton("Recover…")
        self.recover.setObjectName("danger")
        self.recover.clicked.connect(lambda: page.recover(self.family))
        for button in (self.use, self.identify, self.recover):
            row.addWidget(button)
        row.addStretch(1)
        self.add(row)

    def status(self):
        return self.entry["status"] if self.entry else None

    def show_entry(self, entry, selected, identity):
        self.entry = entry
        status = self.status()
        text, level = SCAN_TEXT.get(status, SCAN_TEXT[None])
        if selected and identity:
            text, level = "Selected", "info"
        self.badge.set(text, level)
        found = (entry or {}).get("identity") or {}
        part = found.get("software_part_number") or found.get("part_number") or ""
        if selected and identity:
            detail = ""
        elif status == "ok":
            detail = f"{part} {found.get('sw_version', '')}  ·  {found.get('system_desc', '')}"
        elif status == "bootloader":
            detail = ("Something at this address answers but isn't running its application"
                      + (f" ({found.get('system_desc')})" if found.get("system_desc") else "")
                      + ". It needs a recovery flash.")
        elif status == "responding":
            detail = ("A controller opened a channel here but didn't identify. Usually that's a "
                      "loader waiting for a flash.")
        elif status == "unexpected":
            detail = (f"Identifies as {part} {found.get('sw_version', '')} "
                      f"({found.get('system_desc', '')}), not a {self.family['label']}.")
        else:
            detail = "Picked by hand. Press Identify to check it's there."
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))

        while self.grid.count():
            self.grid.takeAt(0).widget().deleteLater()
        if selected and identity:
            for row, (key, value) in enumerate(identity_rows(identity)):
                value_label = QLabel(value)
                value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self.grid.addWidget(muted(key), row, 0)
                self.grid.addWidget(value_label, row, 1)

        stuck = status in ("bootloader", "responding")
        self.use.setVisible(not selected and status == "ok")
        self.identify.setText("Identify again" if identity else "Identify")
        self.identify.setVisible(selected and not stuck)
        self.recover.setVisible(stuck and self.family.get("can_flash", False))
        self.setProperty("selected", "true" if selected else "false")
        restyle(self)


class ConnectPage(Page):
    title = "Connect"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self.scan_results = {}
        self.cards = {}

        find = self.add(Card())
        find.add(muted("Ignition on, engine off. Find modules asks every supported module "
                       "address what's there. It only reads; nothing is written."))
        row = QHBoxLayout()
        self.scan_button = QPushButton("Find modules")
        self.scan_button.setObjectName("primary")
        self.scan_button.clicked.connect(self.scan)
        row.addWidget(self.scan_button)
        self.pick_button = QToolButton()
        self.pick_button.setText("Pick by hand  ▾")
        self.pick_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.pick_menu = QMenu(self)
        self.pick_button.setMenu(self.pick_menu)
        row.addWidget(self.pick_button)
        self.adapter_text = muted("")
        row.addWidget(self.adapter_text, 1)
        find.add(row)
        self.adapter_help = find.add(Banner())
        self.bring_up = QPushButton("")
        self.bring_up.clicked.connect(self.bring_up_socketcan)
        self.bring_up.hide()
        row = QHBoxLayout()
        row.addWidget(self.bring_up)
        row.addStretch(1)
        find.add(row)
        self.bring_up_process = None
        self.scan_banner = find.add(Banner())

        self.cards_box = QVBoxLayout()
        self.cards_box.setSpacing(10)
        holder = QWidget()
        holder.setLayout(self.cards_box)
        self.add(holder)
        self.absent = self.add(muted(""))
        self.banner = self.add(Banner())
        self.next_hint = self.add(Banner())
        self.finish()

    # --- module cards ---

    def _build_cards(self):
        if self.cards or not self.state.families:
            return
        self.pick_menu.clear()
        for family in self.state.families:
            self.cards[family["name"]] = ModuleCard(family, self)
            self.pick_menu.addAction(f"{family['label']}  (0x{family['module']:02X})",
                                     lambda f=family: self.choose(f, self.scan_results.get(f["name"])))

    def _refresh_cards(self):
        self._build_cards()
        selected = self.state.family["name"] if self.state.family else None
        shown = []
        for name, card in self.cards.items():
            entry = self.scan_results.get(name)
            if name == selected and self.state.identity:
                entry = dict(entry or {}, status="ok", identity=self.state.identity)
            status = entry["status"] if entry else None
            if status == "absent" and name != selected:
                status = "hidden"
            elif status is None and name != selected:
                status = "hidden"
            card.show_entry(entry, name == selected,
                            self.state.identity if name == selected else None)
            if status != "hidden":
                shown.append((STATUS_ORDER.get(status, 5), name != selected, card))
            card.setVisible(status != "hidden")
        for _, _, card in sorted(shown, key=lambda item: (item[0], item[1])):
            self.cards_box.removeWidget(card)
            self.cards_box.addWidget(card)
        missing = [self.cards[n].family for n, e in self.scan_results.items()
                   if e["status"] == "absent" and n != selected]
        self.absent.setText("Not found: " + ", ".join(
            f"{f['label']} (0x{f['module']:02X})" for f in missing) if missing else "")
        self.absent.setVisible(bool(missing))

    def choose(self, family, entry):
        identity = (entry or {}).get("identity") if (entry or {}).get("status") == "ok" else None
        self.banner.hide()
        self.state.select_module(family, identity)

    def recover(self, family):
        self.state.select_module(family)
        self.state.start_recovery.emit()

    def scan(self):
        self.scan_banner.show_message("Looking for modules…", "info")
        self.banner.hide()

        def done(result):
            entries = result.get("result") or []
            if result.get("error"):
                self.scan_banner.show_message("The scan stopped: " + str(result["error"]), "error")
                return
            self.scan_results = {e["family"]: e for e in entries}
            found = [e for e in entries if e["status"] == "ok"]
            stuck = [e for e in entries if e["status"] in ("bootloader", "responding")]
            current = (self.state.family["name"] if self.state.family
                       else self.state.settings.get("module"))
            pick = next((e for e in found if e["family"] == current), None)
            if pick is None and len(found) == 1:
                pick = found[0]
            if pick:
                self.choose(self.state.family_by_name(pick["family"]), pick)
            elif current and current not in {e["family"] for e in found}:
                self.state.set_identity(None)
            if not entries or all(e["status"] == "absent" for e in entries):
                self.scan_banner.show_message(
                    "Nothing answered. Check the ignition is on and the adapter is plugged into "
                    "the car (or the bench harness is powered).", "warning")
            elif stuck:
                self.scan_banner.show_message(
                    "A module is waiting in its loader. Use Recover on its card to flash it back.",
                    "warning")
            elif len(found) > 1 and not pick:
                self.scan_banner.show_message("Found several modules. Choose one.", "info")
            else:
                self.scan_banner.hide()
            self._refresh_cards()

        def failed(error):
            self.scan_banner.show_message(self.error_text(error), "error")

        self.state.run("scan", {}, done, failed)

    def refresh(self):
        adapter = self.state.adapter
        backends = self.state.hello.get("backends", {})
        self._refresh_cards()
        self.bring_up.hide()
        if not adapter:
            self.adapter_text.setText("No CAN interface found.")
            self.adapter_help.show_message(
                "Plug in your adapter; it shows up on its own within a few seconds.\n"
                "• J2534 (Windows): install the adapter's own driver.\n"
                "• CANable / candleLight (gs_usb): no driver needed on Windows 10/11. "
                "If it isn't listed, use Zadig to bind WinUSB to it once.\n"
                "• Linux SocketCAN: plug it in and it shows up as can0.", "info")
        else:
            self.adapter_text.setText(f"via {adapter.get('label', '')}")
            problem = adapter.get("problem")
            if adapter["adapter"] == "gs_usb" and not backends.get("gs_usb"):
                problem = "gs_usb support isn't installed. Re-run the installer."
            elif adapter["adapter"] == "socketcan" and adapter.get("up") is False:
                problem = (f"{adapter['channel']} is down. Bring it up at 500 kbit/s first "
                           "(you'll be asked for your password).")
                self.bring_up.setText(f"Bring up {adapter['channel']}")
                self.bring_up.show()
            elif adapter["adapter"] == "socketcan" and adapter.get("bitrate") not in (None, 500000):
                problem = (f"{adapter['channel']} is set to {adapter['bitrate'] // 1000} kbit/s. "
                           "The PQ35 powertrain/diagnostic CAN runs at 500 kbit/s.")
                self.bring_up.setText(f"Set {adapter['channel']} to 500 kbit/s")
                self.bring_up.show()
            elif adapter["adapter"] == "j2534" and not adapter.get("present", True):
                problem = f"The registered driver DLL is missing: {adapter.get('dll')}"
            self.adapter_help.show_message(problem or "", "warning")
        idle = not self.state.busy
        self.scan_button.setEnabled(bool(adapter) and idle)
        self.pick_button.setEnabled(idle)
        for card in self.cards.values():
            card.setEnabled(idle)
        self._next_hint()

    def bring_up_socketcan(self):
        channel = str((self.state.adapter or {}).get("channel", ""))
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", channel):
            return
        pkexec, ip = shutil.which("pkexec"), shutil.which("ip")
        if not (pkexec and ip):
            self.adapter_help.show_message(
                "Couldn't find pkexec to ask for your password. Run this in a terminal:\n"
                f"sudo ip link set {channel} down; "
                f"sudo ip link set {channel} up type can bitrate 500000", "warning")
            return
        script = (f"{ip} link set {channel} down && "
                  f"{ip} link set {channel} up type can bitrate 500000 restart-ms 100")
        self.bring_up.setEnabled(False)
        self.bring_up_process = QProcess(self)

        def finished(code, _status):
            self.bring_up.setEnabled(True)
            if code == 0:
                self.state.refresh_interfaces.emit()
            else:
                error = bytes(self.bring_up_process.readAllStandardError()).decode(errors="replace")
                self.adapter_help.show_message(
                    f"Couldn't bring up {channel}" + (f": {error.strip()}" if error.strip() else
                                                      " (cancelled?)."), "error")

        self.bring_up_process.finished.connect(finished)
        self.bring_up_process.start(pkexec, ["/bin/sh", "-c", script])

    def _next_hint(self):
        if not self.state.identity:
            self.next_hint.hide()
            return
        if self.state.family["name"] not in BACKUP_FAMILIES:
            self.next_hint.show_message(
                "Ready. EPS racks can't be read back over OBD, so there's no backup step. "
                "Keep a copy of the firmware file the rack is on now.", "ok")
        elif self.state.record and self.state.record.has_backup():
            self.next_hint.show_message("Ready. You already have a backup of this module.", "ok")
        else:
            self.next_hint.show_message("Next: make a backup before flashing anything.", "info")

    def identify(self):
        self.banner.show_message("Talking to the module…", "info")

        def done(result):
            if result.get("error") or result.get("exit_code"):
                self.state.set_identity(None)
                self.banner.show_message(
                    "The module didn't answer. Check the ignition is on and the adapter is "
                    "plugged in, or press Find modules.\n\n" + str(result.get("error") or ""),
                    "error")
                return
            self.banner.hide()
            self.state.set_identity(result.get("result") or {})

        def failed(error):
            self.state.set_identity(None)
            message = self.error_text(error)
            if "not " in message and "identified as" in message:
                message = ("That module isn't a " + self.state.family["label"] + ". "
                           "Press Find modules to see what's there.\n\n" + message)
            self.banner.show_message(message, "error")

        self.state.run("identify", {}, done, failed)


class BackupPage(Page):
    title = "Back up"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        # Shown instead of everything else for modules that can't be read back.
        self.not_possible = self.add(Card("EPS racks can't be backed up"))
        self.not_possible.add(muted(
            "The rack's firmware can't be read back over OBD, so there's no backup step for "
            "EPS. You don't need one to flash it: the Flash page only allows writes that suit "
            "the firmware the rack reports."))
        self.not_possible.add(muted(
            "Instead, keep copies of what the rack runs now: the factory SGO or .bin for its "
            "part number and version, and its steer dataset. A factory full firmware carries "
            "the placeholder 000 dataset, so going back to stock means both."))
        self.not_possible.add(Banner()).show_message(
            "Nothing to do here for EPS. Go to Flash when you're ready.", "info")
        # BCM: no firmware backup here; its read-only page FF dump is on Diagnostics.
        self.bcm_note = self.add(Card("No firmware backup for the BCM here"))
        self.bcm_note.add(muted(
            "This app can't read the BCM's full firmware back. The read-only boot page FF "
            "dump is on the Diagnostics page."))
        card = self.add(Card("Read the module"))
        self.read_card = card
        card.add(muted("Copies the firmware off the module into your library. It's your way "
                       "back if a tune misbehaves, and new images are compared against it. "
                       "Takes about 7 minutes over OBD. Nothing is written."))
        row = QHBoxLayout()
        self.read_button = QPushButton("Read module")
        self.read_button.setObjectName("primary")
        self.read_button.clicked.connect(self.read)
        row.addWidget(self.read_button)
        self.open_button = QPushButton("Open module folder")
        self.open_button.clicked.connect(self.open_folder)
        row.addWidget(self.open_button)
        row.addStretch(1)
        card.add(row)
        self.progress = card.add(ProgressPanel())
        self.banner = card.add(Banner())
        history = self.add(Card("Backups of this module"))
        self.history_card = history
        self.list = history.add(QListWidget())
        self.list.setMinimumHeight(140)
        self.finish()
        state.progress.connect(self.on_progress)

    def refresh(self):
        record = self.state.record
        family = self.state.family["name"] if self.state.family else None
        possible = family is None or family in BACKUP_CAPABLE
        self.not_possible.setVisible(not possible)
        self.bcm_note.setVisible(False)
        self.read_card.setVisible(possible)
        self.history_card.setVisible(possible)
        if not possible:
            return
        self.read_button.setText("Back up BCM (boot page + coding)" if family == BCM
                                 else "Read module")
        self.read_button.setToolTip(
            "Saves the resident boot page and the BCM's identity/coding. The application "
            "firmware can't be read back, so this is not a full-image backup."
            if family == BCM else "")
        self.read_button.setEnabled(bool(record) and not self.state.busy)
        self.open_button.setEnabled(bool(record))
        self.list.clear()
        if not record:
            self.list.addItem("Identify a module first.")
            return
        backups = record.backups()
        for entry in reversed(backups):
            self.list.addItem(f"{entry['time']}   {entry.get('status', '')}   "
                              f"{entry.get('sha256', '')[:16]}…")
        if not backups:
            self.list.addItem("No backups yet.")

    def on_progress(self, message):
        if self.progress.isVisibleTo(self) and self.state.busy:
            self.progress.update_from(message)

    def open_folder(self):
        if self.state.record:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.state.record.root)))

    def read(self):
        record = self.state.record
        out = record.new_backup_dir()
        family = self.state.family["name"] if self.state.family else None
        if family == BCM:
            return self._backup_bcm(record, out)
        self.banner.show_message("Reading… keep the ignition on and don't unplug anything.", "info")
        self.progress.start("Connecting")

        def done(result):
            self.progress.stop()
            report = result.get("result") or {}
            status = report.get("status", "unknown")
            image = result.get("image")
            if image and Path(image).is_file():
                import hashlib
                sha = hashlib.sha256(Path(image).read_bytes()).hexdigest().upper()
                record.add({"kind": "backup", "image": image, "dir": str(out), "status": status,
                            "sha256": sha, "identity": self.state.identity})
                record.adopt_readout(image)
            level = {"captured_checksums_valid": "ok", "captured_complete": "ok"}.get(status, "warning")
            messages = {
                "captured_checksums_valid": "Backup saved. Checksums inside it are valid.",
                "captured_complete": "Backup saved.",
                "captured_partial": "Backup saved, but some areas couldn't be read. Flashing "
                                    "will play it safe and write the full range.",
                "checksum_mismatch": "Backup saved, but its checksums don't add up. Read again "
                                     "before relying on it.",
            }
            text = messages.get(status, f"Read finished with status {status}. "
                                        + str(report.get("error") or result.get("error") or ""))
            if not image:
                level, text = "error", "The read failed. " + str(report.get("error") or result.get("error") or "")
            self.banner.show_message(text, level)
            self.refresh()

        def failed(error):
            self.progress.stop()
            self.banner.show_message(self.error_text(error), "error")

        self.state.run("readout", {"out": str(out)}, done, failed)

    def _backup_bcm(self, record, out):
        """BCM snapshot: boot page FF + identity/coding bundle (read-only)."""
        self.banner.show_message("Backing up the BCM boot page and coding… keep the ignition on.", "info")
        self.progress.start("Connecting")

        def done(result):
            self.progress.stop()
            report = result.get("result") or {}
            status = report.get("status", "unknown")
            pageff = (report.get("components") or {}).get("pageff") or {}
            image = pageff.get("path")
            if image and Path(image).is_file():
                record.add({"kind": "backup", "image": image, "dir": result.get("backup_dir") or str(out),
                            "status": status, "sha256": (pageff.get("sha256") or "").upper(),
                            "scope": "boot_page+coding", "identity": self.state.identity})
            if image:
                self.banner.show_message(
                    "BCM backup saved: boot page and coding. (The application firmware can't be "
                    "read back, so this isn't a full-image backup.)",
                    "ok" if status == "ok" else "warning")
            else:
                self.banner.show_message(
                    "Backup failed. " + str(report.get("error") or result.get("error") or ""), "error")
            self.refresh()

        def failed(error):
            self.progress.stop()
            self.banner.show_message(self.error_text(error), "error")

        self.state.run("backup", {"out": str(out)}, done, failed)


class FlashPage(Page):
    title = "Flash"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self.path = None
        self.loaded_for = None
        self.inspection = None
        self.plan = None
        self.gate = self.add(Banner())

        # BCM only: a read-only check of the four gates a BCM flash's 10 85 needs
        # (supply voltage, attempt counter, two flags). Independent of the flash
        # gate below — run it any time a BCM is connected; nothing is written.
        self.precond = QWidget()
        precond_box = QVBoxLayout(self.precond)
        precond_box.setContentsMargins(0, 0, 0, 0)
        precond_row = QHBoxLayout()
        self.precond_button = QPushButton("Check programming conditions")
        self.precond_button.clicked.connect(self.check_preconditions)
        precond_row.addWidget(self.precond_button)
        precond_row.addStretch(1)
        precond_box.addLayout(precond_row)
        self.precond_banner = Banner()
        precond_box.addWidget(self.precond_banner)
        self.precond_note = muted("")
        precond_box.addWidget(self.precond_note)
        self.precond_note.hide()
        self.add(self.precond)
        self.precond.hide()

        file_card = self.add(Card("1. Choose the image"))
        self.drop = file_card.add(DropZone())
        self.drop.picked.connect(self.load)
        self.badges = file_card.add(FlowRow())
        self.file_banner = file_card.add(Banner())

        # BCM only: the stock full-span image the module currently runs. The plan
        # diffs against it and writes just the 16 KiB sectors that differ.
        self.bcm_reference = None
        self.reference_card = self.add(Card("Stock reference image"))
        ref_row = QHBoxLayout()
        self.reference_button = QPushButton("Choose stock reference…")
        self.reference_button.clicked.connect(self._choose_reference)
        ref_row.addWidget(self.reference_button)
        self.reference_label = muted("No stock reference chosen")
        ref_row.addWidget(self.reference_label, 1)
        self.reference_card.add(ref_row)
        self.reference_card.add(muted(
            "The BCM writes only the 16 KiB sectors that differ from this stock image. "
            "Point it at the full-span .bin the BCM currently runs."))

        self.range_card = self.add(Card("2. What to write"))
        self.reason = self.range_card.add(muted(""))
        self.plan_notes = self.range_card.add(Banner())
        self.options_box = QVBoxLayout()
        self.range_card.add(self.options_box)
        self.option_group = QButtonGroup(self)
        self.option_group.buttonToggled.connect(lambda *_: self.update_ready())

        advanced = self.add(Card())
        self.advanced_toggle = QCheckBox("Show advanced options")
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        advanced.add(self.advanced_toggle)
        self.advanced = QWidget()
        adv = QVBoxLayout(self.advanced)
        adv.setContentsMargins(22, 0, 0, 0)
        self.correct = QCheckBox("Fix wrong internal checksums (only for images you edited yourself)")
        self.simulator = QCheckBox("Bench simulator patches. BENCH ONLY: never put this in a car")
        self.recovery = QCheckBox("Recovery: the module is stuck in its bootloader and won't identify")
        self.no_backup = QCheckBox("Flash without a backup (not recommended)")
        self.crossflash = QCheckBox("Crossflash: allow an image for a different part number")
        for box in (self.correct, self.simulator, self.recovery, self.no_backup, self.crossflash):
            adv.addWidget(box)
            box.toggled.connect(self._options_changed)
        advanced.add(self.advanced)
        self.advanced.hide()

        checklist = self.add(Card("3. Before you flash"))
        self.checks = [QCheckBox(text) for text in (
            "Battery charger or a stable 13-14 V supply is connected",
            "Ignition on, engine off",
            "This computer is plugged in and won't be closed or unplugged")]
        for box in self.checks:
            checklist.add(box)
            box.toggled.connect(lambda *_: self.update_ready())

        go = self.add(Card("4. Flash"))
        self.summary = go.add(muted(""))
        self.flash_button = go.add(HoldButton("Hold to flash", seconds=3))
        self.flash_button.confirmed.connect(self.flash)
        self.progress = go.add(ProgressPanel())
        self.result_banner = go.add(Banner())
        self.cycle_button = QPushButton("I've cycled the ignition: check again")
        self.cycle_button.setObjectName("primary")
        self.cycle_button.clicked.connect(self.check_after_cycle)
        self.cycle_button.hide()
        go.add(self.cycle_button)
        self.expected_version = None
        self.finish()
        state.progress.connect(self.on_progress)
        state.open_in_flash.connect(self.load)
        state.start_recovery.connect(self.begin_recovery)

    # --- state ---

    def begin_recovery(self):
        """From a Recover button on the Connect page: preset recovery mode."""
        self._reset_file()
        self.advanced_toggle.setChecked(True)
        self.recovery.blockSignals(True)
        self.recovery.setChecked(True)
        self.recovery.blockSignals(False)
        self.result_banner.show_message(
            f"Recovery for the {self.state.family['label']}: choose the exact image made for this "
            "module. Recovery skips the checks that it's the module you think it is, so the "
            "wrong image bricks it.", "warning")
        self.refresh()

    def _toggle_advanced(self, on):
        self.advanced.setVisible(on)
        if not on:
            for box in (self.correct, self.simulator, self.recovery, self.no_backup,
                        self.crossflash):
                box.setChecked(False)
        if self.plan:
            self._planned(self.plan)

    def _options_changed(self, *_):
        if self.crossflash.isChecked() and self.sender() is self.crossflash:
            QMessageBox.warning(self, "Crossflash",
                                "A crossflash puts firmware for a different part number on "
                                "this module, and it won't work as before without further "
                                "steps.\n\nOnly do this if you know exactly what your car needs.")
        if self.recovery.isChecked() and self.sender() is self.recovery:
            QMessageBox.warning(self, "Recovery mode",
                                "Recovery skips every check that the module is what you think it "
                                "is. Only use it when the module is stuck in its bootloader, and "
                                "only with the exact image made for it.\n\n"
                                "Flashing the wrong image in recovery mode bricks the module.")
        if self.path:
            self.load(self.path)
        else:
            self.refresh()

    def family_name(self):
        return self.state.family["name"] if self.state.family else None

    def _choose_reference(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose stock reference image", "", "Firmware images (*.bin)")
        if not path:
            return
        self.bcm_reference = path
        self.reference_label.setText(Path(path).name)
        if self.path:
            # Re-plan against the new reference.
            self.load(self.path)

    def refresh(self):
        family = self.state.family
        if self.path and self.loaded_for != (self.family_name(), self.state.part()):
            self._reset_file()
        self.simulator.setVisible(self.family_name() == HALDEX)
        self.no_backup.setVisible(self.family_name() in BACKUP_FAMILIES)
        self.reference_card.setVisible(self.family_name() == BCM)
        can_precond = (self.family_name() == BCM and self.state.family
                       and self.state.family.get("can_check_preconditions"))
        self.precond.setVisible(bool(can_precond))
        self.precond_button.setEnabled(bool(can_precond) and not self.state.busy)
        problem = None
        if not family:
            problem = "Choose a module at the top."
        elif not family.get("can_flash"):
            problem = f"Flashing isn't supported for the {family['label']}."
        elif not self.state.identity and not self.recovery.isChecked():
            problem = "Identify the module on the Connect page first."
        elif (self.family_name() in BACKUP_FAMILIES and self.state.record
              and not self.state.record.has_backup()
              and not self.no_backup.isChecked() and not self.recovery.isChecked()):
            problem = "Make a backup on the Back up page first."
        self.gate.show_message(problem or "", "warning")
        self.update_ready()

    def selected_option(self):
        button = self.option_group.checkedButton()
        return button.property("option") if button else None

    def blocking_reason(self):
        if self.state.busy:
            return "Working…"
        if self.gate.active:
            return self.gate.text()
        if not self.inspection:
            return "Choose an image."
        allowed = {"checksums": self.correct.isChecked(), "crossflash": self.crossflash.isChecked()}
        if any(c["level"] == "error" and not allowed.get(c["key"], False)
               for c in self.inspection["checks"]):
            if any(c["key"] == "crossflash" for c in self.inspection["checks"]):
                return ("This image is for a different part number (a crossflash). Allow it under "
                        "advanced options if that's what you want.")
            return "This image can't be flashed. See the red badges."
        option = self.selected_option()
        if not option:
            return self.plan["reason"] if self.plan and not self.plan.get("recommended") else "Choose what to write."
        if not all(box.isChecked() for box in self.checks):
            return "Tick every item in the checklist."
        return None

    def update_ready(self):
        reason = self.blocking_reason()
        self.flash_button.setEnabled(reason is None)
        option = self.selected_option()
        if reason:
            self.summary.setText(reason)
        else:
            self.summary.setText(
                f"Writes {option['label']} (0x{option['start']:05X}-0x{option['end']:05X}, "
                f"{kib(option['size'])}) from {Path(self.path).name}.")

    # --- programming conditions (BCM, read-only) ---

    def check_preconditions(self):
        self.precond_button.setEnabled(False)
        self.precond_note.hide()
        self.precond_banner.show_message(
            "Checking programming conditions… nothing is written to the module.", "info")

        def done(result):
            self.precond_button.setEnabled(not self.state.busy)
            report = result.get("result") or {}
            if result.get("error") or not report:
                self.precond_banner.show_message(
                    "Couldn't read the programming conditions. "
                    + str(result.get("error") or ""), "error")
                self.precond_note.hide()
                return
            if report.get("all_ok"):
                self.precond_banner.show_message(
                    f"Programming conditions OK — supply {report.get('supply_volts')} V.", "ok")
                self.precond_note.hide()
            else:
                blocking = report.get("blocking") or ["a programming condition isn't met"]
                self.precond_banner.show_message("Not ready: " + "; ".join(blocking), "warning")
                note = report.get("scaling_note")
                self.precond_note.setText(note or "")
                self.precond_note.setVisible(bool(note))

        def failed(error):
            self.precond_button.setEnabled(not self.state.busy)
            self.precond_note.hide()
            self.precond_banner.show_message(self.error_text(error), "error")

        self.state.run("preconditions", {}, done, failed)

    # --- image ---

    def _reset_file(self):
        self.path = self.inspection = self.plan = None
        self.loaded_for = None
        self.drop.set_file("Drop a .bin file here, or click to choose", "")
        self.badges.clear()
        self.file_banner.hide()
        self._clear_options()

    def load(self, path, converted=False):
        if self.state.busy:
            return
        if str(path).lower().endswith(".sgo"):
            self.file_banner.show_message("")
            convert_sgo_then(self, path, self.family_name(), self.file_banner,
                             lambda out: self.load(out, converted=True))
            return
        self.converted = converted
        if not converted:
            self.file_banner.show_message("")
        self.path = path
        self.loaded_for = (self.family_name(), self.state.part())
        self.inspection = self.plan = None
        self.result_banner.hide()
        self.cycle_button.hide()
        self.drop.set_file(Path(path).name, "Checking…")
        self.badges.clear()
        self._clear_options()
        family = self.family_name()
        if not family:
            self.file_banner.show_message("Choose a module at the top first.", "warning")
            return
        self.state.offline("inspect_image", {"path": path, "family": family,
                                             "ecu_part": self.state.part() or None,
                                             "ecu_version": (self.state.identity or {}).get("sw_version")},
                           self._inspected, lambda e: self._file_error(e))

    def _file_error(self, error):
        self.drop.set_file(Path(self.path).name if self.path else "", "")
        self.file_banner.show_message(self.error_text(error), "error")
        self.refresh()

    def _inspected(self, result):
        self.inspection = result
        self.drop.set_file(result["name"], f"{kib(result['size'])} · SHA-256 {result['sha256']}")
        badges = [(c["label"], c["level"], c.get("detail")) for c in result["checks"]]
        self.badges.set_badges(badges)
        errors = [c for c in result["checks"] if c["level"] == "error"]
        warnings = [c for c in result["checks"] if c["level"] == "warning"]
        if errors:
            text = "\n".join(c["label"] + (": " + c["detail"] if c["detail"] else "") for c in errors)
            if result.get("correctable") and not self.correct.isChecked():
                text += ("\n\nIf you edited this image yourself, turn on \"Fix wrong internal "
                         "checksums\" under advanced options. Otherwise the file is damaged or "
                         "incomplete: get a fresh copy.")
            self.file_banner.show_message(text, "error")
            allowable = {"crossflash"} | ({"checksums"} if result.get("correctable") else set())
            if not all(c["key"] in allowable for c in errors):
                self.refresh()
                return
        elif warnings:
            self.file_banner.show_message("\n".join(c["detail"] for c in warnings), "warning")
        elif not getattr(self, "converted", False):
            self.file_banner.show_message("")
        reference = None
        if self.family_name() == BCM:
            reference = self.bcm_reference
        elif self.state.record and self.family_name() in BACKUP_FAMILIES:
            reference = self.state.record.state()
        self.state.offline("plan_flash", {
            "path": self.path, "family": self.family_name(),
            "reference": str(reference) if reference else None,
            "ecu_version": (self.state.identity or {}).get("sw_version"),
            "correct_checksum": self.correct.isChecked(),
            "simulator_mode": self.simulator.isChecked(),
        }, self._planned, self._file_error)

    def _clear_options(self):
        for button in self.option_group.buttons():
            self.option_group.removeButton(button)
            button.deleteLater()
        while self.options_box.count():
            item = self.options_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.reason.setText("")
        self.plan_notes.show_message("")

    def _planned(self, plan):
        self.plan = plan
        self._clear_options()
        self.reason.setText(plan["reason"])
        self.plan_notes.show_message("\n\n".join(plan.get("notes") or []), "warning")
        hidden = 0
        for option in plan["options"]:
            if (plan["family"] == HALDEX and option["key"] != "full"
                    and not self.advanced_toggle.isChecked()):
                hidden += 1
                continue
            text = f"{option['label']}   ·   {kib(option['size'])}"
            if option["key"] == plan.get("recommended"):
                text += "   ·   recommended"
            button = QRadioButton(text)
            button.setObjectName("option")
            button.setProperty("option", option)
            button.setToolTip(option.get("hint", ""))
            if not option["valid"]:
                button.setEnabled(False)
                button.setToolTip(option.get("reason", ""))
            self.option_group.addButton(button)
            self.options_box.addWidget(button)
            hint = muted(option.get("hint", "") if option["valid"] else "Not possible: " + option.get("reason", ""))
            hint.setContentsMargins(28, 0, 0, 4)
            self.options_box.addWidget(hint)
            if option["key"] == plan.get("recommended"):
                button.setChecked(True)
        if hidden:
            self.options_box.addWidget(muted(f"{hidden} smaller range(s) under advanced options."))
        self.refresh()

    # --- flashing ---

    def flash(self):
        if self.blocking_reason():
            return
        option = self.selected_option()
        crossing = next((c for c in self.inspection["checks"] if c["key"] == "crossflash"), None)
        if crossing:
            text, ok = QInputDialog.getText(
                self, "Confirm crossflash",
                f"{crossing['label']}.\nType CROSSFLASH to write it anyway:")
            if not ok or text.strip() != "CROSSFLASH":
                return
        if self.recovery.isChecked():
            text, ok = QInputDialog.getText(
                self, "Confirm recovery flash",
                f"Recovery mode can't check the module.\nType RECOVER to flash "
                f"{Path(self.path).name} to the {self.state.family['label']}:")
            if not ok or text.strip() != "RECOVER":
                return
        params = {"input": self.path, "start": option["start"], "end": option["end"],
                  "correct_checksum": self.correct.isChecked(),
                  "simulator_mode": self.simulator.isChecked(),
                  "recovery": self.recovery.isChecked()}
        if option.get("sectors"):
            # BCM flashes by 16 KiB sector; the worker builds --bcm-sector args.
            params["sectors"] = list(option["sectors"])
        self.result_banner.show_message("Flashing. Don't touch the car, the cable or this computer.", "warning")
        self.cycle_button.hide()
        self.progress.start("Connecting")
        QTimer.singleShot(50, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))
        self.flashed_plan = dict(self.plan, flashed=option["key"])
        self.state.run("flash", params, lambda r: self._flashed(r, params, option), self._flash_failed)

    def on_progress(self, message):
        if self.progress.isVisibleTo(self) and self.state.busy:
            self.progress.update_from(message)

    def _flash_failed(self, error):
        # The worker couldn't run the flash at all; still look at the module.
        self._reidentify("failed", "The flash didn't run: " + self.error_text(error))

    def _flashed(self, result, params, option):
        report = result.get("result") or {}
        record = self.state.record
        entry = {"kind": "flash", "image": self.path, "sha256": self.inspection["sha256"],
                 "start": option["start"], "end": option["end"], "status": report.get("status"),
                 "boot_verified": report.get("boot_verified"), "recovery": params["recovery"],
                 "error": result.get("error")}
        if record and report.get("checksum_verified"):
            try:
                entry["kept_copy"] = record.keep_flashed(self.path)
            except OSError:
                pass
        if record:
            record.add(entry)
        if report.get("boot_verified") and report.get("commit_outcome") == "ambiguous":
            # Written, checksum-verified and seen running, but the ECU didn't
            # acknowledge every commit service (often it restarts before
            # answering StopCommunication). Not a bootloader case.
            if record:
                record.forget_state()
            unanswered = [f"0x{k.upper()}" for k, v in (report.get("commit_responses") or {}).items()
                          if v != "acknowledged"]
            self._reidentify("ambiguous", "Flashed and checksum-verified. The module didn't "
                             f"acknowledge the last commit step ({', '.join(unanswered) or 'commit'}), "
                             "which the ignition cycle below settles.")
            return
        if result.get("error") or report.get("status") != "ok":
            if record:
                record.forget_state()
            self.failed_needs_recovery = report.get("recovery_required", True)
            self._reidentify("failed", "Flash failed: " + str(
                result.get("error") or report.get("error") or report.get("status")))
            return
        if record and self.family_name() in BACKUP_FAMILIES:
            reference = record.state()
            self.state.offline("compose_state", {
                **{k: params[k] for k in ("correct_checksum", "simulator_mode")},
                "path": self.path, "family": self.family_name(), "start": option["start"],
                "end": option["end"], "reference": str(reference) if reference else None,
                "out": str(record.state_path) + ".new"},
                lambda r: self._state_written(r, record), lambda e: record.forget_state())
        self._reidentify("ok", "Flashed and verified.")

    def _state_written(self, result, record):
        new = Path(str(record.state_path) + ".new")
        if result.get("written") and new.is_file():
            new.replace(record.state_path)
        else:
            record.forget_state()

    # --- after a flash: identify, then confirm across an ignition cycle ---

    def _reidentify(self, outcome, headline):
        """The worker has released the adapter; ask the module what it is now.
        This also updates the Connect page and the status pill."""
        for box in self.checks:  # another flash needs a fresh confirmation
            box.setChecked(False)
        self.progress.start("Checking the module")
        self.state.run("identify", {},
                       lambda r: self._after_identify(outcome, headline, r),
                       lambda e: self._after_identify(outcome, headline,
                                                      {"error": self.error_text(e)}))

    @staticmethod
    def _identity_from(result):
        identity = result.get("result") or {}
        if result.get("error") or result.get("exit_code") or not identity:
            return None
        return identity

    def _after_identify(self, outcome, headline, result):
        self.progress.stop()
        identity = self._identity_from(result)
        self.state.set_identity(identity)
        running = identity and not identity.get("in_bootloader")
        if outcome in ("ok", "ambiguous") and running:
            self.expected_version = identity.get("sw_version", "")
            self.result_banner.show_message(
                f"{headline} It's running {self.state.part()} {self.expected_version} "
                f"({identity.get('system_desc', '')}).\n\n"
                "Last step: turn the ignition off, wait about 10 seconds, turn it back on (or "
                "start the engine), then press Check again.",
                "ok" if outcome == "ok" else "warning")
            self.cycle_button.show()
            self._dataset_prompt()
        elif outcome in ("ok", "ambiguous"):
            self.expected_version = None
            self.result_banner.show_message(
                f"{headline} But the module didn't identify straight afterwards.\n\n"
                "Turn the ignition off, wait about 10 seconds, turn it back on, then press "
                "Check again.", "warning")
            self.cycle_button.show()
        elif running:
            self.result_banner.show_message(
                f"{headline}\n\nThe module still runs its application "
                f"({self.state.part()} {identity.get('sw_version', '')}), so nothing was lost.",
                "error")
        else:
            advice = ("\n\nThe module is probably waiting in its bootloader. That's recoverable: "
                      "Find modules on the Connect page shows it with a Recover button, or turn "
                      "on Recovery under advanced options and flash the same image again."
                      if getattr(self, "failed_needs_recovery", True) else "\n\nNothing was erased.")
            self.result_banner.show_message(headline + advice, "error")

    def check_after_cycle(self):
        self.cycle_button.setEnabled(False)
        self.progress.start("Checking after the ignition cycle")
        self.state.run("identify", {}, self._after_cycle,
                       lambda e: self._after_cycle({"error": self.error_text(e)}))

    def _after_cycle(self, result):
        self.progress.stop()
        self.cycle_button.setEnabled(True)
        identity = self._identity_from(result)
        if identity is None:
            self.result_banner.show_message(
                "The module didn't answer after the ignition cycle. Check the ignition is on, "
                "then press Check again. If it still doesn't answer, Find modules on the Connect "
                "page shows whether it's waiting in its bootloader.", "warning")
            return
        self.state.set_identity(identity)
        version = identity.get("sw_version", "")
        if identity.get("in_bootloader"):
            self.result_banner.show_message(
                "After the ignition cycle the module came up in its bootloader. Flash it again "
                "with Recovery (Find modules → Recover).", "error")
        elif self.expected_version and version != self.expected_version:
            self.result_banner.show_message(
                f"After the ignition cycle it reports {version}, not {self.expected_version}. "
                "Check what's on it before driving.", "warning")
        else:
            self.cycle_button.hide()
            if self.state.record:
                self.state.record.add({"kind": "verify", "sw_version": version,
                                       "identity": identity})
            self.result_banner.show_message(
                f"Confirmed after an ignition cycle: {self.state.part()} {version} "
                f"({identity.get('system_desc', '')}). You're done: go ahead and drive. Flashing "
                "doesn't set fault codes, and any faults left from the module being offline "
                "clear themselves once it's running normally.", "ok")

    def _dataset_prompt(self):
        plan = getattr(self, "flashed_plan", None) or {}
        if not (plan.get("needs_dataset") and plan.get("flashed") == "full"):
            return
        self.result_banner.show_message(
            self.result_banner.text() + "\n\nThis firmware came with the placeholder 000 "
            "dataset. Flash a real steer dataset next.", "warning")
        answer = QMessageBox.question(
            self, "Flash a steer dataset",
            "The firmware you flashed carries the factory placeholder dataset (000), so the "
            "rack has no real steer dataset yet.\n\nChoose a dataset to flash now? Use a "
            "4 KiB dataset file or SGO, or a full image (its 0x5E block will be used).")
        if answer == QMessageBox.StandardButton.Yes:
            path, _ = QFileDialog.getOpenFileName(self, "Choose steer dataset",
                                                  self.drop.start_dir,
                                                  "Firmware (*.bin *.sgo);;All files (*)")
            if path:
                self.load(path)


class PatchesPage(Page):
    title = "Patches"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self.path = None
        self.intro = self.add(muted(""))
        card = self.add(Card("Image"))
        self.drop = card.add(DropZone())
        self.drop.picked.connect(self.load)
        self.banner = card.add(Banner())
        self.results = self.add(Card("Result"))
        self.tree = self.results.add(QTreeWidget())
        self.tree.setHeaderLabels(["Item", "Value"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setMinimumHeight(160)
        self.actions = QHBoxLayout()
        self.results.add(self.actions)
        self.finish()

    def refresh(self):
        family = self.state.family["name"] if self.state.family else None
        self.intro.setText({
            EPS: "Check which known patches an EPS image has, and apply them. Patching writes a "
                 "new file; your original is never changed.",
            HALDEX: "Check the Layer-1 checksums of a Haldex image. The anti-brick patches are "
                    "added automatically when you flash, so there's nothing to apply here.",
        }.get(family, "No offline tools for this module yet."))
        self.setEnabled(family in (EPS, HALDEX) and not self.state.busy)

    def _clear_actions(self):
        while self.actions.count():
            item = self.actions.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def load(self, path):
        if str(path).lower().endswith(".sgo"):
            family = self.state.family["name"] if self.state.family else None
            convert_sgo_then(self, path, family, self.banner, self.load)
            return
        self.path = path
        self.tree.clear()
        self._clear_actions()
        self.banner.hide()
        self.drop.set_file(Path(path).name, str(Path(path).parent))
        family = self.state.family["name"]
        if family == EPS:
            self.state.offline("eps_patch_status", {"path": path}, self._eps_status,
                               lambda e: self.banner.show_message(self.error_text(e), "error"))
        else:
            self.state.offline("haldex_checksums", {"path": path}, self._haldex,
                               lambda e: self.banner.show_message(self.error_text(e), "error"))

    def _haldex(self, rows):
        bad = 0
        for row in rows:
            ok = row.get("valid")
            bad += ok is False
            name = f"Block 0x{row['start']:05X}-0x{row['end_exclusive'] - 1:05X}"
            if ok is None:
                value = "not checked"
            elif ok:
                value = f"valid (0x{row['stored']:04X})"
            else:
                value = f"WRONG: stored 0x{row['stored']:04X}, should be 0x{row['computed']:04X}"
            QTreeWidgetItem(self.tree, [name, value])
        self.banner.show_message("All block checksums are valid." if not bad else
                                 f"{bad} block checksum(s) are wrong. If you edited the image, the "
                                 "flasher can fix them (advanced options on the Flash page).",
                                 "ok" if not bad else "warning")

    def _eps_status(self, result):
        known = result["known_patches"]
        if not known:
            self.banner.show_message("No known patch fits this firmware version.", "info")
        for name, status in known.items():
            QTreeWidgetItem(self.tree, [name, status])
        for feature in result["features"]:
            fits = [s for n, s in known.items() if n.startswith(feature)]
            if not fits or fits[0] in ("applied",):
                continue
            button = QPushButton(f"Apply {feature}…")
            button.clicked.connect(lambda _=False, f=feature: self.apply(f))
            self.actions.addWidget(button)
        self.actions.addStretch(1)

    def apply(self, feature):
        source = Path(self.path)
        default = str(patched_dir() / f"{source.stem}_{feature}{source.suffix}")
        out, _ = QFileDialog.getSaveFileName(self, "Save patched image", default, "Firmware (*.bin)")
        if not out:
            return
        if Path(out).resolve() == source.resolve():
            self.banner.show_message("Save the patched image as a new file.", "error")
            return

        def done(result):
            self.banner.show_message(f"Saved {Path(result['out']).name}.", "ok")
            self._clear_actions()
            button = QPushButton("Flash this file")
            button.setObjectName("primary")
            button.clicked.connect(lambda: self.state.open_in_flash.emit(result["out"]))
            self.actions.addWidget(button)
            self.actions.addStretch(1)

        self.state.offline("eps_patch", {"path": self.path, "feature": feature, "out": out}, done,
                           lambda e: self.banner.show_message(self.error_text(e), "error"))


DIAGNOSTICS = (
    ("faults", "Stored faults", "Fault slots with their freeze-frame snapshots."),
    ("supply", "Supply voltage", "Battery and motor supply measuring blocks."),
    ("assist", "Assist", "Assist and torque measuring blocks."),
    ("motion", "Motion", "Steering angle and motion blocks."),
    ("can_config", "CAN configuration", "What the rack thinks is on the bus."),
    ("eeprom", "Serial EEPROM", "Saves the 1 KiB EEPROM to a file."),
)


class DiagnosticsPage(Page):
    title = "Diagnostics"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self.add(muted("Read-only. None of these clear faults or write anything. "
                       "Run them with the car stationary."))
        card = self.add(Card("Read"))
        self.read_card = card
        grid = QGridLayout()
        self.buttons = {}
        for index, (op, label, hint) in enumerate(DIAGNOSTICS):
            button = QPushButton(label)
            button.setToolTip(hint)
            button.clicked.connect(lambda _=False, o=op: self.run(o))
            grid.addWidget(button, index // 3, index % 3)
            self.buttons[op] = button
        card.add(grid)
        row = QHBoxLayout()
        row.addWidget(muted("Samples"))
        self.samples = QSpinBox()
        self.samples.setRange(1, 200)
        row.addWidget(self.samples)
        row.addStretch(1)
        card.add(row)
        self.banner = card.add(Banner())
        self._build_pageff()
        out = self.add(Card("Result"))
        self.result_card = out
        self.tree = out.add(QTreeWidget())
        self.tree.setHeaderLabels(["Field", "Value"])
        self.tree.setColumnWidth(0, 280)
        self.tree.setMinimumHeight(260)
        self.finish()
        state.progress.connect(self.on_progress)

    def _build_pageff(self):
        # BCM only. A memory read, nothing like the Flash page: no erase, no write.
        card = self.add(Card("BCM boot page FF (read-only)"))
        self.pageff_card = card
        card.add(muted("Reads the 16 KiB boot/vector page (0x7FC000–0x7FFFFF) off the BCM over "
                       "TP2.0 and saves it with a JSON sidecar in the module's folder. Nothing is "
                       "written to the module. About 256 small reads; keep the ignition on."))
        row = QHBoxLayout()
        self.pageff_button = QPushButton("Dump BCM page FF (read-only, safe)")
        self.pageff_button.clicked.connect(self.dump_pageff)
        row.addWidget(self.pageff_button)
        self.pageff_open = QPushButton("Open dump")
        self.pageff_open.clicked.connect(lambda: self._open(self.pageff_paths.get("image")))
        row.addWidget(self.pageff_open)
        self.pageff_sidecar = QPushButton("Open sidecar")
        self.pageff_sidecar.clicked.connect(lambda: self._open(self.pageff_paths.get("sidecar")))
        row.addWidget(self.pageff_sidecar)
        self.pageff_folder = QPushButton("Open folder")
        self.pageff_folder.clicked.connect(lambda: self._open(self.pageff_paths.get("out")))
        row.addWidget(self.pageff_folder)
        row.addStretch(1)
        card.add(row)
        self.pageff_progress = card.add(ProgressPanel())
        self.pageff_banner = card.add(Banner())
        self.pageff_result = card.add(muted(""))
        self.pageff_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pageff_result.setWordWrap(True)
        self.pageff_paths = {}
        self.pageff_running = False

    def refresh(self):
        ops = set(self.state.family["operations"]) if self.state.family else set()
        for op, button in self.buttons.items():
            button.setVisible(op in ops)
            button.setEnabled(bool(self.state.identity) and not self.state.busy)
        bcm = bool(self.state.family) and self.state.family["name"] == BCM
        self.read_card.setVisible(not bcm)
        self.result_card.setVisible(not bcm)
        self.pageff_card.setVisible(bcm)
        if bcm:
            self._refresh_pageff()
        if not ops & set(self.buttons):
            self.banner.show_message("No diagnostics for this module yet.", "info")
        elif not self.state.identity:
            self.banner.show_message("Identify the module on the Connect page first.", "info")
        else:
            self.banner.hide()

    def run(self, op):
        self.tree.clear()
        params = {"samples": self.samples.value()} if op in ("motion", "assist", "supply", "can_config") else {}
        if op == "eeprom" and self.state.record:
            params["out"] = str(self.state.record.root / f"eeprom_{timestamp()}.bin")
        self.banner.show_message("Reading…", "info")

        def done(result):
            report = result.get("result") or {}
            if result.get("error"):
                self.banner.show_message(str(result["error"]), "error")
            elif op == "eeprom":
                self.banner.show_message(f"Saved to {result.get('out')}", "ok")
            else:
                self.banner.hide()
            self._fill(self.tree.invisibleRootItem(), report)
            self.tree.expandToDepth(1)

        self.state.run(op, params, done, lambda e: self.banner.show_message(self.error_text(e), "error"))

    def _pageff_available(self):
        demo = bool(self.state.adapter) and self.state.adapter.get("adapter") == "demo"
        return demo or bool(self.state.family.get("read_pageff"))

    def _refresh_pageff(self):
        available = self._pageff_available()
        self.pageff_button.setEnabled(available and bool(self.state.identity) and not self.state.busy)
        for button, key in ((self.pageff_open, "image"), (self.pageff_sidecar, "sidecar"),
                            (self.pageff_folder, "out")):
            button.setEnabled(bool(self.pageff_paths.get(key)))
        if self.pageff_running:
            return
        if not available:
            self.pageff_banner.show_message(
                "This flasher build can't read page FF yet (no --read-bcm-pageff). Update the "
                "flasher submodule, then restart the app.", "warning")
        elif not self.state.identity:
            self.pageff_banner.show_message("Identify the module on the Connect page first.", "info")
        elif not self.pageff_paths and self.pageff_banner.property("level") != "error":
            self.pageff_banner.hide()

    def on_progress(self, message):
        if self.pageff_running:
            self.pageff_progress.update_from(message)

    def _open(self, path):
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def dump_pageff(self):
        params = {}
        if self.state.record:
            params["out_dir"] = str(self.state.record.root / "PageFF")
        self.pageff_paths = {}
        self.pageff_result.setText("")
        self.pageff_banner.show_message("Reading page FF… nothing is written to the module.", "info")
        self.pageff_progress.start("Connecting")

        def finished():
            self.pageff_running = False
            self.pageff_progress.stop()

        def done(result):
            finished()
            image = result.get("image")
            if image and Path(image).is_file():
                self.pageff_paths = {key: result.get(key) for key in ("image", "sidecar", "out")}
                if self.state.record:
                    import hashlib
                    sha = hashlib.sha256(Path(image).read_bytes()).hexdigest().upper()
                    self.state.record.add({"kind": "pageff", "image": image,
                                           "sidecar": result.get("sidecar"), "sha256": sha,
                                           "identity": self.state.identity})
                self.pageff_result.setText(
                    f"Dump: {image}  ({kib(Path(image).stat().st_size)})\n"
                    f"Sidecar: {result.get('sidecar') or 'none found'}")
            if result.get("error") or not self.pageff_paths:
                text = str(result.get("error") or "The read failed.")
                if result.get("exit_code"):
                    text += f" (exit code {result['exit_code']})"
                self.pageff_banner.show_message(text + " See the Log page for details.", "error")
            else:
                self.pageff_banner.show_message("Page FF saved. Nothing was written to the module.", "ok")
            self.refresh()

        def failed(error):
            finished()
            self.pageff_banner.show_message(self.error_text(error), "error")
            self.refresh()

        self.pageff_running = self.state.run("pageff", params, done, failed)
        if not self.pageff_running:
            self.pageff_progress.stop()

    def _fill(self, parent, value):
        if isinstance(value, dict):
            for key, child in value.items():
                item = QTreeWidgetItem(parent, [str(key), "" if isinstance(child, (dict, list)) else str(child)])
                self._fill(item, child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                item = QTreeWidgetItem(parent, [f"[{index}]", "" if isinstance(child, (dict, list)) else str(child)])
                self._fill(item, child)


class LogPage(Page):
    title = "Log"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        prune_logs(keep=20)
        self.log_stamp = timestamp()
        self.log_part = 1
        self.log_path = logs_dir() / f"session_{self.log_stamp}.log"
        self.file = open(self.log_path, "a", encoding="utf-8")
        row = QHBoxLayout()
        self.verbose = QCheckBox("Show protocol detail")
        row.addWidget(self.verbose)
        row.addStretch(1)
        bundle = QPushButton("Save support bundle…")
        bundle.clicked.connect(self.save_bundle)
        row.addWidget(bundle)
        folder = QPushButton("Open logs folder")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir()))))
        row.addWidget(folder)
        holder = QWidget()
        holder.setLayout(row)
        self.add(holder)
        self.text = self.add(QPlainTextEdit())
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(420)
        self.text.setMaximumBlockCount(20000)
        state.pool.event.connect(self.on_event)

    def write(self, line, show=True):
        stamped = time.strftime("%H:%M:%S ") + line
        if self.file.tell() > 5 * 1024 * 1024:
            # Roll over so one long session can't grow without bound.
            self.file.close()
            self.log_part += 1
            self.log_path = logs_dir() / f"session_{self.log_stamp}_{self.log_part}.log"
            self.file = open(self.log_path, "a", encoding="utf-8")
            prune_logs(keep=20)
        self.file.write(stamped + "\n")
        self.file.flush()
        if show:
            self.text.appendPlainText(stamped)

    def on_event(self, message):
        kind = message.get("event")
        if kind == "log":
            level = message.get("level", "info")
            self.write(f"[{level}] {message.get('message', '')}",
                       show=level != "debug" or self.verbose.isChecked())
        elif kind == "output":
            self.write(message.get("line", ""), show=self.verbose.isChecked())
        elif kind == "progress":
            self.write(f"[{message.get('stage', '').strip()}] {message.get('percent')}% "
                       f"{message.get('detail', '')}", show=self.verbose.isChecked())

    def save_bundle(self):
        default = str(Path.home() / f"pq35-support-{timestamp()}.zip")
        path, _ = QFileDialog.getSaveFileName(self, "Save support bundle", default, "Zip (*.zip)")
        if not path:
            return
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(self.log_path, "session.log")
            info = {"hello": self.state.hello, "adapter": self.state.adapter,
                    "family": self.state.family, "identity": self.state.identity}
            bundle.writestr("state.json", json.dumps(info, indent=2, default=str))
            if self.state.record:
                bundle.writestr("history.json", json.dumps(self.state.record.history(), indent=2))
        QMessageBox.information(self, "Support bundle", f"Saved {path}.\nIt contains the session "
                                "log and module history, not your firmware files.")
