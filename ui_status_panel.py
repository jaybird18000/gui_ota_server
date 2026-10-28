"""
gui_ota_server - ui_status_panel.py
Top strip: server state indicator, start/stop/restart buttons, metric tiles.
"""

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QFrame, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QFont

from server_controller import ServerController


class StatusPanel(QWidget):
    def __init__(self, controller: ServerController, parent=None):
        super().__init__(parent)
        self._ctrl = controller
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(12)

        state_frame = QFrame()
        state_frame.setFrameShape(QFrame.Shape.StyledPanel)
        state_frame.setStyleSheet(
            "QFrame { background:#181825; border:1px solid #313244; border-radius:6px; }"
        )
        sf_layout = QHBoxLayout(state_frame)
        sf_layout.setContentsMargins(12, 6, 12, 6)
        sf_layout.setSpacing(8)

        self._indicator = QLabel("🔴  STOPPED")
        f = QFont(); f.setPointSize(14); f.setBold(True)
        self._indicator.setFont(f)
        self._indicator.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._state_sub = QLabel("Server is idle")
        self._state_sub.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._state_sub.setStyleSheet("color: #585b70; font-size: 11px;")

        sf_layout.addWidget(self._indicator)
        sf_layout.addWidget(self._state_sub)
        layout.addWidget(state_frame)

        self._tile_uptime   = _MetricTile("Uptime",      "--:--:--")
        self._tile_requests = _MetricTile("Requests",    "0")
        self._tile_served   = _MetricTile("Data Served", "0 B")
        self._tile_devices  = _MetricTile("Devices",     "0")

        for tile in (self._tile_uptime, self._tile_requests,
                     self._tile_served, self._tile_devices):
            layout.addWidget(tile)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        self._btn_start = QPushButton("▶  Start Server")
        self._btn_start.setObjectName("startButton")
        self._btn_start.setMinimumWidth(140)
        self._btn_start.clicked.connect(self._on_start)

        self._btn_stop = QPushButton("■  Stop Server")
        self._btn_stop.setObjectName("stopButton")
        self._btn_stop.setMinimumWidth(140)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)

        self._btn_restart = QPushButton("↺  Restart")
        self._btn_restart.setMinimumWidth(140)
        self._btn_restart.setEnabled(False)
        self._btn_restart.clicked.connect(self._on_restart)

        btn_layout.addWidget(self._btn_start)
        btn_layout.addWidget(self._btn_stop)
        btn_layout.addWidget(self._btn_restart)
        layout.addLayout(btn_layout)

    def _connect_signals(self):
        self._ctrl.status_changed.connect(self._on_status_changed)
        self._ctrl.stats_updated.connect(self._on_stats_updated)

    @pyqtSlot(str)
    def _on_status_changed(self, status: str):
        cfg = {
            "stopped":  ("🔴  STOPPED",  "Server is idle",   True,  False, False),
            "stopping": ("🟡  STOPPING", "Shutting down",    False, False, False),
            "starting": ("🟡  STARTING", "Launching…",       False, False, False),
            "running":  ("🟢  RUNNING",  "Serving firmware", False, True,  True),
            "error":    ("❌  ERROR",    "Check logs",       True,  False, False),
        }
        icon_text, sub, se, pe, re_ = cfg.get(
            status, ("🔴  STOPPED", "Server is idle", True, False, False)
        )
        self._indicator.setText(icon_text)
        self._state_sub.setText(sub)
        self._btn_start.setEnabled(se)
        self._btn_stop.setEnabled(pe)
        self._btn_restart.setEnabled(re_)

    @pyqtSlot(dict)
    def _on_stats_updated(self, stats: dict):
        self._tile_uptime.set_value(stats.get("uptime") or "--:--:--")
        self._tile_requests.set_value(str(stats.get("requests", 0)))
        self._tile_devices.set_value(str(stats.get("device_count", 0)))
        b = stats.get("bytes_served", 0)
        bs = f"{b/1_048_576:.1f} MB" if b >= 1_048_576 else \
             f"{b/1024:.1f} KB"      if b >= 1024       else f"{b} B"
        self._tile_served.set_value(bs)

    def _on_start(self):
        mw = self.window()
        if hasattr(mw, "_config_panel"):
            cfg = mw._config_panel.get_config()
            # Remove unsupported arguments
            cfg2 = {
                "python_exe": cfg["python_exe"],
                "script_path": cfg["script_path"],
                "host": cfg["host"],
                "port": cfg["port"],
                "firmware_dir": cfg.get("firmware_dir") or None,
            }
            self._ctrl.start(**cfg2)
        else:
            self._ctrl.start()

    def _on_stop(self):
        self._ctrl.stop()

    def _on_restart(self):
        mw = self.window()
        if hasattr(mw, "_config_panel"):
            cfg = mw._config_panel.get_config()
            # Remove unsupported arguments
            cfg2 = {
                "python_exe": cfg["python_exe"],
                "script_path": cfg["script_path"],
                "host": cfg["host"],
                "port": cfg["port"],
                "firmware_dir": cfg.get("firmware_dir") or None,
            }
            self._ctrl.restart(**cfg2)
        else:
            self._ctrl.restart()


class _MetricTile(QFrame):
    def __init__(self, title: str, value: str, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame { background:#181825; border:1px solid #313244; border-radius:6px; }"
        )
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(2)

        self._title_lbl = QLabel(title)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setStyleSheet("color: #585b70; font-size: 11px;")

        self._value_lbl = QLabel(value)
        self._value_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont(); f.setPointSize(16); f.setBold(True)
        self._value_lbl.setFont(f)
        self._value_lbl.setStyleSheet("color: #89b4fa;")

        layout.addWidget(self._title_lbl)
        layout.addWidget(self._value_lbl)

    def set_value(self, text: str):
        self._value_lbl.setText(text)