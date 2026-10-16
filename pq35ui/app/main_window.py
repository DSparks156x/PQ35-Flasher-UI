"""Top bar (interface + module), left navigation, page stack."""
import ctypes
import shutil
import sys

from PySide6.QtCore import QProcess, Qt
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import pages
from .client import WorkerPool
from .state import AppState
from .storage import Settings
from .widgets import Badge, muted

NAV = (
    ("connect", "Connect", pages.ConnectPage),
    ("backup", "Back up", pages.BackupPage),
    ("flash", "Flash", pages.FlashPage),
    ("patches", "Patches", pages.PatchesPage),
    ("diagnostics", "Diagnostics", pages.DiagnosticsPage),
    ("log", "Log", pages.LogPage),
)


class SleepBlocker:
    """Keep the computer awake while a flash or read is running."""

    def __init__(self):
        self.process = None

    def hold(self):
        if sys.platform == "win32":
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
            return
        tool = shutil.which("systemd-inhibit") or shutil.which("caffeinate")
        if tool and self.process is None:
            self.process = QProcess()
            if tool.endswith("caffeinate"):
                self.process.start(tool, ["-i"])
            else:
                self.process.start(tool, ["--what=sleep:idle:handle-lid-switch",
                                          "--who=PQ35 Flasher", "--why=Writing ECU firmware",
                                          "sleep", "infinity"])

    def release(self):
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            return
        if self.process is not None:
            self.process.kill()
            self.process.waitForFinished(1000)
            self.process = None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PQ35 Flasher")
        self.resize(1040, 760)
        self.settings = Settings()
        self.pool = WorkerPool(self.settings, self)
        self.state = AppState(self.pool, self.settings, self)
        self.sleep = SleepBlocker()
        self.interfaces = []

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._topbar())

        body = QHBoxLayout()
        body.setSpacing(0)
        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedWidth(180)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(0, 12, 0, 12)
        self.nav = QListWidget()
        self.nav.setObjectName("navlist")
        nav_layout.addWidget(self.nav)
        self.version_label = muted("")
        self.version_label.setContentsMargins(14, 0, 8, 0)
        nav_layout.addWidget(self.version_label)
        body.addWidget(nav)

        self.stack = QStackedWidget()
        self.pages = {}
        for key, label, cls in NAV:
            page = cls(self.state)
            self.pages[key] = page
            self.stack.addWidget(page)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)

        self.busy_bar = QProgressBar()
        self.busy_bar.setFixedHeight(4)
        self.busy_bar.hide()
        outer.addWidget(self.busy_bar)
        self.setCentralWidget(root)

        self.state.changed.connect(self._state_changed)
        self.state.busy_changed.connect(self._busy_changed)
        self.state.progress.connect(lambda m: self.busy_bar.setValue(int(m.get("percent", 0))))
        self.state.open_in_flash.connect(lambda _path: self.nav.setCurrentRow(2))

        native = self.pool.native()
        native.call("hello", {}, self._hello, self._worker_broken)
        native.call("families", {}, self._families, self._worker_broken)
        self.refresh_interfaces()

    # --- top bar ---

    def _topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)
        title = QLabel("PQ35 Flasher")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addSpacing(12)
        layout.addWidget(muted("Interface"))
        self.interface_box = QComboBox()
        self.interface_box.setMinimumWidth(260)
        self.interface_box.currentIndexChanged.connect(self._interface_picked)
        layout.addWidget(self.interface_box)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Look for interfaces again")
        self.refresh_button.clicked.connect(self.refresh_interfaces)
        layout.addWidget(self.refresh_button)
        layout.addSpacing(8)
        layout.addWidget(muted("Module"))
        self.module_box = QComboBox()
        self.module_box.setMinimumWidth(200)
        self.module_box.currentIndexChanged.connect(self._module_picked)
        layout.addWidget(self.module_box)
        layout.addStretch(1)
        self.status = Badge("Not connected", "idle")
        layout.addWidget(self.status)
        return bar

    def refresh_interfaces(self):
        self.refresh_button.setEnabled(False)
        self.pool.native().call("list_interfaces", {}, self._interfaces, self._worker_broken)

    def _interfaces(self, interfaces):
        self.refresh_button.setEnabled(True)
        self.interfaces = interfaces
        remembered = self.settings.get("interface")
        self.interface_box.blockSignals(True)
        self.interface_box.clear()
        selected = 0
        for index, interface in enumerate(interfaces):
            self.interface_box.addItem(interface["label"])
            if remembered and all(interface.get(k) == remembered.get(k)
                                  for k in ("adapter", "dll", "channel", "serial")):
                selected = index
        if not interfaces:
            self.interface_box.addItem("No interface found")
        self.interface_box.setCurrentIndex(selected)
        self.interface_box.blockSignals(False)
        self._interface_picked(selected)

    def _interface_picked(self, index):
        adapter = self.interfaces[index] if 0 <= index < len(self.interfaces) else None
        if adapter:
            self.settings.set("interface", adapter)
        self.state.set_adapter(adapter)
        self._state_changed()

    def _families(self, families):
        self.state.families = families
        remembered = self.settings.get("module")
        self.module_box.blockSignals(True)
        self.module_box.clear()
        self.module_box.addItem("Choose a module…")
        selected = 0
        for index, family in enumerate(families, start=1):
            self.module_box.addItem(f"{family['label']}  ·  0x{family['module']:02X}")
            if family["name"] == remembered:
                selected = index
        self.module_box.setCurrentIndex(selected)
        self.module_box.blockSignals(False)
        self._module_picked(selected)

    def _module_picked(self, index):
        family = self.state.families[index - 1] if index > 0 else None
        if family:
            self.settings.set("module", family["name"])
        self.state.set_family(family)

    def _hello(self, info):
        self.state.hello = info
        self.version_label.setText(f"flasher {info.get('flasher_commit') or '?'}\n"
                                   f"Python {info.get('python')} ({info.get('bits')}-bit)")
        self.state.changed.emit()

    def _worker_broken(self, error):
        self.refresh_button.setEnabled(True)
        QMessageBox.critical(self, "PQ35 Flasher",
                             "The background worker couldn't start:\n\n" + error.get("message", "")
                             + "\n\nTry re-running the installer.")

    # --- state ---

    def _state_changed(self):
        state = self.state
        if state.identity:
            self.status.set(f"{state.part()} · {state.identity.get('sw_version', '')}", "ok")
        elif state.adapter and state.family:
            self.status.set("Not identified", "idle")
        else:
            self.status.set("Not connected", "idle")
        has_family = state.family is not None
        enabled = {
            "connect": True, "log": True,
            "backup": bool(state.identity),
            "flash": has_family and state.family.get("can_flash", False),
            "patches": has_family and state.family["name"] in (pages.EPS, pages.HALDEX),
            "diagnostics": bool(state.identity) and has_family
            and bool(set(state.family["operations"]) & {op for op, *_ in pages.DIAGNOSTICS}),
        }
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            on = enabled[item.data(Qt.ItemDataRole.UserRole)]
            flags = item.flags()
            item.setFlags(flags | Qt.ItemFlag.ItemIsEnabled if on else flags & ~Qt.ItemFlag.ItemIsEnabled)
        if not self.nav.currentItem() or not (self.nav.currentItem().flags() & Qt.ItemFlag.ItemIsEnabled):
            self.nav.setCurrentRow(0)

    def _busy_changed(self, busy):
        for widget in (self.interface_box, self.module_box, self.refresh_button):
            widget.setEnabled(not busy)
        self.nav.setEnabled(not busy)
        self.busy_bar.setVisible(busy)
        self.busy_bar.setValue(0)
        if busy:
            self.sleep.hold()
            self.status.set("Working…", "warning")
        else:
            self.sleep.release()
            self._state_changed()

    def closeEvent(self, event):
        if self.state.busy:
            QMessageBox.warning(self, "PQ35 Flasher",
                                "Something is talking to the module right now. Closing would "
                                "interrupt it. Wait for it to finish.")
            event.ignore()
            return
        self.pool.stop()
        event.accept()
