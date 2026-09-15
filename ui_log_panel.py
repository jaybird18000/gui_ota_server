"""
gui_ota_server - ui_log_panel.py
Live log viewer with colour-coding, filter, auto-scroll, and export.
"""

import os
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
    QPushButton, QLineEdit, QGroupBox, QCheckBox, QFileDialog,
)
from PyQt6.QtCore import Qt, pyqtSlot, QTimer
from PyQt6.QtGui import QTextCursor, QColor, QTextCharFormat

_LEVEL_COLOURS = {
    "[ERROR]": "#f38ba8",
    "[WARN]":  "#f9e2af",
    "[INFO]":  "#89b4fa",
    "[DEBUG]": "#6c7086",
    "GET ":    "#a6e3a1",
    "POST ":   "#cba6f7",
    "PUT ":    "#fab387",
    "DELETE ": "#f38ba8",
}
_DEFAULT_COLOUR   = "#cdd6f4"
_DEVICE_COLOUR    = "#94e2d5"
_TIMESTAMP_COLOUR = "#585b70"


class LogPanel(QGroupBox):
    MAX_LINES = 2000

    def __init__(self, parent=None):
        super().__init__("📋  Live Log", parent)
        self._line_count = 0
        self._paused = False
        self._filter_text = ""
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 12, 6, 6)
        root.setSpacing(4)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter logs…")
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self._filter_edit, 1)

        self._chk_auto_scroll = QCheckBox("Auto-scroll")
        self._chk_auto_scroll.setChecked(True)
        toolbar.addWidget(self._chk_auto_scroll)

        self._chk_timestamps = QCheckBox("Timestamps")
        self._chk_timestamps.setChecked(True)
        toolbar.addWidget(self._chk_timestamps)

        self._btn_pause = QPushButton("⏸ Pause")
        self._btn_pause.setCheckable(True)
        self._btn_pause.setMaximumWidth(80)
        self._btn_pause.toggled.connect(self._on_pause_toggled)
        toolbar.addWidget(self._btn_pause)

        self._btn_clear = QPushButton("🗑 Clear")
        self._btn_clear.setMaximumWidth(80)
        self._btn_clear.clicked.connect(self.clear)
        toolbar.addWidget(self._btn_clear)

        self._btn_export = QPushButton("💾 Export")
        self._btn_export.setMaximumWidth(80)
        self._btn_export.clicked.connect(self._export)
        toolbar.addWidget(self._btn_export)

        root.addLayout(toolbar)

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        root.addWidget(self._log_view, 1)

        from PyQt6.QtWidgets import QLabel
        self._lbl_count = QLabel("0 lines")
        self._lbl_count.setStyleSheet("color: #585b70; font-size: 11px;")
        self._lbl_count.setAlignment(Qt.AlignmentFlag.AlignRight)
        root.addWidget(self._lbl_count)

        self._pending: list[tuple[str, str]] = []
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(33)
        self._flush_timer.timeout.connect(self._flush_pending)
        self._flush_timer.start()

    @pyqtSlot(str)
    def append_line(self, line: str):
        if self._paused:
            return
        colour = _DEFAULT_COLOUR
        for token, col in _LEVEL_COLOURS.items():
            if token in line:
                colour = col
                break
        self._pending.append((line, colour))

    @pyqtSlot(dict)
    def append_device_event(self, device: dict):
        msg = (f"[DEVICE]  {device['id']} connected — "
               f"first seen {device['first_seen']}")
        self._pending.append((msg, _DEVICE_COLOUR))

    def clear(self):
        self._log_view.clear()
        self._line_count = 0
        self._lbl_count.setText("0 lines")

    def _flush_pending(self):
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        for text, colour in batch:
            if self._filter_text and self._filter_text.lower() not in text.lower():
                continue
            if self._line_count >= self.MAX_LINES:
                self._trim_oldest(200)

            if self._chk_timestamps.isChecked():
                ts = datetime.now().strftime("%H:%M:%S.%f")[:12]
                fmt_ts = QTextCharFormat()
                fmt_ts.setForeground(QColor(_TIMESTAMP_COLOUR))
                cursor.insertText(ts + "  ", fmt_ts)

            fmt = QTextCharFormat()
            fmt.setForeground(QColor(colour))
            cursor.insertText(text + "\n", fmt)
            self._line_count += 1

        self._lbl_count.setText(f"{self._line_count} lines")
        if self._chk_auto_scroll.isChecked():
            self._log_view.setTextCursor(cursor)
            self._log_view.ensureCursorVisible()

    def _trim_oldest(self, n: int):
        doc = self._log_view.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        for _ in range(n):
            cursor.movePosition(QTextCursor.MoveOperation.Down,
                                QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._line_count = max(0, self._line_count - n)

    def _on_filter_changed(self, text: str):
        self._filter_text = text.strip()

    def _on_pause_toggled(self, paused: bool):
        self._paused = paused
        self._btn_pause.setText("▶ Resume" if paused else "⏸ Pause")

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Log",
            os.path.expanduser(
                f"~/ota_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            ),
            "Text Files (*.txt);;All Files (*)",
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._log_view.toPlainText())
            except Exception as exc:
                self.append_line(f"[ERROR] Log export failed: {exc}")