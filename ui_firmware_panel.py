"""Firmware update status and per-device update actions."""

import json
import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QAbstractItemView,
)
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QColor
from server_controller import ServerController


_STATUS_COLOURS = {
    "active": "#a6e3a1",
    "idle": "#f9e2af",
    "offline": "#f38ba8",
    "update requested": "#89b4fa",
}


class FirmwarePanel(QWidget):
    _COLUMNS = ["Device ID", "Status", "Current Version", "New Version", "Update"]

    def __init__(self, controller: ServerController, parent=None):
        super().__init__(parent)
        self._ctrl = controller
        self._rows: dict[str, int] = {}
        self._new_versions = self._load_versions()
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self._table = QTableWidget(0, len(self._COLUMNS))
        self._table.setHorizontalHeaderLabels(self._COLUMNS)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        for column in range(len(self._COLUMNS) - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table)

    def _connect_signals(self):
        self._ctrl.device_connected.connect(self._upsert_device)
        self._ctrl.device_updated.connect(self._upsert_device)
        self._ctrl.device_removed.connect(self._remove_device)

    @staticmethod
    def _load_versions():
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.json")
        try:
            with open(path, "r", encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
            return {
                name: str(details.get("latest", metadata.get("latest_version", "Unknown")))
                for name, details in metadata.get("versions", {}).items()
            }
        except (OSError, ValueError, TypeError):
            return {}

    @pyqtSlot(dict)
    def _upsert_device(self, device: dict):
        device_id = device.get("id", "?")
        row = self._rows.get(device_id)
        if row is None:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._rows[device_id] = row

        device_type = device.get("type", "")
        new_version = self._new_versions.get(
            device_type,
            self._new_versions.get(f"r33-{device_type}", "Unknown"),
        )
        values = [
            device_id,
            device.get("status", "active").upper(),
            device.get("current_version", "Unknown"),
            new_version,
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if column == 1:
                status = str(value).lower()
                item.setForeground(QColor(_STATUS_COLOURS.get(status, "#cdd6f4")))
            self._table.setItem(row, column, item)

        button = QPushButton("Update")
        button.clicked.connect(lambda checked=False, current_id=device_id: self._request_update(current_id))
        self._table.setCellWidget(row, 4, button)

    @pyqtSlot(str)
    def _remove_device(self, device_id: str):
        row = self._rows.pop(device_id, None)
        if row is None:
            return
        self._table.removeRow(row)
        self._rows = {
            self._table.item(index, 0).text(): index
            for index in range(self._table.rowCount())
            if self._table.item(index, 0)
        }

    def _request_update(self, device_id: str):
        device = self._ctrl.devices.get(device_id, {})
        device["update_status"] = "Update requested"
        row = self._rows.get(device_id)
        if row is not None:
            status_item = self._table.item(row, 1)
            status_item.setText("UPDATE REQUESTED")
            status_item.setForeground(QColor(_STATUS_COLOURS["update requested"]))
        self._ctrl.log_message.emit(
            f"[FIRMWARE] Update requested for {device_id}; device must initiate the OTA download."
        )