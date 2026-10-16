"""Small reusable widgets."""
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)

from . import theme


def restyle(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Card(QFrame):
    def __init__(self, title: str = None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(8)
        if title:
            label = QLabel(title)
            label.setStyleSheet("font-weight: 600;")
            self.body.addWidget(label)

    def add(self, item):
        if isinstance(item, QLayout):
            self.body.addLayout(item)
        else:
            self.body.addWidget(item)
        return item


class Badge(QLabel):
    def __init__(self, text="", level="idle", parent=None):
        super().__init__(text, parent)
        self.setProperty("level", level)

    def set(self, text, level):
        self.setText(text)
        self.setProperty("level", level)
        restyle(self)


class Banner(QLabel):
    """A full-width message. Hidden when empty."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("banner")
        self.setWordWrap(True)
        self.setContentsMargins(12, 10, 12, 10)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.active = False
        self.hide()

    def show_message(self, text, level="info"):
        self.active = bool(text)
        if not text:
            self.hide()
            return
        self.setText(text)
        self.setProperty("level", level)
        restyle(self)
        self.show()


class FlowRow(QWidget):
    """A row of badges that wraps onto new lines."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(6)
        self.items = []

    def clear(self):
        while self.layout_.count():
            row = self.layout_.takeAt(0)
            if row.layout():
                while row.layout().count():
                    child = row.layout().takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
        self.items = []

    def set_badges(self, badges, per_row=4):
        self.clear()
        row = None
        for index, (text, level, tooltip) in enumerate(badges):
            if index % per_row == 0:
                row = QHBoxLayout()
                row.setSpacing(6)
                self.layout_.addLayout(row)
            badge = Badge(text, level)
            if tooltip:
                badge.setToolTip(tooltip)
            row.addWidget(badge)
            self.items.append(badge)
        if row is not None:
            row.addStretch(1)


class HoldButton(QPushButton):
    """Must be held down for `seconds` to fire. Releasing early cancels."""
    confirmed = Signal()

    def __init__(self, text, seconds=3.0, parent=None):
        super().__init__(text, parent)
        self.setObjectName("primary")
        self.seconds = seconds
        self.base_text = text
        self.fraction = 0.0
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._tick)
        self.setMinimumHeight(44)

    def mousePressEvent(self, event):
        if self.isEnabled() and event.button() == Qt.MouseButton.LeftButton:
            self.clock.start()
            self.timer.start()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._reset()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):  # Space/Enter must not bypass the hold
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return
        super().keyPressEvent(event)

    def _reset(self):
        self.timer.stop()
        self.fraction = 0.0
        self.setText(self.base_text)
        self.update()

    def _tick(self):
        self.fraction = min(1.0, self.clock.elapsed() / (self.seconds * 1000))
        left = max(0.0, self.seconds * (1 - self.fraction))
        self.setText(f"Keep holding… {left:.1f} s")
        self.update()
        if self.fraction >= 1.0:
            self._reset()
            self.setDown(False)
            self.confirmed.emit()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.fraction > 0:
            painter = QPainter(self)
            color = QColor("#ffffff")
            color.setAlpha(70)
            painter.fillRect(0, 0, int(self.width() * self.fraction), self.height(), color)
            painter.end()


class DropZone(QFrame):
    """Drag a .bin here or click to browse."""
    picked = Signal(str)

    def __init__(self, prompt="Drop a .bin file here, or click to choose", parent=None):
        super().__init__(parent)
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(74)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        self.title = QLabel(prompt)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setStyleSheet("font-weight: 600;")
        self.detail = QLabel("")
        self.detail.setObjectName("muted")
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.title)
        layout.addWidget(self.detail)
        self.start_dir = str(Path.home())

    def set_file(self, name, detail):
        self.title.setText(name)
        self.detail.setText(detail)

    def mouseReleaseEvent(self, event):
        path, _ = QFileDialog.getOpenFileName(self, "Choose firmware image", self.start_dir,
                                              "Firmware images (*.bin);;All files (*)")
        if path:
            self.start_dir = str(Path(path).parent)
            self.picked.emit(path)

    def _hover(self, on):
        self.setProperty("hover", "true" if on else "false")
        restyle(self)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._hover(True)

    def dragLeaveEvent(self, event):
        self._hover(False)

    def dropEvent(self, event):
        self._hover(False)
        urls = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.picked.emit(urls[0])


def muted(text) -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(True)
    return label


def h1(text) -> QLabel:
    label = QLabel(text)
    label.setObjectName("h1")
    return label


def kib(size: int) -> str:
    return f"{size / 1024:.0f} KiB" if size >= 1024 else f"{size} B"


def colors():
    return theme.colors()
