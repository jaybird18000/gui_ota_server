"""Firmware update status and per-device update actions."""

import json
import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QAbstractItemView, QLabel, QGroupBox,
    QGridLayout, QProgressBar,
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

        status_group = QGroupBox("Firmware Update Status")
        status_layout = QGridLayout(status_group)
        self._status_device = QLabel("No update selected")
        self._command_status = QLabel("--")
        self._update_status = QLabel("--")
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("%p% transmitted")
        status_layout.addWidget(QLabel("Device:"), 0, 0)
        status_layout.addWidget(self._status_device, 0, 1)
        status_layout.addWidget(QLabel("Command Status:"), 1, 0)
        status_layout.addWidget(self._command_status, 1, 1)
        status_layout.addWidget(QLabel("Update Status:"), 2, 0)
        status_layout.addWidget(self._update_status, 2, 1)
        status_layout.addWidget(self._progress, 3, 0, 1, 2)
        layout.addWidget(status_group)

    def _connect_signals(self):
        self._ctrl.device_connected.connect(self._upsert_device)
        self._ctrl.device_updated.connect(self._upsert_device)
        self._ctrl.device_removed.connect(self._remove_device)
        self._ctrl.command_status.connect(self._show_command_status)
        self._ctrl.firmware_progress.connect(self._show_firmware_progress)

    @staticmethod
    def _load_versions():
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.json")
        try:
            with open(path, "r", encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
            return {
                name: str(details.get("latest", "Unknown"))
                for name, details in metadata.get("versions", {}).items()
                if isinstance(details, dict)
            }
        except (OSError, ValueError, TypeError):
            return {}

    def _new_version_for(self, device: dict) -> str:
        device_id = str(device.get("id", ""))
        device_type = str(device.get("type", ""))
        for metadata_name in (device_id, device_type, f"r33-{device_type}"):
            if metadata_name in self._new_versions:
                return self._new_versions[metadata_name]
        for metadata_name in sorted(self._new_versions, key=len, reverse=True):
            if device_id.startswith(f"{metadata_name}-"):
                return self._new_versions[metadata_name]
        return "Unknown"

    @pyqtSlot(dict)
    def _upsert_device(self, device: dict):
        device_id = device.get("id", "?")
        device_status = str(device.get("status", "active")).lower()
        row = self._rows.get(device_id)
        if row is None:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._rows[device_id] = row

        values = [
            device_id,
            device_status.upper(),
            device.get("current_version", "Unknown"),
            self._new_version_for(device),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if column == 1:
                status = str(value).lower()
                item.setForeground(QColor(_STATUS_COLOURS.get(status, "#cdd6f4")))
            self._table.setItem(row, column, item)

        button = QPushButton("Update")
        button.setEnabled(device_status not in {"idle", "offline"})
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

    def clear_table(self):
        """Clear the firmware update device list (mirrors the Devices tab's Clear List action)."""
        self._table.setRowCount(0)
        self._rows.clear()

    def _request_update(self, device_id: str):
        if str(self._ctrl.devices.get(device_id, {}).get("status", "active")).lower() in {
            "idle", "offline"
        }:
            return
        self._status_device.setText(device_id)
        self._command_status.setText("pending")
        self._update_status.setText("waiting")
        self._progress.setValue(0)
        self._ctrl.request_firmware_update(device_id)

    @pyqtSlot(str, str)
    def _show_command_status(self, device_id: str, status: str):
        normalized = status.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in {"accepted", "command_accepted", "update_accepted"}:
            self._status_device.setText(device_id)
            self._command_status.setText("accepted")
        elif normalized in {"rejected", "command_rejected", "update_rejected"}:
            self._status_device.setText(device_id)
            self._command_status.setText("rejected")
        elif normalized in {"duplicate", "command_duplicate", "update_duplicate"}:
            self._status_device.setText(device_id)
            self._command_status.setText("duplicate")
        elif normalized in {"started", "update_started", "firmware_started"}:
            self._status_device.setText(device_id)
            self._update_status.setText("started")
        elif normalized in {
            "completed", "complete", "update_complete", "update_completed", "firmware_completed",
            "success", "successful",
        }:
            self._status_device.setText(device_id)
            self._update_status.setText("completed")
            self._progress.setValue(100)
        elif normalized.startswith("command_sent"):
            self._status_device.setText(device_id)
            self._command_status.setText("accepted")
        elif normalized.startswith(("server_is_not_running", "update_request_failed")):
            self._status_device.setText(device_id)
            self._command_status.setText("rejected")

    @pyqtSlot(str, int)
    def _show_firmware_progress(self, device_id: str, percentage: int):
        self._status_device.setText(device_id)
        progress = max(0, min(100, percentage))
        self._progress.setValue(progress)
        if progress >= 100:
            self._update_status.setText("completed")
        elif progress > 0 and self._update_status.text() == "waiting":
            self._update_status.setText("started")