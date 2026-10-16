"""Top bar (interface menu + module chip), left navigation, page stack."""
import ctypes
import os
import shutil
import sys

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QProgressBar, QPushButton, QStackedWidget, QToolButton, QVBoxLayout,
    QWidget,
)

from . import pages, updates
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
        self.demo = bool(os.environ.get("PQ35_DEMO"))
        self.setWindowTitle("PQ35 Flasher (demo mode)" if self.demo else "PQ35 Flasher")
        self.resize(1040, 760)
        self.settings = Settings()
        self.pool = WorkerPool(self.settings, self)
        self.state = AppState(self.pool, self.settings, self)
        self.sleep = SleepBlocker()
        self.interfaces = []
        self.listing = False

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
        footer = QVBoxLayout()
        footer.setContentsMargins(12, 0, 10, 0)
        footer.setSpacing(4)
        self.local_version = updates.local_version()
        commit = (self.local_version.get("commit") or "unknown")[:7]
        self.version_label = QLabel(f"Version {self.local_version['version']} · {commit}")
        self.version_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.runtime_label = muted("")
        self.update_status = muted("")
        self.update_button = QPushButton("Check for updates")
        self.update_button.clicked.connect(self._update_clicked)
        for widget in (self.version_label, self.runtime_label, self.update_status, self.update_button):
            footer.addWidget(widget)
        nav_layout.addLayout(footer)
        self.update_info = None
        self.checker = updates.UpdateChecker(self)
        self.checker.finished.connect(self._update_checked)
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
        self.state.refresh_interfaces.connect(self.refresh_interfaces)
        self.state.start_recovery.connect(lambda: self.nav.setCurrentRow(2))

        native = self.pool.native()
        native.call("hello", {}, self._hello, self._worker_broken)
        native.call("families", {}, self._families, self._worker_broken)
        # Pick up adapters as they're plugged in or unplugged.
        self.listing = False
        self.refresh_interfaces()
        self.rescan = QTimer(self)
        self.rescan.setInterval(4000)
        self.rescan.timeout.connect(self._auto_refresh)
        self.rescan.start()

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
        if os.environ.get("PQ35_DEMO"):
            layout.addWidget(Badge("DEMO: no hardware", "info"))
        layout.addStretch(1)
        # Interface: rarely changed, so it lives in a menu.
        self.interface_button = QToolButton()
        self.interface_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.interface_button.setToolTip("CAN interface")
        self.interface_menu = QMenu(self)
        self.interface_button.setMenu(self.interface_menu)
        self.interface_group = QActionGroup(self)
        layout.addWidget(self.interface_button)
        # Module: shows what's selected; click to change it on the Connect page.
        self.module_button = QPushButton("Choose module")
        self.module_button.setToolTip("Change module")
        self.module_button.clicked.connect(lambda: self.nav.setCurrentRow(0))
        layout.addWidget(self.module_button)
        self.status = Badge("Not connected", "idle")
        layout.addWidget(self.status)
        return bar

    def refresh_interfaces(self):
        if self.listing:
            return
        self.listing = True
        self.pool.native().call("list_interfaces", {}, self._interfaces, self._listing_failed)

    def _auto_refresh(self):
        if not self.state.busy and self.isActiveWindow():
            self.refresh_interfaces()

    def _listing_failed(self, error):
        self.listing = False
        self._worker_broken(error)

    def _interfaces(self, interfaces):
        self.listing = False
        if interfaces == self.interfaces and self.interface_menu.actions():
            return
        self.interfaces = interfaces
        current = self.state.adapter or self.settings.get("interface")
        selected = next((i for i, a in enumerate(interfaces) if self.state.same_adapter(a, current)),
                        0 if interfaces else -1)
        self.interface_menu.clear()
        for action in self.interface_group.actions():
            self.interface_group.removeAction(action)
        for index, interface in enumerate(interfaces):
            action = QAction(interface["label"], self, checkable=True)
            action.setChecked(index == selected)
            action.triggered.connect(lambda _=False, i=index: self._interface_picked(i))
            self.interface_group.addAction(action)
            self.interface_menu.addAction(action)
        if not interfaces:
            none = self.interface_menu.addAction("No interface found")
            none.setEnabled(False)
        self.interface_menu.addSeparator()
        self.interface_menu.addAction("Look for interfaces again", self.refresh_interfaces)
        self._interface_picked(selected)

    def _interface_picked(self, index):
        adapter = self.interfaces[index] if 0 <= index < len(self.interfaces) else None
        if adapter:
            self.settings.set("interface", adapter)
        self.state.set_adapter(adapter)
        self.interface_button.setText((adapter["label"] if adapter else "No interface") + "  ▾")
        self._state_changed()

    def _families(self, families):
        self.state.families = families
        remembered = self.state.family_by_name(self.settings.get("module"))
        if remembered:
            self.state.select_module(remembered)

    def _hello(self, info):
        self.state.hello = info
        self.runtime_label.setText(f"flasher {(info.get('flasher_commit') or '?')[:7]} · "
                                   f"Py {info.get('python')}")
        self.state.changed.emit()

    def _worker_broken(self, error):
        if getattr(self, "worker_error_shown", False):
            return
        self.worker_error_shown = True
        QMessageBox.critical(self, "PQ35 Flasher",
                             "The background worker couldn't start:\n\n" + error.get("message", "")
                             + "\n\nTry re-running the installer.")

    # --- state ---

    def _state_changed(self):
        state = self.state
        self.module_button.setText((state.family["label"] if state.family else "Choose module") + "  ▾")
        if state.identity:
            self.status.set(f"{state.part()} · {state.identity.get('sw_version', '')}", "ok")
        elif state.adapter and state.family:
            self.status.set("Not identified", "idle")
        else:
            self.status.set("Not connected", "idle")
        has_family = state.family is not None
        enabled = {
            "connect": True, "log": True,
            "backup": bool(state.identity) and state.family["name"] in pages.BACKUP_FAMILIES,
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

    # --- updates ---

    def _update_clicked(self):
        info = self.update_info or {}
        if info.get("status") in ("behind", "unknown_local"):
            self._run_update(info)
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("Checking…")
        self.update_status.setText("")
        self.checker.start(self.local_version)

    def _update_checked(self, info):
        self.update_info = info
        self.update_button.setEnabled(not self.state.busy)
        self.update_button.setObjectName("")
        status = info.get("status")
        if status == "behind":
            count = info.get("behind_by") or len(info.get("changes", []))
            self.update_status.setText(f"{count} update{'s' if count != 1 else ''} available")
            self.update_button.setText("Update now")
            self.update_button.setObjectName("primary")
        elif status == "unknown_local":
            self.update_status.setText("Can't tell which version this is.")
            self.update_button.setText("Update now")
        elif status == "identical":
            self.update_status.setText("Up to date")
            self.update_button.setText("Check again")
        elif status in ("ahead", "local_only"):
            self.update_status.setText("Development version (newer than GitHub)")
            self.update_button.setText("Check again")
        elif status == "diverged":
            self.update_status.setText("Local changes. Update from a terminal.")
            self.update_button.setText("Check again")
        else:
            self.update_status.setText("Couldn't reach GitHub.")
            self.update_button.setToolTip(info.get("error", ""))
            self.update_button.setText("Try again")
        self.update_button.style().unpolish(self.update_button)
        self.update_button.style().polish(self.update_button)

    def _run_update(self, info):
        if self.state.busy:
            QMessageBox.warning(self, "Update", "Wait for the current operation to finish first.")
            return
        changes = info.get("changes") or []
        listing = "\n".join(f"• {c}" for c in changes[:12])
        if len(changes) > 12:
            listing += f"\n• …and {len(changes) - 12} more"
        answer = QMessageBox.question(
            self, "Update PQ35 Flasher",
            "PQ35 Flasher will close, update and reopen."
            + (f"\n\nWhat's new:\n{listing}" if listing else ""))
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            note = updates.launch_update()
        except OSError as exc:
            QMessageBox.critical(self, "Update", f"Couldn't start the updater: {exc}")
            return
        self.update_status.setText(note)
        self.close()

    def _busy_changed(self, busy):
        self.update_button.setEnabled(not busy)
        for widget in (self.interface_button, self.module_button):
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
