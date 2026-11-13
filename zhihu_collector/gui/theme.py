"""浅色桌面主题。颜色、圆角和按钮状态集中在这里。"""

from __future__ import annotations

APP_STYLE = """
QWidget {
    background: #f3f4f6;
    color: #1f2933;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog {
    background: #f3f4f6;
}
QFrame, QGroupBox, QTextBrowser, QPlainTextEdit, QTableWidget, QListWidget, QLineEdit, QComboBox, QSpinBox {
    background: #ffffff;
}
QGroupBox {
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    margin-top: 12px;
    padding: 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {
    border: 1px solid #d0d5dd;
    border-radius: 8px;
    padding: 4px 8px;
    background: #ffffff;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #d0d5dd;
    border-radius: 8px;
    padding: 6px 12px;
}
QPushButton:hover {
    background: #f8fafc;
    border-color: #98a2b3;
}
QPushButton:disabled {
    color: #98a2b3;
    background: #f2f4f7;
}
QPushButton:focus {
    border: 2px solid #1d4ed8;
}
QPushButton[role="primary"] {
    background: #1d4ed8;
    color: white;
    border: 1px solid #1d4ed8;
    font-weight: 600;
}
QPushButton[role="primary"]:hover {
    background: #1e40af;
}
QPushButton[role="primary"]:disabled {
    background: #93c5fd;
    color: white;
    border-color: #93c5fd;
}
QPushButton[role="nav"] {
    text-align: left;
    padding: 8px 12px;
    border: none;
    background: transparent;
}
QPushButton[role="nav"]:checked {
    background: #dbeafe;
    color: #1d4ed8;
    font-weight: 600;
}
QHeaderView::section {
    background: #f8fafc;
    border: none;
    border-bottom: 1px solid #e5e7eb;
    padding: 6px;
}
QTableWidget {
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    gridline-color: #eef2f6;
}
QStatusBar, QLabel[role="hint"] {
    color: #52606d;
}
"""


def apply_theme(widget) -> None:
    widget.setStyleSheet(APP_STYLE)
