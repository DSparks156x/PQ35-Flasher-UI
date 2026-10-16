import sys

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from . import theme
from .storage import data_dir


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PQ35 Flasher")
    app.setOrganizationName("PQ35")
    theme.apply(app)
    app.styleHints().colorSchemeChanged.connect(lambda *_: theme.apply(app))

    lock = QLockFile(str(data_dir() / "ui.lock"))
    if not lock.tryLock(100):
        QMessageBox.warning(None, "PQ35 Flasher", "PQ35 Flasher is already open.")
        return 1

    from .main_window import MainWindow
    window = MainWindow()
    window.show()
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
