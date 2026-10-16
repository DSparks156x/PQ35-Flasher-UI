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
    QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
    QScrollArea, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .storage import data_dir, timestamp
from .widgets import Banner, Card, DropZone, FlowRow, HoldButton, ProgressPanel, h1, kib, muted

HALDEX, EPS = "haldex-gen4", "pq-eps"
# Modules whose firmware can be read back over OBD. EPS racks can't, so there
# is no backup to require and nothing to diff against.
BACKUP_FAMILIES = {HALDEX}


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


def identity_rows(identity):
    rows = [("Part number", identity.get("software_part_number") or identity.get("part_number")),
            ("Software version", identity.get("sw_version")),
            ("Description", identity.get("system_desc")),
            ("Flash attempts / successes", f"{identity.get('flash_attempts')} / {identity.get('flash_counter')}"),
            ("Last flashed", identity.get("flash_date") or "never / unknown")]
    return [(k, str(v)) for k, v in rows if v not in (None, "")]


class ConnectPage(Page):
    title = "Connect"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self.adapter_card = self.add(Card("Interface"))
        self.adapter_text = self.adapter_card.add(muted(""))
        self.adapter_help = self.adapter_card.add(Banner())
        self.bring_up = QPushButton("")
        self.bring_up.clicked.connect(self.bring_up_socketcan)
        self.bring_up.hide()
        row = QHBoxLayout()
        row.addWidget(self.bring_up)
        row.addStretch(1)
        self.adapter_card.add(row)
        self.bring_up_process = None

        card = self.add(Card("Identify the module"))
        card.add(muted("Turn the ignition on (engine off), then identify the module. "
                       "This only reads. Nothing is written."))
        row = QHBoxLayout()
        self.identify_button = QPushButton("Identify module")
        self.identify_button.setObjectName("primary")
        self.identify_button.clicked.connect(self.identify)
        row.addWidget(self.identify_button)
        row.addStretch(1)
        card.add(row)
        self.banner = card.add(Banner())
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(24)
        card.add(self.grid)
        self.next_hint = self.add(Banner())
        self.finish()

    def refresh(self):
        adapter, family = self.state.adapter, self.state.family
        backends = self.state.hello.get("backends", {})
        if not adapter:
            self.adapter_text.setText("No CAN interface found.")
            self.adapter_help.show_message(
                "Plug in your adapter, then press the refresh button next to the interface list.\n"
                "• J2534 (Windows): install the adapter's own driver.\n"
                "• CANable / candleLight (gs_usb): no driver needed on Windows 10/11. "
                "If it isn't listed, use Zadig to bind WinUSB to it once.\n"
                "• Linux SocketCAN: sudo ip link set can0 up type can bitrate 500000", "info")
        self.bring_up.hide()
        if adapter:
            self.adapter_text.setText(adapter.get("label", ""))
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
        self.identify_button.setEnabled(bool(adapter and family) and not self.state.busy)
        self._show_identity()

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

    def _show_identity(self):
        while self.grid.count():
            self.grid.takeAt(0).widget().deleteLater()
        identity = self.state.identity
        if not identity:
            self.next_hint.hide()
            return
        for row, (key, value) in enumerate(identity_rows(identity)):
            label = muted(key)
            value_label = QLabel(value)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.grid.addWidget(label, row, 0)
            self.grid.addWidget(value_label, row, 1)
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
                    "The module didn't answer. Check the ignition is on, the adapter is plugged "
                    "in, and the right module is selected.\n\n" + str(result.get("error") or ""),
                    "error")
                return
            identity = result.get("result") or {}
            self.state.set_identity(identity)
            self.banner.show_message(f"Found {self.state.part()} {identity.get('sw_version', '')}", "ok")

        def failed(error):
            self.state.set_identity(None)
            message = self.error_text(error)
            if "not " in message and "identified as" in message:
                message = ("That module isn't a " + self.state.family["label"] + ". "
                           "Pick the right module from the list.\n\n" + message)
            self.banner.show_message(message, "error")

        self.state.run("identify", {}, done, failed)


class BackupPage(Page):
    title = "Back up"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        card = self.add(Card("Read the module"))
        card.add(muted("Copies the firmware off the module into your library. It's your way "
                       "back if a tune misbehaves, and new images are compared against it. "
                       "Takes about 7 minutes over OBD. Nothing is written."))
        row = QHBoxLayout()
        self.read_button = QPushButton("Read module")
        self.read_button.setObjectName("primary")
        self.read_button.clicked.connect(self.read)
        row.addWidget(self.read_button)
        self.open_button = QPushButton("Open library folder")
        self.open_button.clicked.connect(self.open_folder)
        row.addWidget(self.open_button)
        row.addStretch(1)
        card.add(row)
        self.progress = card.add(ProgressPanel())
        self.banner = card.add(Banner())
        history = self.add(Card("Backups of this module"))
        self.list = history.add(QListWidget())
        self.list.setMinimumHeight(140)
        self.finish()
        state.progress.connect(self.on_progress)

    def refresh(self):
        record = self.state.record
        family = self.state.family["name"] if self.state.family else None
        if family and family not in BACKUP_FAMILIES:
            record = None
            self.banner.show_message("This module can't be read back over OBD, so it can't be "
                                     "backed up here.", "info")
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


class FlashPage(Page):
    title = "Flash"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self.path = None
        self.loaded_for = None
        self.inspection = None
        self.plan = None
        self.gate = self.add(Banner())

        file_card = self.add(Card("1. Choose the image"))
        self.drop = file_card.add(DropZone())
        self.drop.picked.connect(self.load)
        self.badges = file_card.add(FlowRow())
        self.file_banner = file_card.add(Banner())

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
        for box in (self.correct, self.simulator, self.recovery, self.no_backup):
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
        self.finish()
        state.progress.connect(self.on_progress)
        state.open_in_flash.connect(self.load)

    # --- state ---

    def _toggle_advanced(self, on):
        self.advanced.setVisible(on)
        if not on:
            for box in (self.correct, self.simulator, self.recovery, self.no_backup):
                box.setChecked(False)
        if self.plan:
            self._planned(self.plan)

    def _options_changed(self, *_):
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

    def refresh(self):
        family = self.state.family
        if self.path and self.loaded_for != (self.family_name(), self.state.part()):
            self._reset_file()
        self.simulator.setVisible(self.family_name() == HALDEX)
        self.no_backup.setVisible(self.family_name() in BACKUP_FAMILIES)
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
        if any(c["level"] == "error" and not (c["key"] == "checksums" and self.correct.isChecked())
               for c in self.inspection["checks"]):
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

    # --- image ---

    def _reset_file(self):
        self.path = self.inspection = self.plan = None
        self.loaded_for = None
        self.drop.set_file("Drop a .bin file here, or click to choose", "")
        self.badges.clear()
        self.file_banner.hide()
        self._clear_options()

    def load(self, path):
        if self.state.busy:
            return
        self.path = path
        self.loaded_for = (self.family_name(), self.state.part())
        self.inspection = self.plan = None
        self.result_banner.hide()
        self.drop.set_file(Path(path).name, "Checking…")
        self.badges.clear()
        self._clear_options()
        family = self.family_name()
        if not family:
            self.file_banner.show_message("Choose a module at the top first.", "warning")
            return
        self.state.offline("inspect_image", {"path": path, "family": family,
                                             "ecu_part": self.state.part() or None},
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
            if not (result.get("correctable") and self.correct.isChecked()):
                self.refresh()
                return
        elif warnings:
            self.file_banner.show_message("\n".join(c["detail"] for c in warnings), "warning")
        else:
            self.file_banner.hide()
        reference = None
        if self.state.record and self.family_name() in BACKUP_FAMILIES:
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
        self.result_banner.show_message("Flashing. Don't touch the car, the cable or this computer.", "warning")
        self.progress.start("Connecting")
        QTimer.singleShot(50, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))
        self.flashed_plan = dict(self.plan, flashed=option["key"])
        self.state.run("flash", params, lambda r: self._flashed(r, params, option), self._flash_failed)

    def on_progress(self, message):
        if self.progress.isVisibleTo(self) and self.state.busy:
            self.progress.update_from(message)

    def _flash_failed(self, error):
        self.progress.stop()
        self.result_banner.show_message(
            "The flash didn't start or the worker stopped: " + self.error_text(error)
            + "\n\nIf the module no longer identifies, it is waiting in its bootloader. "
              "Flash again with Recovery mode and the same image.", "error")

    def _flashed(self, result, params, option):
        report = result.get("result") or {}
        record = self.state.record
        entry = {"kind": "flash", "image": self.path, "sha256": self.inspection["sha256"],
                 "start": option["start"], "end": option["end"], "status": report.get("status"),
                 "boot_verified": report.get("boot_verified"), "recovery": params["recovery"],
                 "error": result.get("error")}
        if record:
            record.add(entry)
        if result.get("error") or report.get("status") != "ok":
            self.progress.stop()
            needs_recovery = report.get("recovery_required", True)
            if record:
                record.forget_state()
            self.result_banner.show_message(
                "Flash failed: " + str(result.get("error") or report.get("error") or report.get("status"))
                + ("\n\nThe module is probably waiting in its bootloader. That's recoverable: "
                   "turn on Recovery under advanced options and flash the same image again."
                   if needs_recovery else "\n\nNothing was erased."), "error")
            return
        if record and self.family_name() in BACKUP_FAMILIES:
            reference = record.state()
            self.state.offline("compose_state", {
                **{k: params[k] for k in ("correct_checksum", "simulator_mode")},
                "path": self.path, "family": self.family_name(), "start": option["start"],
                "end": option["end"], "reference": str(reference) if reference else None,
                "out": str(record.state_path) + ".new"},
                lambda r: self._state_written(r, record), lambda e: record.forget_state())
        self.progress.start("Checking the module boots")
        self.state.run("identify", {}, self._verified, self._not_verified)

    def _state_written(self, result, record):
        new = Path(str(record.state_path) + ".new")
        if result.get("written") and new.is_file():
            new.replace(record.state_path)
        else:
            record.forget_state()

    def _verified(self, result):
        self.progress.stop()
        identity = result.get("result") or {}
        if result.get("error") or result.get("exit_code"):
            self._not_verified({"message": result.get("error")})
            return
        self.state.set_identity(identity)
        self.result_banner.show_message(
            f"Done. The module is running again: {self.state.part()} {identity.get('sw_version', '')}. "
            "Clear any fault codes the flash left behind with your usual scan tool.", "ok")
        plan = getattr(self, "flashed_plan", None) or {}
        if plan.get("needs_dataset") and plan.get("flashed") == "full":
            self.result_banner.show_message(
                self.result_banner.text() + "\n\nThis firmware came with the placeholder 000 "
                "dataset. Flash a real steer dataset next.", "warning")
            answer = QMessageBox.question(
                self, "Flash a steer dataset",
                "The firmware you flashed carries the factory placeholder dataset (000), so the "
                "rack has no real steer dataset yet.\n\nChoose a dataset to flash now? Use a "
                "4 KiB dataset file, or a full image (its 0x5E block will be used).")
            if answer == QMessageBox.StandardButton.Yes:
                path, _ = QFileDialog.getOpenFileName(self, "Choose steer dataset",
                                                      self.drop.start_dir,
                                                      "Firmware images (*.bin);;All files (*)")
                if path:
                    self.load(path)

    def _not_verified(self, error):
        self.progress.stop()
        self.result_banner.show_message(
            "The flash finished, but a fresh identification failed: " + self.error_text(error)
            + "\nCycle the ignition and press Identify on the Connect page.", "warning")


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
        default = str(source.with_name(f"{source.stem}_{feature}{source.suffix}"))
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
        out = self.add(Card("Result"))
        self.tree = out.add(QTreeWidget())
        self.tree.setHeaderLabels(["Field", "Value"])
        self.tree.setColumnWidth(0, 280)
        self.tree.setMinimumHeight(260)
        self.finish()

    def refresh(self):
        ops = set(self.state.family["operations"]) if self.state.family else set()
        for op, button in self.buttons.items():
            button.setVisible(op in ops)
            button.setEnabled(bool(self.state.identity) and not self.state.busy)
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
        self.log_dir = data_dir() / "logs"
        self.log_dir.mkdir(exist_ok=True)
        self.log_path = self.log_dir / f"session_{timestamp()}.log"
        self.file = open(self.log_path, "a", encoding="utf-8")
        row = QHBoxLayout()
        self.verbose = QCheckBox("Show protocol detail")
        row.addWidget(self.verbose)
        row.addStretch(1)
        bundle = QPushButton("Save support bundle…")
        bundle.clicked.connect(self.save_bundle)
        row.addWidget(bundle)
        folder = QPushButton("Open data folder")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir()))))
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
