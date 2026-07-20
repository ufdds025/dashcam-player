#!/usr/bin/env python3
"""
行車紀錄器影片播放與截圖工具 — 入口點

Usage:
    pip install PyQt5 opencv-python
    python main.py
"""

from __future__ import annotations

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication

from dashcam_player.core.video_player import VideoPlayer
from dashcam_player.ui.styles import GLOBAL_STYLESHEET
from dashcam_player.utils.log import init_log
from dashcam_player.utils.osm_map import init_osm_cache


def main() -> None:
    init_log()
    init_osm_cache()

    # 高分螢幕支援
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(GLOBAL_STYLESHEET)

    win = VideoPlayer()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
