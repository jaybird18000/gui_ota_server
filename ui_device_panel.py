"""
gui_ota_server - ui_device_panel.py
Device list: every device that has contacted the OTA server, with status badges.
"""

from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QLineEdit, QAbstractItemView, QFileDialog,
)
from PyQt6.QtCore import Qt, pyqtSlot, pyqtSignal, QTimer
from PyQt6.QtGui import QColor
from server_controller import ServerController

_STATUS_COLOURS = {"active": "#a6e3a1", "idle": "#f9e2af", "offline": "#f38ba8"}
_COLUMNS = ["Device ID", "Device IP", "Status", "Requests", "Up Time"]


class DevicePanel(QWidget):
    DEFAULT_HEALTH_TIMEOUT_SEC = 60
    list_cleared = pyqtSignal()  # emitted when the user clears the device list

    def __init__(self, controller: ServerController, parent=None):
        super().__init__(parent)
        self._ctrl = controller
        self._row_map: dict[str, QTableWidgetItem] = {}
        self._build_ui()
        self._connect_signals()

        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(10_000)
        self._stale_timer.timeout.connect(self._mark_stale_devices)
        self._stale_timer.start()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        toolbar = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search devices…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self._search_edit, 1)

        self._lbl_count = QLabel("0 devices")
        self._lbl_count.setStyleSheet("color: #585b70;")
        toolbar.addWidget(self._lbl_count)

        self._btn_clear = QPushButton("🗑  Clear List")
        self._btn_clear.clicked.connect(self._clear_devices)
        toolbar.addWidget(self._btn_clear)

        self._btn_export = QPushButton("💾  Export CSV")
        self._btn_export.clicked.connect(self._export_csv)
        toolbar.addWidget(self._btn_export)
        root.addLayout(toolbar)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setSortingEnabled(True)

        hdr = self._table.horizontalHeader()
        for col in range(4):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self._table, 1)

        summary = QHBoxLayout()
        self._lbl_active  = _BadgeLabel("Active",  "#a6e3a1")
        self._lbl_idle    = _BadgeLabel("Idle",    "#f9e2af")
        self._lbl_offline = _BadgeLabel("Offline", "#f38ba8")
        for w in (self._lbl_active, self._lbl_idle, self._lbl_offline):
            summary.addWidget(w)
        summary.addStretch()
        root.addLayout(summary)

    def _connect_signals(self):
        self._ctrl.device_connected.connect(self._on_device_connected)
        self._ctrl.device_updated.connect(self._on_device_updated)
        self._ctrl.device_removed.connect(self._on_device_removed)

    @pyqtSlot(dict)
    def _on_device_connected(self, device: dict):
        existing = self._row_map.get(device["id"])
        if existing is not None:
            self._populate_row(existing.row(), device)
            self._refresh_counts()
            return

        self._table.setSortingEnabled(False)
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._populate_row(row, device)
        self._row_map[device["id"]] = self._table.item(row, 0)
        self._table.setSortingEnabled(True)
        self._refresh_counts()

    @pyqtSlot(dict)
    def _on_device_updated(self, device: dict):
        item = self._row_map.get(device["id"])
        if item is None:
            self._on_device_connected(device)
            return
        row = item.row()
        self._table.setSortingEnabled(False)
        self._populate_row(row, device)
        self._row_map[device["id"]] = self._table.item(row, 0)
        self._table.setSortingEnabled(True)
        self._refresh_counts()

    @pyqtSlot(str)
    def _on_device_removed(self, device_id: str):
        item = self._row_map.pop(device_id, None)
        if item is None:
            return
        self._table.removeRow(item.row())
        self._refresh_counts()

    def _populate_row(self, row: int, device: dict):
        status = device.get("status", "active")
        colour = QColor(_STATUS_COLOURS.get(status, "#cdd6f4"))

        def _item(text):
            it = QTableWidgetItem(str(text))
            it.setForeground(colour)
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            return it

        self._table.setItem(row, 0, _item(device.get("id", "?")))
        self._table.setItem(row, 1, _item(device.get("ip", "")))

        st_item = _item(status.upper())
        st_item.setForeground(QColor(_STATUS_COLOURS.get(status, "#cdd6f4")))
        self._table.setItem(row, 2, st_item)

        self._table.setItem(row, 3, _item(str(device.get("requests", 0))))
        self._table.setItem(
            row, 4, _item(self._uptime(device.get("active_since", ""), status))
        )

        details = (
            f"First seen: {device.get('first_seen', '')}\n"
            f"Last seen: {device.get('last_seen', '')}\n"
            f"Last event: {device.get('last_event', '')}"
        )
        for column in range(self._table.columnCount()):
            self._table.item(row, column).setToolTip(details)

    @staticmethod
    def _uptime(active_since: str, status: str) -> str:
        if status == "offline":
            return "00:00:00"
        try:
            started = datetime.strptime(active_since, "%H:%M:%S")
            now = datetime.now()
            started = started.replace(year=now.year, month=now.month, day=now.day)
            elapsed = max(0, int((now - started).total_seconds()))
            return f"{elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}"
        except (TypeError, ValueError):
            return "--:--:--"

    def _refresh_counts(self):
        total = self._table.rowCount()
        self._lbl_count.setText(f"{total} device{'s' if total != 1 else ''}")
        active = idle = offline = 0
        for row in range(total):
            it = self._table.item(row, 2)
            if it:
                s = it.text().lower()
                if s == "active":   active  += 1
                elif s == "idle":   idle    += 1
                else:               offline += 1
        self._lbl_active.set_count(active)
        self._lbl_idle.set_count(idle)
        self._lbl_offline.set_count(offline)

    def _apply_filter(self, text: str):
        text = text.lower().strip()
        for row in range(self._table.rowCount()):
            match = any(
                text in (self._table.item(row, col) or QTableWidgetItem("")).text().lower()
                for col in range(self._table.columnCount())
            )
            self._table.setRowHidden(row, not match if text else False)

    def _mark_stale_devices(self):
        now = datetime.now()
        for dev_id, item in self._row_map.items():
            row = item.row()
            last_it = self._table.item(row, 0)
            if not last_it:
                continue
            device = self._ctrl.devices.get(dev_id, {})
            last_seen = device.get("last_seen", "")
            if not last_seen:
                continue
            try:
                ls = datetime.strptime(last_seen, "%H:%M:%S").replace(
                    year=now.year, month=now.month, day=now.day)
                elapsed = (now - ls).total_seconds()
                timeout_sec = max(
                    1, device.get("health_timeout_ms", self.DEFAULT_HEALTH_TIMEOUT_SEC * 1000)
                ) / 1000
                new_st = "offline" if elapsed >= timeout_sec else \
                         "idle"    if elapsed >= timeout_sec / 2 else None
                if new_st:
                    device["status"] = new_st
                    st_it = self._table.item(row, 2)
                    if st_it and st_it.text().lower() != new_st:
                        st_it.setText(new_st.upper())
                        st_it.setForeground(QColor(_STATUS_COLOURS[new_st]))
                        self._ctrl.device_updated.emit(device)
                    uptime_it = self._table.item(row, 4)
                    if uptime_it:
                        uptime_it.setText(self._uptime(device.get("active_since", ""), new_st))
            except (ValueError, TypeError):
                pass
        self._refresh_counts()

    def _clear_devices(self):
        self._table.setRowCount(0)
        self._row_map.clear()
        self._refresh_counts()
        self.list_cleared.emit()

    def _export_csv(self):
        import os
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Device List",
            os.path.expanduser(f"~/ota_devices_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"),
            "CSV Files (*.csv);;All Files (*)",
        )
        if not path: return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(",".join(_COLUMNS) + "\n")
                for row in range(self._table.rowCount()):
                    cells = [f'"{(self._table.item(row,c) or QTableWidgetItem("")).text()}"'
                             for c in range(self._table.columnCount())]
                    f.write(",".join(cells) + "\n")
        except Exception as exc:
            print(f"[ERROR] CSV export: {exc}")


class _BadgeLabel(QWidget):
    def __init__(self, label: str, colour: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 12, 0)
        layout.setSpacing(4)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {colour}; font-size: 10px;")
        layout.addWidget(dot)
        self._lbl = QLabel(f"{label}: 0")
        self._lbl.setStyleSheet("color: #cdd6f4; font-size: 12px;")
        layout.addWidget(self._lbl)
        self._label = label

    def set_count(self, n: int):
        self._lbl.setText(f"{self._label}: {n}")