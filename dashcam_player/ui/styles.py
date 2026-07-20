# 全域樣式常數

PROGRESS_BAR_HIDDEN = "QProgressBar { background: transparent; border: none; }"

VIDEO_LABEL_STYLE = (
    "color: #aaa; font-size: 18px; background-color: #1e1e1e; border: 1px solid #444;"
)

BTN_START_STYLE = "background-color: #2d6a2d; color: white;"
BTN_END_STYLE = "background-color: #8b0000; color: white;"
BTN_CAPTURE_STYLE = "background-color: #005a8c; color: white; font-weight: bold;"
BTN_BATCH_STYLE = "background-color: #8c5800; color: white; font-weight: bold;"
BTN_EXPORT_STYLE = "background-color: #8c005a; color: white; font-weight: bold;"


PREVIEW_TOOLBAR = (
    "QWidget#previewToolbar { background-color: #252525; border-top: 1px solid #555; }"
)
BTN_COMPACT = "QPushButton { padding: 1px 6px; font-size: 11px; }"

GLOBAL_STYLESHEET = """
QToolTip { background-color: #333; color: #fff; border: 1px solid #555; padding: 4px; }
QGroupBox { font-weight: bold; border: 1px solid #ccc; border-radius: 4px; margin-top: 8px; padding-top: 16px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
QPushButton { padding: 4px 10px; }
QPushButton:hover { background-color: #e0e0e0; }
QSpinBox, QComboBox, QLineEdit { padding: 3px; }
"""
