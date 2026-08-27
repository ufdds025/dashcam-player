from __future__ import annotations

from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from dashcam_player.ui.styles import (
    BTN_BATCH_STYLE,
    BTN_CAPTURE_STYLE,
    BTN_COMPACT,
    BTN_END_STYLE,
    BTN_EXPORT_STYLE,
    BTN_START_STYLE,
    PREVIEW_TOOLBAR,
)


class PlaybackControls(QWidget):
    """播放控制工具列：一份 widget，全時可見，兩種模式共用"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewToolbar")
        self.setStyleSheet(PREVIEW_TOOLBAR)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(3)

        # 播放按鈕
        self.btn_rewind = QPushButton("⏮")
        self.btn_rewind.setFixedWidth(30)
        self.btn_rewind.setToolTip("倒帶 5 秒")

        self.btn_step_bwd = QPushButton("⏪")
        self.btn_step_bwd.setFixedWidth(30)
        self.btn_step_bwd.setToolTip("單張後退")

        self.btn_play = QPushButton("▶")
        self.btn_play.setFixedWidth(36)
        self.btn_play.setToolTip("播放 / 暫停")

        self.btn_step_fwd = QPushButton("⏩")
        self.btn_step_fwd.setFixedWidth(30)
        self.btn_step_fwd.setToolTip("單張前進")

        self.btn_ffwd = QPushButton("⏭")
        self.btn_ffwd.setFixedWidth(30)
        self.btn_ffwd.setToolTip("快進 5 秒")

        layout.addWidget(self.btn_rewind)
        layout.addWidget(self.btn_step_bwd)
        layout.addWidget(self.btn_play)
        layout.addWidget(self.btn_step_fwd)
        layout.addWidget(self.btn_ffwd)

        # 速度
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.25x", "0.5x", "1.0x", "1.5x", "2.0x"])
        self.speed_combo.setCurrentIndex(2)
        self.speed_combo.setFixedWidth(58)
        layout.addWidget(self.speed_combo)

        # 標記
        self.btn_set_start = QPushButton("標記開始")
        self.btn_set_start.setFixedWidth(72)
        self.btn_set_start.setStyleSheet(BTN_START_STYLE)

        self.btn_set_end = QPushButton("標記結束")
        self.btn_set_end.setFixedWidth(72)
        self.btn_set_end.setStyleSheet(BTN_END_STYLE)

        self.btn_clear_markers = QPushButton("清除標記")
        self.btn_clear_markers.setFixedWidth(72)

        layout.addWidget(self.btn_set_start)
        layout.addWidget(self.btn_set_end)
        layout.addWidget(self.btn_clear_markers)

        # 標記狀態
        self.marker_label = QLabel("開始:-- | 結束:--")
        self.marker_label.setFont(QFont("Consolas", 9))
        self.marker_label.setFixedWidth(130)
        layout.addWidget(self.marker_label)

        # 時間 / 幀數
        self.time_label = QLabel("00:00:00/00:00:00")
        self.time_label.setFont(QFont("Consolas", 9))
        self.frame_label = QLabel("F:0/0")
        self.frame_label.setFont(QFont("Consolas", 9))
        layout.addWidget(self.time_label)
        layout.addWidget(self.frame_label)

        # 視覺測速顯示
        self.speed_label = QLabel("-- km/h")
        self.speed_label.setFont(QFont("Consolas", 9, QFont.Bold))
        self.speed_label.setStyleSheet("color: #000; padding-left: 6px;")
        self.speed_label.setFixedWidth(80)
        layout.addWidget(self.speed_label)

        layout.addStretch()

        # 動作按鈕
        self.btn_output = QPushButton("輸出設定")
        self.btn_output.setStyleSheet(BTN_COMPACT)
        self.btn_capture_cfg = QPushButton("擷取設定")
        self.btn_capture_cfg.setStyleSheet(BTN_COMPACT)
        self.btn_single = QPushButton("截取單張")
        self.btn_single.setStyleSheet(BTN_CAPTURE_STYLE)
        self.btn_range = QPushButton("批次截取")
        self.btn_range.setStyleSheet(BTN_BATCH_STYLE)
        self.btn_export = QPushButton("擷取影片")
        self.btn_export.setStyleSheet(BTN_EXPORT_STYLE)

        layout.addWidget(self.btn_output)
        layout.addWidget(self.btn_capture_cfg)
        layout.addWidget(self.btn_single)
        layout.addWidget(self.btn_range)
        layout.addWidget(self.btn_export)

        # 進度提示
        self.progress_label = QLabel()
        self.progress_label.setFont(QFont("Consolas", 9))
        self.progress_label.setStyleSheet("color: #0a0;")
        self.progress_label.setVisible(False)
        layout.addWidget(self.progress_label)
