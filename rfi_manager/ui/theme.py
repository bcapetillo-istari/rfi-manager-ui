"""Shared visual styling only — no behavior. Object names below are QSS
hooks; they don't affect any Python attribute name, signal, or logic."""

from __future__ import annotations

ACCENT = "#3457d5"
ACCENT_DARK = "#2a44ab"
TEXT_DARK = "#1f2430"
TEXT_MUTED = "#5b6270"
BORDER = "#d4d8e0"
SURFACE = "#f6f7fb"
CARD = "#ffffff"

STYLESHEET = f"""
QMainWindow {{
    background: {SURFACE};
}}

QWidget {{
    font-size: 13px;
    color: {TEXT_DARK};
}}

QLabel[role="section"] {{
    font-size: 14px;
    font-weight: 600;
    color: {TEXT_DARK};
    padding-top: 4px;
}}

QLabel[role="hint"] {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}

QWidget#card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}

QMenuBar {{
    background: {CARD};
    border-bottom: 1px solid {BORDER};
}}

QMenuBar::item:selected {{
    background: {SURFACE};
}}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 4px 8px;
    selection-background-color: {ACCENT};
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{
    border: 1px solid {ACCENT};
}}

QLineEdit:disabled, QComboBox:disabled, QPushButton:disabled {{
    color: {TEXT_MUTED};
    background: {SURFACE};
}}

QPushButton {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 6px 14px;
}}

QPushButton:hover {{
    border-color: {ACCENT};
}}

QPushButton:pressed {{
    background: {SURFACE};
}}

QPushButton#primaryButton {{
    background: {ACCENT};
    color: white;
    border: 1px solid {ACCENT_DARK};
    font-weight: 600;
}}

QPushButton#primaryButton:hover {{
    background: {ACCENT_DARK};
}}

QPushButton#primaryButton:disabled {{
    background: {BORDER};
    border-color: {BORDER};
    color: {TEXT_MUTED};
}}

QPushButton#navButton {{
    background: transparent;
    border: none;
    border-bottom: 3px solid transparent;
    border-radius: 0;
    padding: 10px 16px;
    font-weight: 500;
    color: {TEXT_MUTED};
}}

QPushButton#navButton:checked {{
    color: {TEXT_DARK};
    border-bottom: 3px solid {ACCENT};
    font-weight: 600;
}}

QPushButton#navButton:hover {{
    color: {TEXT_DARK};
}}

QTableWidget, QTableView {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
    gridline-color: {BORDER};
    alternate-background-color: {SURFACE};
}}

QHeaderView::section {{
    background: {SURFACE};
    color: {TEXT_DARK};
    padding: 6px;
    border: none;
    border-bottom: 1px solid {BORDER};
    border-right: 1px solid {BORDER};
    font-weight: 600;
}}

QDockWidget {{
    font-weight: 600;
}}

QDockWidget::title {{
    background: {CARD};
    border-bottom: 1px solid {BORDER};
    padding: 6px;
}}

QSplitter::handle {{
    background: {BORDER};
}}
"""
