"""
gui_ota_server - ui_config_panel.py
Configuration tab: host/port, firmware directory, script path, flags, presets.
"""

import json
import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QSpinBox,
    QPushButton, QCheckBox, QFileDialog, QComboBox,
)
from PyQt6.QtCore import QSettings, pyqtSlot
from PyQt6.QtNetwork import QAbstractSocket, QNetworkInterface
from server_controller import ServerController

# Prefer a real (non-Microsoft-Store) interpreter, since Store Python can't
# accept inbound LAN connections needed for OTA/mDNS to reach the ESP32s.
_DEFAULT_PYTHON_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"),
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BUILDS_CONFIG_FILE = os.path.join(SCRIPT_DIR, "builds_config.json")


def _default_python_exe() -> str:
    for candidate in _DEFAULT_PYTHON_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return "python"


def _load_builds_dir_config() -> str:
    try:
        with open(BUILDS_CONFIG_FILE, "r") as f:
            return json.load(f).get("builds_dir", "")
    except (OSError, ValueError):
        return ""


def _save_builds_dir_config(builds_dir: str):
    try:
        with open(BUILDS_CONFIG_FILE, "w") as f:
            json.dump({"builds_dir": builds_dir}, f, indent=2)
    except OSError:
        pass


class ConfigPanel(QWidget):
    def __init__(self, controller: ServerController, parent=None):
        super().__init__(parent)
        self._ctrl = controller
        self._settings = QSettings("r33_master_slave", "gui_ota_server")
        self._build_ui()
        self._load_settings()
        self._connect_signals()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        # Network
        net_group = QGroupBox("Network")
        net_form = QFormLayout(net_group)
        net_form.setContentsMargins(10, 16, 10, 10)
        net_form.setSpacing(8)
        self._host_combo = QComboBox()
        self._host_combo.setToolTip("Select the local interface the OTA server should listen on")
        self._populate_host_options()
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(8080)
        net_form.addRow("Host:", self._host_combo)
        net_form.addRow("Port:", self._port_spin)
        root.addWidget(net_group)

        # Paths
        path_group = QGroupBox("Paths")
        path_layout = QVBoxLayout(path_group)
        path_layout.setContentsMargins(10, 16, 10, 10)
        path_layout.setSpacing(8)

        py_row = QHBoxLayout()
        self._python_edit = QLineEdit()
        self._python_edit.setPlaceholderText("Path to python.exe…")
        self._btn_python_browse = QPushButton("…")
        self._btn_python_browse.setMaximumWidth(36)
        self._btn_python_browse.clicked.connect(self._browse_python)
        py_row.addWidget(QLabel("Python Exe:"))
        py_row.addWidget(self._python_edit, 1)
        py_row.addWidget(self._btn_python_browse)

        fw_row = QHBoxLayout()
        self._fw_dir_edit = QLineEdit()
        self._fw_dir_edit.setPlaceholderText("Directory containing .bin firmware files…")
        self._btn_fw_browse = QPushButton("…")
        self._btn_fw_browse.setMaximumWidth(36)
        self._btn_fw_browse.clicked.connect(self._browse_fw_dir)
        fw_row.addWidget(QLabel("Firmware Dir:"))
        fw_row.addWidget(self._fw_dir_edit, 1)
        fw_row.addWidget(self._btn_fw_browse)

        sc_row = QHBoxLayout()
        self._script_edit = QLineEdit()
        self._script_edit.setPlaceholderText("Path to firmware_server.py…")
        self._btn_script_browse = QPushButton("…")
        self._btn_script_browse.setMaximumWidth(36)
        self._btn_script_browse.clicked.connect(self._browse_script)
        sc_row.addWidget(QLabel("Server Script:"))
        sc_row.addWidget(self._script_edit, 1)
        sc_row.addWidget(self._btn_script_browse)

        path_layout.addLayout(py_row)
        path_layout.addLayout(fw_row)
        path_layout.addLayout(sc_row)
        root.addWidget(path_group)

        # Advanced
        adv_group = QGroupBox("Advanced")
        adv_layout = QVBoxLayout(adv_group)
        adv_layout.setContentsMargins(10, 16, 10, 10)
        adv_layout.setSpacing(8)
        self._chk_debug  = QCheckBox("Enable debug / verbose logging (--debug)")
        self._chk_no_auth = QCheckBox("Disable authentication (--no-auth)")
        extra_row = QHBoxLayout()
        extra_row.addWidget(QLabel("Extra args:"))
        self._extra_args_edit = QLineEdit()
        self._extra_args_edit.setPlaceholderText("--foo bar  (space-separated)")
        extra_row.addWidget(self._extra_args_edit, 1)
        adv_layout.addWidget(self._chk_debug)
        adv_layout.addWidget(self._chk_no_auth)
        adv_layout.addLayout(extra_row)
        root.addWidget(adv_group)

        # Presets
        preset_group = QGroupBox("Presets")
        preset_layout = QHBoxLayout(preset_group)
        preset_layout.setContentsMargins(10, 16, 10, 10)
        self._preset_combo = QComboBox()
        self._preset_combo.addItems([
            "Development (localhost:8080)",
            "LAN Server (0.0.0.0:8080)",
            "Production (0.0.0.0:80)",
        ])
        self._btn_apply_preset = QPushButton("Apply")
        self._btn_apply_preset.clicked.connect(self._apply_preset)
        preset_layout.addWidget(QLabel("Quick preset:"))
        preset_layout.addWidget(self._preset_combo, 1)
        preset_layout.addWidget(self._btn_apply_preset)
        root.addWidget(preset_group)

        # Buttons
        btn_row = QHBoxLayout()
        self._btn_save  = QPushButton("💾  Save Settings")
        self._btn_save.clicked.connect(self._save_settings)
        self._btn_reset = QPushButton("↺  Reset Defaults")
        self._btn_reset.clicked.connect(self._reset_defaults)
        btn_row.addWidget(self._btn_save)
        btn_row.addWidget(self._btn_reset)
        btn_row.addStretch()
        root.addLayout(btn_row)
        root.addStretch()

        default_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "firmware_server.py"
        )
        self._script_edit.setText(default_script)
        self._python_edit.setText(_default_python_exe())

    def get_config(self) -> dict:
        extra: list[str] = []
        if self._chk_debug.isChecked():   extra.append("--debug")
        if self._chk_no_auth.isChecked(): extra.append("--no-auth")
        raw = self._extra_args_edit.text().strip()
        if raw: extra += raw.split()

        script = self._script_edit.text().strip()
        if script and os.path.isfile(script):
            try: self._ctrl.server_script = script
            except RuntimeError: pass

        python_exe = self._python_edit.text().strip() or _default_python_exe()

        return {
            "python_exe":   python_exe,
            "script_path":  script,                     # REQUIRED
            "host":         self._host_combo.currentData() or "0.0.0.0",
            "port":         self._port_spin.value(),
            "firmware_dir": self._fw_dir_edit.text().strip(),
            "extra_args":   extra or None,
        }

    def _connect_signals(self):
        self._ctrl.status_changed.connect(self._on_status_changed)

    @pyqtSlot(str)
    def _on_status_changed(self, status: str):
        enabled = status == "stopped"
        for w in (self._host_combo, self._port_spin, self._python_edit, self._fw_dir_edit,
                  self._script_edit, self._btn_python_browse, self._btn_fw_browse, self._btn_script_browse,
                  self._chk_debug, self._chk_no_auth, self._extra_args_edit,
                  self._btn_apply_preset, self._btn_reset):
            w.setEnabled(enabled)

    def _browse_python(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select python.exe",
            os.path.dirname(self._python_edit.text()) or os.path.expanduser("~"),
            "Python Executable (python.exe);;All Files (*)"
        )
        if path: self._python_edit.setText(path)

    def _browse_fw_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, "Firmware Directory", SCRIPT_DIR
        )
        if path:
            self._fw_dir_edit.setText(path)
            _save_builds_dir_config(path)

    def _browse_script(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select firmware_server.py",
            os.path.dirname(self._script_edit.text()) or os.path.expanduser("~"),
            "Python Files (*.py);;All Files (*)"
        )
        if path: self._script_edit.setText(path)

    def _apply_preset(self):
        presets = [("127.0.0.1", 8080), ("0.0.0.0", 8080), ("0.0.0.0", 80)]
        host, port = presets[self._preset_combo.currentIndex()]
        self._select_host(host)
        self._port_spin.setValue(port)

    def _populate_host_options(self):
        self._host_combo.clear()
        self._host_combo.addItem("All interfaces (0.0.0.0)", "0.0.0.0")

        interfaces = []
        for interface in QNetworkInterface.allInterfaces():
            flags = interface.flags()
            if not (flags & QNetworkInterface.InterfaceFlag.IsUp):
                continue
            if not (flags & QNetworkInterface.InterfaceFlag.IsRunning):
                continue
            if flags & QNetworkInterface.InterfaceFlag.IsLoopBack:
                continue

            for entry in interface.addressEntries():
                address = entry.ip()
                if address.protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                    continue
                interfaces.append((interface.humanReadableName(), address.toString(), interface.name()))

        for human_name, address, interface_name in sorted(interfaces, key=lambda item: (item[0].lower(), item[1])):
            label = human_name or interface_name or "Network interface"
            self._host_combo.addItem(f"{label} ({address})", address)

    def _select_host(self, host):
        index = self._host_combo.findData(host)
        if index >= 0:
            self._host_combo.setCurrentIndex(index)

    def _save_settings(self):
        s = self._settings
        s.setValue("config/host",         self._host_combo.currentData() or "0.0.0.0")
        s.setValue("config/port",         self._port_spin.value())
        s.setValue("config/python_exe",   self._python_edit.text())
        s.setValue("config/firmware_dir", self._fw_dir_edit.text())
        s.setValue("config/script",       self._script_edit.text())
        s.setValue("config/debug",        self._chk_debug.isChecked())
        s.setValue("config/no_auth",      self._chk_no_auth.isChecked())
        s.setValue("config/extra_args",   self._extra_args_edit.text())
        _save_builds_dir_config(self._fw_dir_edit.text())

    def _load_settings(self):
        s = self._settings
        self._select_host(s.value("config/host", "0.0.0.0"))
        self._port_spin.setValue(int(s.value("config/port", 8080)))
        self._python_edit.setText(s.value("config/python_exe", "") or _default_python_exe())
        self._fw_dir_edit.setText(_load_builds_dir_config() or s.value("config/firmware_dir", ""))
        saved = s.value("config/script", "")
        if saved: self._script_edit.setText(saved)
        self._chk_debug.setChecked(s.value("config/debug", False, type=bool))
        self._chk_no_auth.setChecked(s.value("config/no_auth", False, type=bool))
        self._extra_args_edit.setText(s.value("config/extra_args", ""))

    def _reset_defaults(self):
        self._select_host("0.0.0.0")
        self._port_spin.setValue(8080)
        self._python_edit.setText(_default_python_exe())
        self._fw_dir_edit.setText("")
        self._chk_debug.setChecked(False)
        self._chk_no_auth.setChecked(False)
        self._extra_args_edit.setText("")
        self._script_edit.setText(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmware_server.py")
        )
