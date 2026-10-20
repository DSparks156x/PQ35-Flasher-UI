"""Choose EPS patches for an image: 0x5D configuration patches and the 0x5E HCA gains.

The dialog only collects a selection; the caller applies it to the source image
(``eps_apply_patches``), so the source file is never changed.
"""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QRadioButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from .widgets import Card, muted

GAIN_PRESETS = {"hca-gain-140": 140, "hca-gain-160": 160}
TITLES = {
    "hca-timeout-minspeed": "HCA timeout and minimum speed",
    "slew-up-10": "Slew up 10: rise step 1 → 10, matching the fall step",
    "slew-20": "Slew up/down 20: rise and fall steps 1/10 → 20/20",
}
MODES = 3


def _q7(value):
    return f"{value / 128:.2f}×"


def _gain_text(gains):
    """'all 140', '88, 113, 155' (every mode), or one triple per mode."""
    values = {g for row in gains for g in row}
    if len(values) == 1:
        return f"all {values.pop()}"
    if all(row == gains[0] for row in gains):
        return ", ".join(map(str, gains[0]))
    return " / ".join(",".join(map(str, row)) for row in gains)


class PatchDialog(QDialog):
    """``options`` is the worker's eps_patch_options result for the source image."""

    def __init__(self, options, parent=None):
        super().__init__(parent)
        self.options = options
        self.setWindowTitle("Patch EPS image")
        self.setModal(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 18)
        # The body scrolls when the screen is too short for it; the buttons don't.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.body = QWidget()
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll)
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(24, 20, 24, 6)
        layout.setSpacing(12)
        title = QLabel("Patch EPS image")
        title.setObjectName("h1")
        title.setMinimumWidth(572)
        layout.addWidget(title)
        firmware = options.get("firmware")
        layout.addWidget(muted(
            f"{options['name']}" + (f" · {firmware['part']} {firmware['version']}" if firmware else "")
            + ". Patches are applied to this file and saved as a new copy; the file itself is "
              "never changed."))

        current = options.get("current") or {}
        now = current.get("known_patches") or {}
        self.checks = {}
        self.slew_group = None
        self.slew_buttons = {}
        config = [f for f in options["features"]
                  if f["feature"] not in GAIN_PRESETS and f["manifest"]]
        if config:
            card = Card("0x5D configuration")
            layout.addWidget(card)
            groups = {}
            for feature in config:
                if feature["group"]:
                    groups.setdefault(tuple(feature["group"]), []).append(feature)
                    continue
                box = QCheckBox(TITLES.get(feature["feature"], feature["description"]))
                box.setToolTip(feature["description"])
                self._fit(box, feature, now)
                card.add(box)
                self.checks[feature["feature"]] = box
            for members in groups.values():
                self.slew_group = QButtonGroup(self)
                none = QRadioButton("Stock slew (no change)")
                none.setChecked(True)
                self.slew_group.addButton(none)
                card.add(none)
                self.slew_buttons[None] = none
                for feature in members:
                    button = QRadioButton(TITLES.get(feature["feature"], feature["description"]))
                    button.setToolTip(feature["description"])
                    self.slew_group.addButton(button)
                    self._fit(button, feature, now)
                    card.add(button)
                    self.slew_buttons[feature["feature"]] = button
                if any(f["status"] == "applied" for f in members):
                    # A slew patch is already in the source; the others don't fit on top.
                    for button in self.slew_buttons.values():
                        button.setEnabled(False)
                elif not any(b.isChecked() for f, b in self.slew_buttons.items() if f):
                    none.setChecked(True)
        self.gains = options.get("gains")
        self.gain_mode = None
        if self.gains:
            self._build_gains(layout, current.get("gains") or self.gains)
        if not config and not self.gains:
            layout.addWidget(muted("No known patch fits this image."))

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.apply_button = QPushButton("Apply patches")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self.accept)
        self.apply_button.setEnabled(bool(config or self.gains))
        row.setContentsMargins(24, 0, 24, 0)
        row.addWidget(cancel)
        row.addWidget(self.apply_button)
        outer.addLayout(row)
        self._size_to_content()

    def _size_to_content(self):
        """Size to the content, up to 90% of the screen's height (then it scrolls)."""
        hint = self.body.sizeHint()
        screen = (self.screen() or self.parentWidget() and self.parentWidget().screen())
        tallest = int(screen.availableGeometry().height() * 0.9) if screen else hint.height() + 70
        self.resize(hint.width() + self.scroll.verticalScrollBar().sizeHint().width(),
                    min(hint.height() + 70, tallest))

    @staticmethod
    def _fit(button, feature, now):
        """Enable a patch that fits; tick the ones already in the source or in
        the image the user has open now."""
        status = feature["status"]
        if status == "applied":
            button.setChecked(True)
            button.setEnabled(False)
            button.setText(button.text() + " (already in this image)")
        elif status == "mismatch":
            button.setEnabled(False)
            button.setToolTip("This image's bytes don't match the patch's stock values.")
        else:
            button.setChecked(now.get(feature["manifest"]) == "applied")

    def _build_gains(self, layout, current):
        card = Card("HCA gain (0x5E dataset)")
        layout.addWidget(card)
        maximum = self.options.get("gain_max", 160)
        speeds = self.options.get("gain_speeds") or [0, 50, 120]
        source = self.gains
        card.add(muted(
            f"HCA torque is scaled by these gains at {', '.join(str(s) for s in speeds)} km/h, "
            "linearly in between (128 = 1.0×). They only affect HCA. The rack uses one of the "
            "three modes; which one isn't mapped yet, so set all three unless you know."))
        self.gain_mode = QButtonGroup(self)
        self.gain_mode.buttonToggled.connect(lambda *_: self._gain_mode_changed())
        self.gain_buttons = {"keep": QRadioButton(f"Keep the image's gains ({_gain_text(source)})")}
        for key, value in GAIN_PRESETS.items():
            self.gain_buttons[key] = QRadioButton(f"All {value} ({_q7(value)})")
        self.gain_buttons["custom"] = QRadioButton("Custom")
        for button in self.gain_buttons.values():
            self.gain_mode.addButton(button)
            card.add(button)

        self.custom = QWidget()
        grid = QGridLayout(self.custom)
        grid.setContentsMargins(28, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        for column, speed in enumerate(speeds):
            grid.addWidget(muted(f"{speed} km/h"), 0, column + 1)
        self.spins = []
        for mode in range(MODES):
            grid.addWidget(QLabel(f"Mode {mode}"), mode + 1, 0)
            row = []
            for column in range(len(speeds)):
                spin = QSpinBox()
                spin.setRange(0, maximum)
                spin.setValue(current[mode][column])
                spin.valueChanged.connect(lambda *_: self._mirror())
                grid.addWidget(spin, mode + 1, column + 1)
                row.append(spin)
            self.spins.append(row)
        self.same = QCheckBox("Same gains for all modes")
        self.same.setChecked(all(row == current[0] for row in current))
        self.same.toggled.connect(lambda *_: self._mirror())
        grid.addWidget(self.same, MODES + 1, 0, 1, len(speeds) + 1)
        self.scale = muted("")
        grid.addWidget(self.scale, MODES + 2, 0, 1, len(speeds) + 1)
        # Keep the rows apart when the dialog is short and the body scrolls.
        self.custom.setMinimumHeight(self.custom.minimumSizeHint().height())
        card.add(self.custom)
        card.add(muted(
            f"Capped at {maximum}: at the largest HCA input, {maximum} reaches 3.75 Nm, just under "
            "the rack's 3.8 Nm torque monitor. Tripping that monitor latches a fault until the "
            "power is cycled."))

        if current == source:
            self.gain_buttons["keep"].setChecked(True)
        else:
            preset = next((k for k, v in GAIN_PRESETS.items()
                           if all(g == v for row in current for g in row)), None)
            self.gain_buttons[preset or "custom"].setChecked(True)
        self._mirror()
        self._gain_mode_changed()

    def _mirror(self):
        if not self.gains:
            return
        same = self.same.isChecked()
        for mode, row in enumerate(self.spins):
            for column, spin in enumerate(row):
                if mode and same:
                    spin.blockSignals(True)
                    spin.setValue(self.spins[0][column].value())
                    spin.blockSignals(False)
                spin.setEnabled(not (mode and same))
        self.scale.setText("Mode 0: " + ", ".join(_q7(s.value()) for s in self.spins[0]))

    def _gain_mode_changed(self):
        self.custom.setVisible(self.gain_buttons["custom"].isChecked())
        QTimer.singleShot(0, self._size_to_content)

    def selection(self):
        """(features, gains): the patches to apply to the source image. gains is
        None when the image's own gains are kept."""
        features = [f for f, box in self.checks.items() if box.isEnabled() and box.isChecked()]
        if self.slew_group:
            chosen = next((f for f, b in self.slew_buttons.items() if b.isChecked()), None)
            if chosen and self.slew_buttons[chosen].isEnabled():
                features.append(chosen)
        gains = None
        if self.gain_mode:
            checked = next(k for k, b in self.gain_buttons.items() if b.isChecked())
            if checked in GAIN_PRESETS:
                features.append(checked)
            elif checked == "custom":
                gains = [[spin.value() for spin in row] for row in self.spins]
                if gains == self.gains:
                    gains = None
        return features, gains

    @staticmethod
    def describe(features, gains):
        """A short summary of a selection, for banners and file names."""
        parts = [TITLES.get(f, f).split(":")[0] for f in features if f not in GAIN_PRESETS]
        parts += [f"HCA gain {GAIN_PRESETS[f]}" for f in features if f in GAIN_PRESETS]
        if gains:
            parts.append(f"HCA gains {_gain_text(gains)}")
        return ", ".join(parts)
