import os
import sys

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from . import theme
from .storage import data_dir, migrate_old_library


def main():
    if "--demo" in sys.argv:
        # Set before anything reads the data dir; workers inherit it.
        os.environ["PQ35_DEMO"] = "1"
    app = QApplication(sys.argv)
    app.setApplicationName("PQ35 Flasher")
    app.setOrganizationName("PQ35")
    theme.apply(app)
    app.styleHints().colorSchemeChanged.connect(lambda *_: theme.apply(app))

    lock = QLockFile(str(data_dir() / "ui.lock"))
    if not lock.tryLock(100):
        QMessageBox.warning(None, "PQ35 Flasher", "PQ35 Flasher is already open.")
        return 1

    migrate_old_library()
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
