"""The disclaimer shown on every start, and the patcher warning, over a dimmed main window."""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

TEXT = """
<p><b>You are at your own risk using this project.</b> I am not responsible for thermonuclear
war, broken timing chains, divorce, etc.</p>

<p><b>A failed flash means whatever you flashed will not be functional until it is
recovered.</b> This means no EPS, no Haldex, no whatever.</p>

<p>Take faint care when flashing. This tool can usually recover from an interrupted flash, but
that doesn't mean you want to end up in that scenario. Flashing a steer dataset takes seconds,
but full module flashes can take several minutes: <b>a battery tender or otherwise is
recommended</b>, and turn off high-draw items (blower, headlights, heated seats).
<b>The engine cannot be running.</b></p>

<p>There is no warranty or anything provided, and I cannot help you recover your shit without
logs and useful information (exact component information).</p>

<p><b>Do not Dumb</b>, and have fun.</p>
"""

PATCH_TEXT = """
<p>The patches here really do what they say they do. <b>That does not make them safe. At all.</b></p>

<p><b>The EPS is faster than you. The EPS is stronger than you.</b> Usually it just cuts EPS
should you sufficiently piss it off, but <b>it is physically capable of much worse.</b></p>

<p><b>Do not fuck with it without knowing what you are doing.</b> HCA speed/timeout is the
only "safe" patch here.</p>
"""


class Dimmer(QWidget):
    """Darkens a window while a dialog needs an answer, and follows its size."""

    def __init__(self, window):
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: rgba(0, 0, 0, 150);")
        self.setGeometry(window.rect())
        window.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        return False


class Disclaimer(QDialog):
    def __init__(self, parent=None, title="Before you start", text=TEXT, decline="Quit",
                 accept="I understand"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(14)
        heading = QLabel(title)
        heading.setObjectName("h1")
        layout.addWidget(heading)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setStyleSheet("line-height: 140%;")
        layout.addWidget(body)
        row = QHBoxLayout()
        row.addStretch(1)
        quit_button = QPushButton(decline)
        quit_button.clicked.connect(self.reject)
        agree = QPushButton(accept)
        agree.setObjectName("primary")
        agree.clicked.connect(self.accept)
        row.addWidget(quit_button)
        row.addWidget(agree)
        layout.addLayout(row)
        agree.setFocus()


def confirm(window, **dialog) -> bool:
    """Show the disclaimer (or another one, via Disclaimer's arguments) over a
    dimmed window. True if acknowledged."""
    dimmer = Dimmer(window)
    dimmer.show()
    dimmer.raise_()
    try:
        return Disclaimer(window, **dialog).exec() == QDialog.DialogCode.Accepted
    finally:
        dimmer.deleteLater()


_patch_warning_seen = False


def confirm_patching(widget) -> bool:
    """The patcher warning, once per session, before the patcher first opens."""
    global _patch_warning_seen
    if _patch_warning_seen:
        return True
    _patch_warning_seen = confirm(widget.window(), title="Before you patch", text=PATCH_TEXT,
                                  decline="Cancel")
    return _patch_warning_seen
