import ctypes
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from . import theme
from .storage import check_writable, data_dir, logs_dir, migrate_old_library


ASSETS = Path(__file__).resolve().parents[1] / "assets"

FOLDER_HELP = {
    "darwin": "On macOS, allow PQ35 Flasher (or Python) under System Settings > Privacy & "
              "Security > Files and Folders > Documents, then press Try again.",
    "win32": "On Windows, Controlled folder access (Windows Security > Virus & threat "
             "protection > Ransomware protection) can block apps from Documents; allow the "
             "app there, then press Try again.",
}


def app_icon() -> QIcon:
    # The .ico carries hand-picked small sizes for Windows; elsewhere the PNG.
    name = "icon.ico" if sys.platform == "win32" else "icon.png"
    return QIcon(str(ASSETS / name))


def crash_file() -> Path:
    """Somewhere a crash report can always be written."""
    for folder in (logs_dir, data_dir):
        try:
            return folder() / f"crash_{time.strftime('%Y%m%d-%H%M%S')}.txt"
        except OSError:
            continue
    return Path(tempfile.gettempdir()) / f"pq35-flasher-crash_{time.strftime('%Y%m%d-%H%M%S')}.txt"


def install_crash_handler():
    """No console behind the app window: show unexpected errors instead of losing them."""
    def hook(kind, value, tb):
        text = "".join(traceback.format_exception(kind, value, tb))
        path = crash_file()
        try:
            path.write_text(text, encoding="utf-8")
            saved = f"Details were saved to:\n{path}"
        except OSError:
            saved = "The details couldn't be saved:\n\n" + text[-1500:]
        sys.__excepthook__(kind, value, tb)
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "PQ35 Flasher",
                                 f"Something went wrong: {value}\n\n{saved}\n\n"
                                 "Include that file if you report this.")
    sys.excepthook = hook


def folders_ok() -> bool:
    """Refuse to start half-broken: say which folder can't be written, and how to fix it."""
    while True:
        problems = check_writable()
        if not problems:
            return True
        box = QMessageBox(QMessageBox.Icon.Critical, "PQ35 Flasher",
                          "PQ35 Flasher can't write to the folders it needs, so it can't keep "
                          "backups, logs or settings:\n\n" + "\n".join(problems)
                          + ("\n\n" + FOLDER_HELP[sys.platform] if sys.platform in FOLDER_HELP else ""))
        retry = box.addButton("Try again", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Quit", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not retry:
            return False


def main():
    if "--demo" in sys.argv:
        # Set before anything reads the data dir; workers inherit it.
        os.environ["PQ35_DEMO"] = "1"
    install_crash_handler()
    if sys.platform == "win32":
        # Group under our own taskbar entry and icon instead of python.exe's.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DSparks156x.PQ35Flasher")
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    app.setApplicationName("PQ35 Flasher")
    app.setOrganizationName("PQ35")
    theme.apply(app)
    app.styleHints().colorSchemeChanged.connect(lambda *_: theme.apply(app))

    if not folders_ok():
        return 1
    lock = QLockFile(str(data_dir() / "ui.lock"))
    if not lock.tryLock(100):
        QMessageBox.warning(None, "PQ35 Flasher", "PQ35 Flasher is already open.")
        return 1

    not_moved = migrate_old_library()
    if not_moved:
        QMessageBox.warning(None, "PQ35 Flasher",
                            "Some module records couldn't be moved to Documents/PQ35 Flasher/"
                            "Modules. They're still in the old folder:\n\n" + "\n".join(not_moved))
    from .main_window import MainWindow
    window = MainWindow()
    window.show()

    def acknowledge():
        from .disclaimer import confirm
        if not confirm(window):
            window.close()

    # Every start: the disclaimer over the dimmed window, once it has painted.
    QTimer.singleShot(150, acknowledge)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
