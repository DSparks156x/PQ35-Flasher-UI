"""Fault codes (DTCs) for modules that support them: read, and clear on request."""
from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem

from .widgets import Banner, Card, muted


class FaultCodesCard(Card):
    def __init__(self, state, page):
        super().__init__("Fault codes")
        self.state = state
        self.page = page
        self.add(muted("Reading is read-only. Clearing deletes the stored codes (and their "
                       "freeze frames) on the module."))
        row = QHBoxLayout()
        self.read_button = QPushButton("Read fault codes")
        self.read_button.setObjectName("primary")
        self.read_button.clicked.connect(lambda: self.run("dtc"))
        self.clear_button = QPushButton("Clear fault codes…")
        self.clear_button.clicked.connect(self.clear)
        row.addWidget(self.read_button)
        row.addWidget(self.clear_button)
        row.addStretch(1)
        self.add(row)
        self.banner = self.add(Banner())
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Code", "Description / status"])
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.setMinimumHeight(140)
        self.add(self.tree)

    def refresh(self):
        ready = bool(self.state.identity) and not self.state.busy
        self.read_button.setEnabled(ready)
        self.clear_button.setEnabled(ready)

    def clear(self):
        label = self.state.family["label"] if self.state.family else "module"
        answer = QMessageBox.question(
            self, "Clear fault codes",
            f"Clear all stored fault codes on the {label}? This also deletes their freeze-frame "
            "data, so read and note them first if you need them.")
        if answer == QMessageBox.StandardButton.Yes:
            self.run("dtc_clear")

    def run(self, operation):
        self.tree.clear()
        self.banner.show_message("Clearing…" if operation == "dtc_clear" else "Reading…", "info")
        self.state.run(operation, {}, lambda r: self._done(operation, r),
                       lambda e: self.banner.show_message(self.page.error_text(e), "error"))

    def _done(self, operation, result):
        if result.get("error"):
            self.banner.show_message(str(result["error"]), "error")
            return
        report = result.get("result") or {}
        codes = report.get("codes") or []
        for code in codes:
            QTreeWidgetItem(self.tree, [str(code.get("code_text") or code.get("code")),
                                        str(code.get("status_text") or f"status 0x{code.get('status', 0):02X}")])
        cleared = (report.get("clear") or {}).get("cleared")
        if operation == "dtc_clear" and not cleared:
            self.banner.show_message("The module didn't confirm the clear. Codes read back below.",
                                     "warning")
        elif codes and cleared:
            self.banner.show_message(f"Cleared, but {len(codes)} code(s) came straight back "
                                     "(the fault is still present).", "warning")
        elif codes:
            self.banner.show_message(f"{len(codes)} fault code(s) stored.", "warning")
        else:
            self.banner.show_message("Cleared. No fault codes stored." if cleared
                                     else "No fault codes stored.", "ok")
