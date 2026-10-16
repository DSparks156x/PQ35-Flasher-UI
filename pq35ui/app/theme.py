"""Flat light/dark theme that follows the OS setting."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

LIGHT = {
    "bg": "#f5f5f3", "surface": "#ffffff", "surface2": "#f0efeb", "border": "#dedcd5",
    "text": "#1f1f1d", "muted": "#6b6a65", "accent": "#2f6fd6", "accent_bg": "#e6f0fc",
    "ok": "#2f6d12", "ok_bg": "#e8f3dc", "warn": "#7a4a06", "warn_bg": "#fbefd9",
    "err": "#a32d2d", "err_bg": "#fcebeb", "info": "#3c3489", "info_bg": "#eeedfe",
}
DARK = {
    "bg": "#1b1b1a", "surface": "#242423", "surface2": "#2d2d2b", "border": "#3a3a37",
    "text": "#ecebe6", "muted": "#a3a19a", "accent": "#5b9cf0", "accent_bg": "#1d3352",
    "ok": "#9fd172", "ok_bg": "#25391a", "warn": "#f0c069", "warn_bg": "#45320f",
    "err": "#f09595", "err_bg": "#4a1d1d", "info": "#c3bff5", "info_bg": "#2e2a5c",
}


def is_dark() -> bool:
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorScheme"):
        return hints.colorScheme() == Qt.ColorScheme.Dark
    return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def colors() -> dict:
    return DARK if is_dark() else LIGHT


def apply(app):
    c = colors()
    app.setStyle("Fusion")
    palette = QPalette()
    for role, key in (
            (QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "surface"),
            (QPalette.ColorRole.AlternateBase, "surface2"), (QPalette.ColorRole.Button, "surface"),
            (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.WindowText, "text"),
            (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "accent"),
            (QPalette.ColorRole.ToolTipBase, "surface"), (QPalette.ColorRole.ToolTipText, "text"),
            (QPalette.ColorRole.PlaceholderText, "muted")):
        palette.setColor(role, QColor(c[key]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setStyleSheet(STYLE.format(**c))


STYLE = """
QWidget {{ font-size: 10pt; }}
QMainWindow, #page {{ background: {bg}; }}
#topbar {{ background: {surface}; border-bottom: 1px solid {border}; }}
#nav {{ background: {surface}; border-right: 1px solid {border}; }}
#title {{ font-size: 12pt; font-weight: 600; }}
#h1 {{ font-size: 15pt; font-weight: 600; }}
#muted, QLabel[muted="true"] {{ color: {muted}; }}
#card {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; }}
#card[selected="true"] {{ border: 2px solid {accent}; }}
#drop {{ background: {surface}; border: 2px dashed {border}; border-radius: 10px; }}
#drop[hover="true"] {{ border-color: {accent}; background: {accent_bg}; }}
QComboBox, QLineEdit, QSpinBox {{ background: {surface}; border: 1px solid {border};
    border-radius: 6px; padding: 5px 8px; min-height: 20px; }}
QPushButton {{ background: {surface}; border: 1px solid {border}; border-radius: 6px;
    padding: 6px 14px; }}
QPushButton:hover {{ background: {surface2}; }}
QToolButton {{ background: {surface}; border: 1px solid {border}; border-radius: 6px;
    padding: 6px 12px; }}
QToolButton:hover {{ background: {surface2}; }}
QPushButton#chip {{ border-radius: 6px; padding: 6px 12px; }}
QPushButton#chip[level="ok"] {{ background: {ok_bg}; color: {ok}; border: 1px solid {ok_bg}; }}
QPushButton#chip[level="idle"] {{ background: {surface2}; color: {muted}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QPushButton:disabled {{ color: {muted}; }}
QPushButton#primary {{ background: {accent}; color: white; border: none; font-weight: 600; }}
QPushButton#primary:disabled {{ background: {surface2}; color: {muted}; }}
QPushButton#danger {{ color: {err}; border-color: {err}; }}
QListWidget#navlist {{ background: transparent; border: none; outline: none; }}
QListWidget#navlist::item {{ padding: 8px 10px; border-radius: 6px; margin: 1px 6px; }}
QListWidget#navlist::item:selected {{ background: {accent_bg}; color: {accent}; }}
QListWidget#navlist::item:disabled {{ color: {muted}; }}
QLabel[level="ok"] {{ background: {ok_bg}; color: {ok}; border-radius: 6px; padding: 3px 9px; }}
QLabel[level="warning"] {{ background: {warn_bg}; color: {warn}; border-radius: 6px; padding: 3px 9px; }}
QLabel[level="error"] {{ background: {err_bg}; color: {err}; border-radius: 6px; padding: 3px 9px; }}
QLabel[level="info"] {{ background: {info_bg}; color: {info}; border-radius: 6px; padding: 3px 9px; }}
QLabel[level="idle"] {{ background: {surface2}; color: {muted}; border-radius: 6px; padding: 3px 9px; }}
#banner[level="error"] {{ background: {err_bg}; color: {err}; border-radius: 8px; }}
#banner[level="warning"] {{ background: {warn_bg}; color: {warn}; border-radius: 8px; }}
#banner[level="ok"] {{ background: {ok_bg}; color: {ok}; border-radius: 8px; }}
#banner[level="info"] {{ background: {accent_bg}; color: {accent}; border-radius: 8px; }}
QRadioButton#option {{ background: {surface}; border: 1px solid {border}; border-radius: 8px; padding: 9px; }}
QRadioButton#option:checked {{ border: 2px solid {accent}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px;
    border: 1px solid {muted}; background: {surface}; border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {accent};
    border: 1px solid {accent}; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {border}; }}
QProgressBar {{ background: {surface2}; border: none; border-radius: 6px; height: 12px;
    text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 6px; }}
QPlainTextEdit, QTreeWidget {{ background: {surface}; border: 1px solid {border}; border-radius: 8px; }}
QPlainTextEdit {{ font-family: Consolas, "DejaVu Sans Mono", monospace; font-size: 9pt; }}
QScrollArea {{ border: none; background: transparent; }}
QGroupBox {{ border: 1px solid {border}; border-radius: 8px; margin-top: 14px; padding: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {muted}; }}
"""
