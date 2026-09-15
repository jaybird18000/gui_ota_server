"""
gui_ota_server - ui_mainwindow.py
Main application window.
"""

import os
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTabWidget, QStatusBar, QLabel,
)
from PyQt6.QtCore import Qt, QTimer, QSettings
from PyQt6.QtGui import QCloseEvent

from server_controller import ServerController
from ui_status_panel import StatusPanel
from ui_log_panel import LogPanel
from ui_config_panel import ConfigPanel


class MainWindow(QMainWindow):
    APP_TITLE = "OTA Firmware Server"
    MIN_W, MIN_H = 1100, 720

    def __init__(self):
        super().__init__()
        self._controller = ServerController(
            log_callback=self.append_log
        )
        self._settings = QSettings("r33_master_slave", "gui_ota_server")
        self._build_ui()
        self._connect_signals()
        self._restore_geometry()

        self._blink_state = False
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(600)
        self._blink_timer.timeout.connect(self._blink_heartbeat)
        self._update_title_bar("stopped")

    def append_log(self, line: str):
        """Append a log line to the GUI log panel."""
        self._log_panel.append_line(line)

    def update_status(self, running: bool):
        """Update server status indicator."""
        if running:
            self.status_label.setText("Server Running")
            self.status_label.setStyleSheet("color: green;")
        else:
            self.status_label.setText("Server Stopped")
            self.status_label.setStyleSheet("color: red;")

    def _build_ui(self):
        self.setWindowTitle(self.APP_TITLE)
        self.setMinimumSize(self.MIN_W, self.MIN_H)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._status_panel = StatusPanel(self._controller, self)
        root.addWidget(self._status_panel)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)

        self._config_panel = ConfigPanel(self._controller, self)
        self._tabs.addTab(self._config_panel, "⚙  Configuration")

        from ui_device_panel import DevicePanel
        self._device_panel = DevicePanel(self._controller, self)
        self._tabs.addTab(self._device_panel, "📡  Devices")

        from ui_firmware_panel import FirmwarePanel
        self._firmware_panel = FirmwarePanel(self._controller, self)
        self._tabs.addTab(self._firmware_panel, "⬆  Firmware Update")

        splitter.addWidget(self._tabs)

        self._log_panel = LogPanel(self)
        splitter.addWidget(self._log_panel)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

        sb = QStatusBar()
        self.setStatusBar(sb)

        self._sb_status = QLabel("● Stopped")
        self._sb_status.setObjectName("statusIndicator")
        self._sb_status.setProperty("status", "stopped")

        self._sb_uptime   = QLabel("Uptime: --:--:--")
        self._sb_requests = QLabel("Requests: 0")
        self._sb_bytes    = QLabel("Served: 0 B")
        self._sb_script   = QLabel(
            f"Script: {os.path.basename(self._controller.server_script)}"
        )
        self._sb_script.setToolTip(self._controller.server_script)
        self._heartbeat = QLabel("♥")
        self._heartbeat.setStyleSheet("color: #45475a;")

        for w in (self._sb_status, self._sb_uptime,
                  self._sb_requests, self._sb_bytes):
            sb.addWidget(w)
            sb.addWidget(_sep())

        sb.addPermanentWidget(self._sb_script)
        sb.addPermanentWidget(self._heartbeat)

    def _connect_signals(self):
        ctrl = self._controller
        ctrl.log_message.connect(self._log_panel.append_line)
        ctrl.status_changed.connect(self._on_status_changed)
        ctrl.stats_updated.connect(self._on_stats_updated)
        ctrl.device_connected.connect(self._log_panel.append_device_event)

    def _on_status_changed(self, status: str):
        self._update_title_bar(status)
        labels = {
            "stopped":  ("● Stopped",  "stopped",  False),
            "stopping": ("◌ Stopping", "starting", True),
            "starting": ("◌ Starting", "starting", True),
            "running":  ("● Running",  "running",  True),
            "error":    ("✖ Error",    "stopped",  False),
        }
        text, prop, blink = labels.get(status, ("● Unknown", "stopped", False))
        self._sb_status.setText(text)
        self._sb_status.setProperty("status", prop)
        self._sb_status.style().unpolish(self._sb_status)
        self._sb_status.style().polish(self._sb_status)
        if blink:
            self._blink_timer.start()
        else:
            self._blink_timer.stop()
            self._heartbeat.setStyleSheet("color: #45475a;")

    def _on_stats_updated(self, stats: dict):
        self._sb_uptime.setText(f"Uptime: {stats.get('uptime') or '--:--:--'}")
        self._sb_requests.setText(f"Requests: {stats.get('requests', 0)}")
        b = stats.get("bytes_served", 0)
        bs = f"{b/1_048_576:.1f} MB" if b >= 1_048_576 else \
             f"{b/1024:.1f} KB"      if b >= 1024       else f"{b} B"
        self._sb_bytes.setText(f"Served: {bs}")

    def _blink_heartbeat(self):
        self._blink_state = not self._blink_state
        color = "#a6e3a1" if self._blink_state else "#45475a"
        self._heartbeat.setStyleSheet(f"color: {color};")

    def _update_title_bar(self, status: str):
        icon = {"running": "🟢", "starting": "🟡",
                "stopped": "🔴", "error": "❌"}.get(status, "🔴")
        self.setWindowTitle(f"{icon} {self.APP_TITLE} — {status.capitalize()}")

    def _restore_geometry(self):
        geom = self._settings.value("mainwindow/geometry")
        if geom:
            self.restoreGeometry(geom)
        else:
            self.resize(1280, 800)

    def closeEvent(self, event: QCloseEvent):
        if self._controller.status == "running":
            self._controller.stop()
        self._settings.setValue("mainwindow/geometry", self.saveGeometry())
        super().closeEvent(event)


def _sep() -> QLabel:
    lbl = QLabel("|")
    lbl.setStyleSheet("color: #45475a; padding: 0 4px;")
    return lbl