"""Data logger: pick named values, poll their measuring groups, show them live
and write a Datazap-friendly CSV."""
import csv
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLineEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
)

from .pages import Page
from .storage import datalogs_dir, timestamp
from .widgets import Banner, Card, muted

# Per-group read interval: 0 is as fast as the module answers.
RATES = (("As fast as possible", 0), ("10 Hz", 100), ("5 Hz", 200), ("2 Hz", 500), ("1 Hz", 1000))
ROLE = Qt.ItemDataRole.UserRole


def csv_number(field):
    """Datazap wants every cell numeric. Bit fields ("0101") become integers."""
    value = field.get("value")
    if field.get("numeric") and isinstance(value, (int, float)):
        return value
    text = str(field.get("text") or value or "").split(" ")[0]
    if text and set(text) <= {"0", "1"}:
        return int(text, 2)
    try:
        return float(text)
    except ValueError:
        return None


class DatazapWriter:
    """CSV with a "Time" column in seconds and one "Name (unit)" column per
    value. A row is written per group read; values forward-fill between reads.
    The header waits until every group has been read and every column has a
    value, so the columns are final and no cell is empty."""

    def __init__(self, path, columns, groups):
        self.path = path
        self.columns = columns  # [(key, title)]; raw groups add theirs on first read
        self.groups = set(groups)
        self.seen = set()
        self.latest = {}
        self.units = {}
        self.file = None
        self.writer = None
        self.rows = 0

    def update(self, group: int, values: dict, units: dict, seconds: float):
        self.seen.add(group)
        self.latest.update({k: v for k, v in values.items() if v is not None})
        self.units.update({k: u for k, u in units.items() if u})
        if self.writer is None:
            if self.seen < self.groups or not all(key in self.latest for key, _ in self.columns):
                return
            self.file = open(self.path, "w", newline="", encoding="utf-8")
            self.writer = csv.writer(self.file)
            self.writer.writerow(["Time"] + [self._title(key, title) for key, title in self.columns])
        self.writer.writerow([f"{seconds:.3f}"] + [self.latest[key] for key, _ in self.columns])
        self.rows += 1
        if self.rows % 50 == 0:
            self.file.flush()

    def _title(self, key, title):
        unit = self.units.get(key, "")
        return f"{title} ({unit})" if unit else title

    def close(self):
        if self.file:
            self.file.close()


class LoggerPage(Page):
    title = "Data logger"

    def __init__(self, state, parent=None):
        super().__init__(state, parent)
        self.catalog = []
        self.catalog_module = None
        self.raw_groups = set()
        self.running = False
        self.writer = None
        self.rows = {}        # column key -> table row
        self.latest = {}      # column key -> (text, unit)
        self.started = 0.0
        self.samples = 0
        self.errors = 0
        self.add(muted("Reads measuring blocks while you drive. Nothing is written to the module. "
                       "One module at a time."))

        pick = self.add(Card("What to log"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search values (boost, timing, gear…)")
        self.search.textChanged.connect(self._filter)
        pick.add(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Value", "Unit", "Group"])
        self.tree.setMinimumHeight(240)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemChanged.connect(lambda *_: self._selection_changed())
        pick.add(self.tree)
        row = QHBoxLayout()
        row.addWidget(muted("Not listed? Log a whole group:"))
        self.raw_spin = QSpinBox()
        self.raw_spin.setRange(1, 255)
        row.addWidget(self.raw_spin)
        add_raw = QPushButton("Add group")
        add_raw.clicked.connect(self._add_raw)
        row.addWidget(add_raw)
        self.clear_raw = QPushButton("Remove raw groups")
        self.clear_raw.clicked.connect(self._remove_raw)
        row.addWidget(self.clear_raw)
        row.addStretch(1)
        pick.add(row)
        self.summary = pick.add(muted(""))

        run = self.add(Card("Log"))
        row = QHBoxLayout()
        row.addWidget(muted("Rate per group"))
        self.rate = QComboBox()
        for label, interval in RATES:
            self.rate.addItem(label, interval)
        row.addWidget(self.rate)
        self.start_button = QPushButton("Start logging")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.toggle)
        row.addWidget(self.start_button)
        self.open_folder = QPushButton("Open datalogs folder")
        self.open_folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(datalogs_dir()))))
        row.addWidget(self.open_folder)
        row.addStretch(1)
        run.add(row)
        self.status = run.add(muted(""))
        self.banner = run.add(Banner())
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Value", "Reading", "Unit", "Group"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(240)
        run.add(self.table)
        self.finish()

        self.ticker = QTimer(self)
        self.ticker.setInterval(150)  # repaint the table ~7x a second, not per sample
        self.ticker.timeout.connect(self._paint)
        state.sample.connect(self.on_sample)

    # --- module and catalog ---

    def refresh(self):
        family = self.state.family
        module = family["module"] if family and family.get("can_log") else None
        if module != self.catalog_module and not self.running:
            self.catalog_module = module
            self.catalog = []
            self.raw_groups = set()
            self._build_tree()
            if module is not None:
                self.state.offline("catalog", {"module": module}, self._catalog_loaded,
                                   lambda e: self.banner.show_message(self.error_text(e), "error"))
        idle = not self.state.busy or self.running
        self.start_button.setEnabled(bool(module is not None and self.state.adapter) and idle)
        self.tree.setEnabled(not self.running)
        self.rate.setEnabled(not self.running)
        if module is None and not self.running:
            self.banner.show_message("Choose a module that supports logging on the Connect page "
                                     "(engine, transmission or Haldex).", "info")
        elif not self.running and self.banner.property("level") == "info":
            self.banner.hide()

    def _settings_key(self):
        return f"logger:{self.state.family['name']}" if self.state.family else None

    def _catalog_loaded(self, values):
        self.catalog = values or []
        saved = self.state.settings.get(self._settings_key()) or {}
        self.raw_groups = set(saved.get("raw_groups", []))
        self._build_tree(set(saved.get("values", [])))
        if not self.catalog:
            self.summary.setText("No named values for this module in this flasher build. "
                                 "Add whole groups below.")

    def _build_tree(self, checked=()):
        self.tree.blockSignals(True)
        self.tree.clear()
        categories = {}
        for value in self.catalog:
            parent = categories.get(value["category"])
            if parent is None:
                parent = QTreeWidgetItem(self.tree, [value["category"] or "Other"])
                parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsAutoTristate
                                | Qt.ItemFlag.ItemIsUserCheckable)
                categories[value["category"]] = parent
            item = QTreeWidgetItem(parent, [value["name"], value["unit"],
                                            f"{value['group']}.{value['field']}"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if value["id"] in checked
                               else Qt.CheckState.Unchecked)
            item.setData(0, ROLE, value)
        for group in sorted(self.raw_groups):
            item = QTreeWidgetItem(self.tree, [f"Group {group} (all fields, raw)", "", str(group)])
            item.setData(0, ROLE, {"raw_group": group})
        self.tree.blockSignals(False)
        self._filter(self.search.text())
        self._selection_changed()

    def _filter(self, text):
        text = text.strip().lower()
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            category = root.child(i)
            shown = 0
            for j in range(category.childCount()):
                item = category.child(j)
                match = not text or text in item.text(0).lower() or text in category.text(0).lower()
                item.setHidden(not match)
                shown += match
            category.setHidden(category.childCount() > 0 and shown == 0)
            if text and shown:
                category.setExpanded(True)

    def _add_raw(self):
        self.raw_groups.add(self.raw_spin.value())
        self._build_tree(self._checked_ids())

    def _remove_raw(self):
        self.raw_groups = set()
        self._build_tree(self._checked_ids())

    def _checked_values(self):
        values = []
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            category = root.child(i)
            for j in range(category.childCount()):
                item = category.child(j)
                if item.checkState(0) == Qt.CheckState.Checked:
                    values.append(item.data(0, ROLE))
        return values

    def _checked_ids(self):
        return {v["id"] for v in self._checked_values()}

    def _selection_changed(self):
        values = self._checked_values()
        groups = sorted({v["group"] for v in values} | self.raw_groups)
        if values or self.raw_groups:
            self.summary.setText(f"{len(values)} value(s)"
                                 + (f" + {len(self.raw_groups)} raw group(s)" if self.raw_groups else "")
                                 + f" from group(s) {', '.join(map(str, groups))}. Fewer groups "
                                 "log faster.")
        elif self.catalog:
            self.summary.setText("Tick the values you want to log.")
        key = self._settings_key()
        if key and self.catalog:
            self.state.settings.set(key, {"values": sorted(v["id"] for v in values),
                                          "raw_groups": sorted(self.raw_groups)})

    # --- running ---

    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        values = self._checked_values()
        groups = sorted({v["group"] for v in values} | self.raw_groups)
        if not groups:
            self.banner.show_message("Tick at least one value, or add a group.", "warning")
            return
        self.columns = [(f"{v['group']}.{v['field']}", v["name"]) for v in values]
        self.rows = {}
        self.latest = {}
        self.table.setRowCount(0)
        for key, name in self.columns:
            self._add_row(key, name)
        module = self.state.family
        path = datalogs_dir() / f"{module['name']}_{timestamp()}.csv"
        self.writer = DatazapWriter(path, self.columns, groups)
        self.samples = self.errors = 0
        self.started = time.time()
        self.banner.show_message(f"Logging to {path.name}. Press Stop when you're done.", "info")
        self.running = self.state.run(
            "log", {"groups": groups, "interval_ms": self.rate.currentData()},
            self._finished, self._failed)
        if self.running:
            self.start_button.setText("Stop logging")
            self.ticker.start()
        self.refresh()

    def stop(self):
        self.start_button.setEnabled(False)
        self.start_button.setText("Stopping…")
        self.state.stop()

    def _add_row(self, key, name):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, text in enumerate((name, "–", "", key)):
            self.table.setItem(row, column, QTableWidgetItem(text))
        self.rows[key] = row

    def on_sample(self, sample):
        if not self.running:
            return
        self.samples += 1
        if sample.get("error"):
            self.errors += 1
            return
        group = sample["group"]
        values, units = {}, {}
        for field in sample.get("fields") or ():
            key = f"{group}.{field['index']}"
            if group in self.raw_groups and key not in self.rows and self.writer.writer is None:
                # Raw groups get a column per field, named once their first read arrives
                # (always before the CSV header, which waits for every group).
                self.columns.append((key, f"Group {group} field {field['index']}"))
                self._add_row(key, self.columns[-1][1])
            if key in self.rows:
                self.latest[key] = (field.get("text") or str(field.get("value")), field.get("unit", ""))
                values[key] = csv_number(field)
                units[key] = field.get("unit", "")
        self.writer.update(group, values, units, sample.get("wall_time", time.time()) - self.started)

    def _paint(self):
        for key, (text, unit) in self.latest.items():
            row = self.rows.get(key)
            if row is None:
                continue
            reading = text[:-len(unit)].strip() if unit and text.endswith(unit) else text
            self.table.item(row, 1).setText(reading)
            self.table.item(row, 2).setText(unit)
        elapsed = time.time() - self.started
        rate = self.samples / elapsed if elapsed > 0 else 0
        self.status.setText(f"{int(elapsed // 60)}:{int(elapsed % 60):02d} · {self.samples} reads "
                            f"({rate:.1f}/s) · {self.writer.rows if self.writer else 0} CSV rows"
                            + (f" · {self.errors} errors" if self.errors else ""))

    def _end(self):
        self.running = False
        self.ticker.stop()
        self._paint()
        if self.writer:
            self.writer.close()
        self.start_button.setText("Start logging")
        self.refresh()

    def _finished(self, result):
        self._end()
        rows = self.writer.rows if self.writer else 0
        if result.get("error"):
            self.banner.show_message("Logging stopped: " + str(result["error"]), "error")
        elif rows:
            self.banner.show_message(f"Saved {rows} rows to {self.writer.path}", "ok")
        else:
            self.banner.show_message("Stopped. No complete rows yet, so nothing was saved: every "
                                     "value needs at least one reading.", "warning")

    def _failed(self, error):
        self._end()
        self.banner.show_message(self.error_text(error), "error")
