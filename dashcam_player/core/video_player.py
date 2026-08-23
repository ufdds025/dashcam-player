from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PyQt5.QtCore import QEvent, Qt, QTimer
from PyQt5.QtGui import QFont, QImage, QKeySequence, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from dashcam_player.core.controller import Controller
from dashcam_player.models.capture_settings import DEFAULT_OUTPUT_DIR
from dashcam_player.models.resolution import Resolution
from dashcam_player.models.video_info import VideoInfo
from dashcam_player.ui.dialogs.capture_settings import CaptureSettingsDialog
from dashcam_player.ui.dialogs.gps_settings import GPSSettingsDialog
from dashcam_player.ui.dialogs.output_settings import OutputSettingsDialog
from dashcam_player.ui.map_view import MapView
from dashcam_player.ui.playback_controls import PlaybackControls
from dashcam_player.ui.styles import (
    PROGRESS_BAR_HIDDEN,
    VIDEO_LABEL_STYLE,
)
from dashcam_player.ui.widgets.encoder_fields import (
    make_codec_combo,
    make_crf_spin,
    make_format_combo,
    make_preset_combo,
    make_scale_combo,
)

if TYPE_CHECKING:
    from dashcam_player.models.capture_settings import CaptureSettings
from dashcam_player.utils.cv_utils import frame_to_qimage
from dashcam_player.utils.log import get_log_path, get_logger
from dashcam_player.utils.time_utils import format_time

log = get_logger()


class VideoPlayer(QMainWindow):
    """瘦 UI：只負責佈局與事件綁定，所有邏輯委託給 Controller"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("行車紀錄器影片播放與截圖工具")
        self.setGeometry(100, 50, 1400, 900)

        self.ctrl = Controller(parent=self)

        # 預覽模式
        self._preview_mode: bool = False

        # 縮放狀態（暫停時滾輪縮放）
        self._zoom_factor: float = 1.0
        self._zoom_rel_x: float = 0.5
        self._zoom_rel_y: float = 0.5
        self._min_zoom: float = 1.0
        self._max_zoom: float = 5.0

        # GPS / 地圖
        self._has_gps: bool = False
        self.map_view: MapView | None = None  # 在 _init_ui 建立

        # 設定 debounce 儲存
        self._save_debounce = QTimer()
        self._save_debounce.setSingleShot(True)
        self._save_debounce.timeout.connect(self._save_settings)

        # 播放中「最新幀」刷新：丟棄中間幀，避免 UI 跟不上時信號佇列堆積造成拖影/延遲
        self._latest_frame: tuple | None = None
        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setInterval(15)
        self._frame_timer.timeout.connect(self._flush_latest_frame)

        self._init_ui()
        self._connect_controller()
        self._restore_settings()

    @property
    def settings(self) -> CaptureSettings:
        """統一經由 Controller 取得 capture_settings，避免雙參照不同步。"""
        return self.ctrl.capture_settings

    # =========================================================================
    # UI 初始化
    # =========================================================================

    def _init_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(4)

        self._create_menu_bar()

        # 影片 + 地圖左右分割
        self._video_container = QWidget()
        vc_layout = QVBoxLayout(self._video_container)
        vc_layout.setContentsMargins(0, 0, 0, 0)
        vc_layout.setSpacing(0)

        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumSize(640, 360)
        self.video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_label.setText("請開啟影片檔\n(檔案 → 開啟影片)")
        self.video_label.setStyleSheet(VIDEO_LABEL_STYLE)
        vc_layout.addWidget(self.video_label, 1)

        # 地圖 (預設隱藏)
        self.map_view = MapView()
        self.map_view.setVisible(False)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self._video_container)
        self.splitter.addWidget(self.map_view)
        self.splitter.setStretchFactor(0, 6)  # 影片 60%
        self.splitter.setStretchFactor(1, 4)  # 地圖 40%

        self.timeline = QSlider(Qt.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.sliderMoved.connect(self._on_timeline_preview)
        self.timeline.sliderReleased.connect(self._on_timeline_seek)

        # PlaybackControls（始終可見，取代兩套舊控制項）
        self.controls = PlaybackControls()
        self.controls.btn_play.clicked.connect(self._toggle_play)
        self.controls.btn_rewind.clicked.connect(lambda: self.ctrl.skip(-5.0))
        self.controls.btn_step_bwd.clicked.connect(self.ctrl.step_backward)
        self.controls.btn_step_fwd.clicked.connect(self.ctrl.step_forward)
        self.controls.btn_ffwd.clicked.connect(lambda: self.ctrl.skip(5.0))
        self.controls.speed_combo.currentIndexChanged.connect(self._on_speed_change)
        self.controls.btn_set_start.clicked.connect(self._set_capture_start)
        self.controls.btn_set_end.clicked.connect(self._set_capture_end)
        self.controls.btn_clear_markers.clicked.connect(self._clear_markers)
        self.controls.btn_output.clicked.connect(self._open_output_settings)
        self.controls.btn_capture_cfg.clicked.connect(self._open_capture_settings)
        self.controls.btn_single.clicked.connect(self._capture_single)
        self.controls.btn_range.clicked.connect(self._capture_range)
        self.controls.btn_export.clicked.connect(self._capture_video)

        # --- 底部容器（設定面板，正常模式顯示，預覽模式隱藏）---
        self._bottom_container = QWidget()
        _bc_layout = QVBoxLayout(self._bottom_container)
        _bc_layout.setContentsMargins(0, 0, 0, 0)
        _bc_layout.setSpacing(0)

        # _full_bottom：正常模式顯示、預覽模式整塊隱藏（維持原有 toggle 行為）
        self._full_bottom = QWidget()
        _full_bottom_layout = QVBoxLayout(self._full_bottom)
        _full_bottom_layout.setContentsMargins(0, 4, 0, 0)
        _full_bottom_layout.setSpacing(4)

        # ---- 共用列：輸出資料夾 + 進度條（單張／批次／影片皆共用，抽出來不重複佔位）----
        shared_row = QHBoxLayout()
        shared_row.setSpacing(6)
        shared_row.addWidget(QLabel("輸出資料夾:"))

        self.output_path_label = QLabel(
            self.settings.output_dir or os.path.abspath(DEFAULT_OUTPUT_DIR)
        )
        self.output_path_label.setFont(QFont("Consolas", 9))
        self.output_path_label.setWordWrap(False)
        shared_row.addWidget(self.output_path_label, 1)

        self.btn_browse = QPushButton("瀏覽...")
        self.btn_browse.clicked.connect(self._browse_output)
        shared_row.addWidget(self.btn_browse)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(16)
        self.progress_bar.setFixedWidth(160)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(PROGRESS_BAR_HIDDEN)
        shared_row.addWidget(self.progress_bar)

        _full_bottom_layout.addLayout(shared_row)

        # ---- 截圖設定（單張／批次共用）：4 欄網格，每列並排 2 組設定 ----
        right_group = QGroupBox("截圖設定（單張 / 批次）")
        right_layout = QGridLayout(right_group)
        right_layout.setColumnStretch(1, 1)
        right_layout.setColumnStretch(3, 1)

        right_layout.addWidget(QLabel("每秒擷取幀數:"), 0, 0)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 9999)
        self.fps_spin.setValue(self.settings.fps)
        self.fps_spin.setToolTip("批次截取時每秒擷取的幀數")
        self.fps_spin.setEnabled(False)
        right_layout.addWidget(self.fps_spin, 0, 1)

        right_layout.addWidget(QLabel("輸出解析度:"), 0, 2)
        self.res_combo = QComboBox()
        self.res_combo.addItems(Resolution.values())
        self.res_combo.setCurrentIndex(
            max(0, self.res_combo.findText(self.settings.resolution.value))
        )
        self.res_combo.setEnabled(False)
        right_layout.addWidget(self.res_combo, 0, 3)

        right_layout.addWidget(QLabel("檔名前綴:"), 1, 0)
        self.prefix_edit = QLineEdit("")
        self.prefix_edit.setReadOnly(True)
        right_layout.addWidget(self.prefix_edit, 1, 1)

        right_layout.addWidget(QLabel("截圖格式:"), 1, 2)
        self.img_format_combo = QComboBox()
        self.img_format_combo.addItems(["JPEG", "PNG"])
        self.img_format_combo.setCurrentIndex(0 if self.settings.img_format == "JPEG" else 1)
        self.img_format_combo.setEnabled(False)
        self.img_format_combo.currentIndexChanged.connect(self._on_img_format_change)
        right_layout.addWidget(self.img_format_combo, 1, 3)

        self.quality_label = QLabel("JPEG 品質:")
        right_layout.addWidget(self.quality_label, 2, 0)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(self.settings.jpeg_quality)
        self.quality_spin.setToolTip("1=最小檔案, 100=最佳品質")
        self.quality_spin.setEnabled(False)
        right_layout.addWidget(self.quality_spin, 2, 1)

        # ---- 影片擷取設定：4 欄網格，相關設定並排 ----
        vid_group = QGroupBox("影片擷取設定")
        vid_layout = QGridLayout(vid_group)
        vid_layout.setColumnStretch(1, 1)
        vid_layout.setColumnStretch(3, 1)

        vid_layout.addWidget(QLabel("輸出格式:"), 0, 0)
        self.vid_format_combo = make_format_combo()
        self.vid_format_combo.setCurrentIndex(0 if self.settings.video_format == "MP4" else 1)
        self.vid_format_combo.setEnabled(False)
        vid_layout.addWidget(self.vid_format_combo, 0, 1)

        self.keep_audio_check = QCheckBox("保留音軌")
        self.keep_audio_check.setChecked(self.settings.keep_audio)
        self.keep_audio_check.setToolTip("無音軌時自動忽略")
        self.keep_audio_check.setEnabled(False)
        vid_layout.addWidget(self.keep_audio_check, 0, 2, 1, 2)

        self.copy_codec_check = QCheckBox("使用原始編碼（stream copy，最快）")
        self.copy_codec_check.setChecked(self.settings.use_stream_copy)
        self.copy_codec_check.setToolTip(
            "直接複製串流，不重新編碼，速度快但不可加浮水印\n"
            "注意：起點精確度受 keyframe 位置影響（通常差距 < 2 秒）"
        )
        self.copy_codec_check.setEnabled(False)
        vid_layout.addWidget(self.copy_codec_check, 1, 0, 1, 4)

        # 重新編碼設定：唯讀顯示
        self._enc_widgets: list[QWidget] = []

        self._enc_label = QLabel("── 重新編碼設定 ──")
        vid_layout.addWidget(self._enc_label, 2, 0, 1, 4)
        self._enc_widgets.append(self._enc_label)

        _lbl = QLabel("編碼格式:")
        vid_layout.addWidget(_lbl, 3, 0)
        self._enc_widgets.append(_lbl)
        self.vid_codec_combo = make_codec_combo()
        self.vid_codec_combo.setCurrentIndex(0 if self.settings.video_codec == "libx264" else 1)
        self.vid_codec_combo.setEnabled(False)
        vid_layout.addWidget(self.vid_codec_combo, 3, 1)
        self._enc_widgets.append(self.vid_codec_combo)

        _lbl = QLabel("CRF 品質:")
        vid_layout.addWidget(_lbl, 3, 2)
        self._enc_widgets.append(_lbl)
        self.vid_crf_spin = make_crf_spin()
        self.vid_crf_spin.setValue(self.settings.video_crf)
        self.vid_crf_spin.setEnabled(False)
        vid_layout.addWidget(self.vid_crf_spin, 3, 3)
        self._enc_widgets.append(self.vid_crf_spin)

        _lbl = QLabel("編碼預設:")
        vid_layout.addWidget(_lbl, 4, 0)
        self._enc_widgets.append(_lbl)
        self.vid_preset_combo = make_preset_combo()
        idx = max(0, self.vid_preset_combo.findText(self.settings.video_preset))
        self.vid_preset_combo.setCurrentIndex(idx)
        self.vid_preset_combo.setEnabled(False)
        vid_layout.addWidget(self.vid_preset_combo, 4, 1)
        self._enc_widgets.append(self.vid_preset_combo)

        _lbl = QLabel("縮放寬度:")
        vid_layout.addWidget(_lbl, 4, 2)
        self._enc_widgets.append(_lbl)
        self.vid_scale_combo = make_scale_combo()
        self.vid_scale_combo.setCurrentIndex(
            max(0, self.vid_scale_combo.findText(self.settings.video_scale_width))
        )
        self.vid_scale_combo.setEnabled(False)
        vid_layout.addWidget(self.vid_scale_combo, 4, 3)
        self._enc_widgets.append(self.vid_scale_combo)

        vid_layout.addWidget(QLabel("開始時間:"), 5, 0)
        self.vid_start_edit = QLineEdit()
        self.vid_start_edit.setPlaceholderText("hh:mm:ss（空=用標記）")
        self.vid_start_edit.setToolTip("留空則使用標記開始位置")
        self.vid_start_edit.setReadOnly(True)
        vid_layout.addWidget(self.vid_start_edit, 5, 1)

        vid_layout.addWidget(QLabel("結束時間:"), 5, 2)
        self.vid_end_edit = QLineEdit()
        self.vid_end_edit.setPlaceholderText("hh:mm:ss（空=用標記）")
        self.vid_end_edit.setToolTip("留空則使用標記結束位置")
        self.vid_end_edit.setReadOnly(True)
        vid_layout.addWidget(self.vid_end_edit, 5, 3)

        groups_row = QWidget()
        groups_row_layout = QHBoxLayout(groups_row)
        groups_row_layout.setContentsMargins(0, 0, 0, 0)
        groups_row_layout.addWidget(right_group, 1)
        groups_row_layout.addWidget(vid_group, 1)
        _full_bottom_layout.addWidget(groups_row)

        _bc_layout.addWidget(self._full_bottom)

        main_layout.addWidget(self.splitter, 1)
        main_layout.addWidget(self.timeline)
        main_layout.addWidget(self.controls)
        main_layout.addWidget(self._bottom_container)

        # 雙擊偵測 & 滾輪縮放
        self.video_label.installEventFilter(self)
        self.video_label.setMouseTracking(True)

        # 狀態列
        self._update_status_bar()

    # =========================================================================
    # 預覽模式（雙擊切換）
    # =========================================================================

    def _toggle_preview_mode(self) -> None:
        self._preview_mode = not self._preview_mode
        self._full_bottom.setVisible(not self._preview_mode)
        mode_name = "預覽（雙擊返回）" if self._preview_mode else "正常"
        self.statusBar().showMessage(f"已切換為{mode_name}模式")

    def eventFilter(self, obj, event) -> bool:
        if obj is self.video_label:
            if event.type() == QEvent.MouseButtonDblClick:
                self._toggle_preview_mode()
                return True
            if event.type() == QEvent.Wheel:
                if self.ctrl.current_frame is not None:
                    self._zoom_rel_x, self._zoom_rel_y = self._get_image_relative_pos(
                        event.pos().x(), event.pos().y()
                    )
                    delta = event.angleDelta().y()
                    if delta == 0:
                        # 觸控板可能只提供 pixelDelta
                        delta = event.pixelDelta().y()
                    if delta > 0:
                        self._zoom_factor = min(self._max_zoom, self._zoom_factor + 0.2)
                    else:
                        self._zoom_factor = max(self._min_zoom, self._zoom_factor - 0.2)
                    self._refresh_display()
                    self._update_zoom_status()
                    return True
            if event.type() == QEvent.Resize:
                self._refresh_display()
        return super().eventFilter(obj, event)

    def _create_menu_bar(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("檔案")
        file_menu.addAction("開啟影片", self._open_video, QKeySequence.Open)
        file_menu.addSeparator()
        file_menu.addAction("結束", self._quit, QKeySequence.Quit)

        capture_menu = menubar.addMenu("截取")
        capture_menu.addAction("截取單張", self._capture_single, QKeySequence("Ctrl+S"))
        capture_menu.addAction("批次截取區間", self._capture_range, QKeySequence("Ctrl+B"))
        capture_menu.addAction("擷取影片", self._capture_video, QKeySequence("Ctrl+V"))

        nav_menu = menubar.addMenu("導覽")
        nav_menu.addAction("播放/暫停", self._toggle_play, QKeySequence(Qt.Key_Space))
        nav_menu.addAction("單張前進", self.ctrl.step_forward, QKeySequence(Qt.Key_Right))
        nav_menu.addAction("單張後退", self.ctrl.step_backward, QKeySequence(Qt.Key_Left))
        nav_menu.addAction("快進 5 秒", lambda: self.ctrl.skip(5.0), QKeySequence(Qt.Key_Up))
        nav_menu.addAction("倒帶 5 秒", lambda: self.ctrl.skip(-5.0), QKeySequence(Qt.Key_Down))

        marker_menu = menubar.addMenu("標記")
        marker_menu.addAction("標記開始", self._set_capture_start, QKeySequence("Ctrl+1"))
        marker_menu.addAction("標記結束", self._set_capture_end, QKeySequence("Ctrl+2"))
        marker_menu.addAction("清除標記", self._clear_markers, QKeySequence("Ctrl+0"))

        gps_menu = menubar.addMenu("GPS")
        self._gps_toggle_action = gps_menu.addAction("顯示 GPS 軌跡")
        self._gps_toggle_action.setCheckable(True)
        self._gps_toggle_action.setChecked(self.settings.show_gps_map)
        self._gps_toggle_action.triggered.connect(self._toggle_gps_map)

        self._gps_skip_action = gps_menu.addAction("跳過照片 GPS 疊加")
        self._gps_skip_action.setCheckable(True)
        self._gps_skip_action.setChecked(self.settings.skip_gps_overlay)
        self._gps_skip_action.triggered.connect(self._toggle_gps_skip)

        gps_menu.addSeparator()
        gps_menu.addAction("GPS 設定...", self._open_gps_settings)

        gps_menu.addSeparator()
        self._gps_roi_action = gps_menu.addAction("設定 GPS 文字位置")
        self._gps_roi_action.triggered.connect(self._open_gps_roi)
        self._gps_roi_action.setEnabled(False)

        gps_menu.addSeparator()
        self._gps_export_action = gps_menu.addAction("匯出 GPX 軌跡...")
        self._gps_export_action.triggered.connect(self._export_gpx)
        self._gps_export_action.setEnabled(False)

        # 初始狀態：無 GPS 時停用
        self._update_gps_menu_state(has_gps=False)

        help_menu = menubar.addMenu("說明")
        help_menu.addAction("檢視除錯日誌", self._show_log, QKeySequence("Ctrl+L"))

    # =========================================================================
    # Controller 信號連接
    # =========================================================================

    def _connect_controller(self) -> None:
        self.ctrl.frame_ready.connect(self._on_frame_ready)
        self.ctrl.video_loaded.connect(self._on_video_loaded)
        self.ctrl.position_updated.connect(self._on_position_updated)
        self.ctrl.status_message.connect(lambda msg: self.statusBar().showMessage(msg))
        self.ctrl.load_failed.connect(self._on_load_failed)
        self.ctrl.error_occurred.connect(self._on_error_occurred)
        self.ctrl.play_finished.connect(self._on_play_finished)
        self.ctrl.batch_progress.connect(self._on_batch_progress)
        self.ctrl.batch_finished.connect(self._on_batch_finished)
        self.ctrl.export_finished.connect(self._on_export_finished)
        self.ctrl.gps_position_updated.connect(self._on_gps_position_updated)
        self.ctrl.gps_ready.connect(self._on_gps_ready)

    # =========================================================================
    # 設定儲存/還原
    # =========================================================================

    def _restore_settings(self) -> None:
        geo = self.ctrl.settings.window_geometry
        if geo:
            self.restoreGeometry(geo)
        state = self.ctrl.settings.window_state
        if state:
            self.restoreState(state)

    def _save_settings(self) -> None:
        self.ctrl.settings.window_geometry = self.saveGeometry()
        self.ctrl.settings.window_state = self.saveState()
        self.ctrl.settings.save_capture_settings(self.settings)

    def _schedule_save(self) -> None:
        """debounce 500ms 儲存設定，避免短時間內多次 I/O"""
        self._save_debounce.start(500)

    # =========================================================================
    # 影片操作
    # =========================================================================

    def _open_video(self) -> None:
        last_dir = self.ctrl.settings.last_open_dir or ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "選擇影片",
            last_dir,
            "影片檔 (*.ts *.mp4 *.avi *.mov *.mkv *.wmv *.flv);;所有檔案 (*.*)",
        )
        if not path:
            return

        self.ctrl.settings.last_open_dir = os.path.dirname(path)
        self._reset_ui_state()
        self.ctrl.open_video(path)

    def _reset_ui_state(self) -> None:
        self._zoom_factor = 1.0
        self._latest_frame = None
        if self._frame_timer.isActive():
            self._frame_timer.stop()
        self.controls.marker_label.setText("開始:-- | 結束:--")
        self.prefix_edit.clear()
        self.vid_start_edit.clear()
        self.vid_end_edit.clear()
        self.controls.btn_play.setText("▶")
        self.progress_bar.setStyleSheet(PROGRESS_BAR_HIDDEN)
        self.progress_bar.setValue(0)
        self.timeline.setRange(0, 0)
        self.controls.time_label.setText("00:00:00 / 00:00:00")
        self.controls.frame_label.setText("F:0/0")
        self.video_label.setText("載入中...")
        self.video_label.setStyleSheet(VIDEO_LABEL_STYLE)
        self.controls.btn_range.setEnabled(True)
        self.controls.btn_export.setEnabled(True)
        # GPS 狀態 — 在背景執行緒啟動前重設，避免舊影片的 GPS 狀態殘留
        self._has_gps = False
        self._update_gps_menu_state(False)
        self._hide_gps_map()

    def _on_frame_ready(self, frame, idx: int) -> None:
        if self.ctrl.is_playing:
            # 播放中只保留最新幀，由 15ms 單發 timer 統一刷新（丟棄中間幀）
            self._latest_frame = (frame, idx)
            if not self._frame_timer.isActive():
                self._frame_timer.start()
        else:
            # 暫停/單步/seek：立即顯示，保持低延遲
            self._latest_frame = None
            if self._frame_timer.isActive():
                self._frame_timer.stop()
            self._display_frame(frame)

    def _flush_latest_frame(self) -> None:
        if self._latest_frame is not None:
            frame, _ = self._latest_frame
            self._latest_frame = None
            self._display_frame(frame)

    def _on_video_loaded(self, info: VideoInfo) -> None:
        self.timeline.setRange(0, self.ctrl.total_frames - 1)
        self.fps_spin.setRange(1, max(1, int(self.ctrl.fps)))
        self.fps_spin.setValue(int(self.ctrl.fps))
        self.prefix_edit.setText(self.settings.prefix)
        self.output_path_label.setText(self.settings.output_dir)
        self._update_time_display(0)
        # 顯示載入完成 + GPS 偵測狀態（若 GPS 尚未完成則顯示「偵測中」）
        hw = self.ctrl.hw_backends
        hw_str = ", ".join(f"{name} ({dev})" for name, dev in hw) if hw else "無（軟體解碼）"
        if self.ctrl.has_gps_manager and not self.ctrl.gps_is_loaded():
            gps_status = "GPS 偵測中..."
        elif self.ctrl.has_gps_data():
            gps_status = f"GPS: {self.ctrl.gps_track_count()} 點"
        else:
            gps_status = "GPS: 無"
        self.statusBar().showMessage(f"就緒  |  硬體加速: {hw_str}  |  {gps_status}")

    def _on_position_updated(self, sec: float, frame_idx: int) -> None:
        self._update_time_display(sec)
        if self.ctrl.total_frames > 0 and not self.timeline.isSliderDown():
            self.timeline.setValue(frame_idx)

    def _on_load_failed(self, msg: str) -> None:
        if self.isVisible():
            QMessageBox.critical(self, "載入失敗", msg)
        self.video_label.setText("載入失敗")
        self.statusBar().showMessage("載入失敗")

    def _on_error_occurred(self, title: str, message: str) -> None:
        """批次截圖/儲存等作業失敗的統一錯誤通報"""
        log.error("%s: %s", title, message)
        self.statusBar().showMessage(f"{title}: {message}")
        if self.isVisible():
            QMessageBox.critical(self, title, message)

    def _on_play_finished(self) -> None:
        self.controls.btn_play.setText("▶")
        self.statusBar().showMessage("播放結束")

    def _update_time_display(self, sec: float) -> None:
        current_str = format_time(sec)
        total_str = format_time(self.ctrl.duration)
        idx = self.ctrl.get_current_frame_index()
        self.controls.time_label.setText(f"{current_str} / {total_str}")
        self.controls.frame_label.setText(f"F:{idx}/{self.ctrl.total_frames}")

    def _display_frame(self, frame_bgr) -> None:
        if frame_bgr is None or frame_bgr.size == 0:
            return

        if self._zoom_factor > 1.0:
            rect = self._get_zoom_crop_rect()
            if rect:
                x, y, w, h = rect
                frame_bgr = frame_bgr[y : y + h, x : x + w].copy()

        qimg = frame_to_qimage(frame_bgr)
        if qimg is None:
            return
        assert isinstance(qimg, QImage)
        label_size = self.video_label.size()
        if label_size.width() > 0 and label_size.height() > 0:
            pix = QPixmap.fromImage(qimg)
            transform = Qt.FastTransformation if self.ctrl.is_playing else Qt.SmoothTransformation
            scaled = pix.scaled(label_size, Qt.KeepAspectRatio, transform)
            self.video_label.setPixmap(scaled)

    def _update_status_bar(self) -> None:
        hw = self.ctrl.hw_backends
        if hw:
            hw_str = ", ".join(f"{name} ({dev})" for name, dev in hw)
            self.statusBar().showMessage(f"就緒  |  硬體加速: {hw_str}")
        else:
            self.statusBar().showMessage("就緒  |  硬體加速: 無（軟體解碼）")

    # =========================================================================
    # 縮放輔助
    # =========================================================================

    def _get_image_relative_pos(self, label_x: int, label_y: int) -> tuple[float, float]:
        if self.ctrl.current_frame is None:
            return (0.5, 0.5)
        fh, fw = self.ctrl.current_frame.shape[:2]
        ls = self.video_label.size()
        scale = min(ls.width() / fw, ls.height() / fh) if fw > 0 and fh > 0 else 1.0
        disp_w = fw * scale
        disp_h = fh * scale
        offset_x = max(0, (ls.width() - disp_w) / 2)
        offset_y = max(0, (ls.height() - disp_h) / 2)
        rx = (label_x - offset_x) / disp_w if disp_w > 0 else 0.5
        ry = (label_y - offset_y) / disp_h if disp_h > 0 else 0.5
        return (max(0.0, min(1.0, rx)), max(0.0, min(1.0, ry)))

    def _get_zoom_crop_rect(self):
        if self._zoom_factor <= 1.0 or self.ctrl.current_frame is None:
            return None
        fh, fw = self.ctrl.current_frame.shape[:2]
        zf = self._zoom_factor
        vis_w = fw / zf
        vis_h = fh / zf
        cx = self._zoom_rel_x * fw
        cy = self._zoom_rel_y * fh
        sx = cx - vis_w / 2
        sy = cy - vis_h / 2
        sx = max(0.0, min(fw - vis_w, sx))
        sy = max(0.0, min(fh - vis_h, sy))
        return (int(sx), int(sy), max(1, int(vis_w)), max(1, int(vis_h)))

    def _refresh_display(self) -> None:
        if self.ctrl.current_frame is not None:
            self._display_frame(self.ctrl.current_frame)

    def _update_zoom_status(self) -> None:
        if self._zoom_factor > 1.0:
            self.statusBar().showMessage(f"縮放: {self._zoom_factor:.1f}x（滾輪調整）")
        else:
            self._update_status_bar()

    # =========================================================================
    # 播放控制
    # =========================================================================

    def _toggle_play(self) -> None:
        if self.ctrl.total_frames <= 0:
            return
        playing = self.ctrl.toggle_play()
        self.controls.btn_play.setText("⏸" if playing else "▶")

    def _on_timeline_preview(self, value: int) -> None:
        """拖曳中即時更新時間顯示，但不下 seek 指令"""
        if self.ctrl.total_frames <= 0 or self.ctrl.fps <= 0:
            return
        sec = value / self.ctrl.fps
        self._update_time_display(sec)

    def _on_timeline_seek(self) -> None:
        """放開滑鼠後才實際 seek"""
        if self.ctrl.total_frames <= 0:
            return
        self.ctrl.seek_to(self.timeline.value())

    def _on_speed_change(self) -> None:
        speed_text = self.controls.speed_combo.currentText()
        speed = float(speed_text.replace("x", ""))
        self.ctrl.set_playback_speed(speed)
        self.statusBar().showMessage(f"播放速度: {speed_text}")

    # =========================================================================
    # 標記
    # =========================================================================

    def _set_capture_start(self) -> None:
        if self.ctrl.total_frames <= 0:
            return
        self.ctrl.capture_start_frame = self.ctrl.get_current_frame_index()
        self._update_marker_label()
        self.statusBar().showMessage(
            f"標記開始位置: Frame {self.ctrl.capture_start_frame} "
            f"({format_time(self.ctrl.capture_start_frame / self.ctrl.fps)})"
        )

    def _set_capture_end(self) -> None:
        if self.ctrl.total_frames <= 0:
            return
        self.ctrl.capture_end_frame = self.ctrl.get_current_frame_index()
        self._update_marker_label()
        self.statusBar().showMessage(
            f"標記結束位置: Frame {self.ctrl.capture_end_frame} "
            f"({format_time(self.ctrl.capture_end_frame / self.ctrl.fps)})"
        )

    def _clear_markers(self) -> None:
        self.ctrl.capture_start_frame = -1
        self.ctrl.capture_end_frame = -1
        self.controls.marker_label.setText("開始:-- | 結束:--")
        self.statusBar().showMessage("標記已清除")

    def _update_marker_label(self) -> None:
        def _fmt(frame: int) -> str:
            return f"F{frame}" if frame >= 0 else "--"

        self.controls.marker_label.setText(
            f"開始:{_fmt(self.ctrl.capture_start_frame)} | 結束:{_fmt(self.ctrl.capture_end_frame)}"
        )

    # =========================================================================
    # 截圖
    # =========================================================================

    def _capture_single(self) -> None:
        if self.ctrl.current_frame is None:
            if self.isVisible():
                QMessageBox.warning(self, "提示", "請先開啟影片並選擇畫面")
            return

        out_dir = self.output_path_label.text()
        prefix = self.prefix_edit.text().strip() or "capture"
        img_format = self.img_format_combo.currentText()
        quality = self.quality_spin.value()
        resolution = Resolution.from_value(self.res_combo.currentText())
        frame_idx = self.ctrl.get_current_frame_index()

        try:
            path = self.ctrl.capture_single(
                out_dir, prefix, img_format, quality, resolution, frame_idx
            )
            self.statusBar().showMessage(f"已儲存: {path}")
        except OSError as e:
            log.error("單張截圖失敗: %s", e)
            if self.isVisible():
                QMessageBox.critical(self, "儲存失敗", str(e))
        except Exception as e:
            log.error("單張截圖發生未預期錯誤: %s", e)
            if self.isVisible():
                QMessageBox.critical(self, "儲存失敗", f"儲存時發生未預期錯誤: {e}")

    def _capture_range(self) -> None:
        err = self.ctrl.validate_batch_range()
        if err:
            if self.isVisible():
                QMessageBox.warning(self, "提示", err)
            return

        out_dir = self.output_path_label.text()
        ext = "jpg" if self.img_format_combo.currentText() == "JPEG" else "png"
        prefix = self.prefix_edit.text().strip() or "capture"
        existing = self.ctrl.check_batch_conflict(out_dir, prefix, ext)
        if existing:
            if not self.isVisible():
                return
            ret = QMessageBox.question(
                self,
                "檔案已存在",
                f"目標資料夾已有 {len(existing)} 個同前綴的 {ext.upper()} 檔案。\n"
                f"繼續執行將覆蓋既有檔案。\n\n是否繼續？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                return

        self.ctrl.update_capture_settings(self.settings.replace(
            fps=self.fps_spin.value(),
            prefix=prefix,
            img_format=self.img_format_combo.currentText(),
            jpeg_quality=self.quality_spin.value(),
            resolution=Resolution.from_value(self.res_combo.currentText()),
            output_dir=out_dir,
        ))

        self.progress_bar.setStyleSheet("")
        self.controls.btn_range.setEnabled(False)

        self.ctrl.start_batch_capture(
            self.ctrl.capture_start_frame,
            self.ctrl.capture_end_frame,
            self.fps_spin.value(),
            self.settings,
            self.ctrl.fps,
        )
        self.statusBar().showMessage("開始批次截取...")

    def _on_batch_progress(self, current: int, total: int) -> None:
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        if self._preview_mode:
            self.controls.progress_label.setText(f"截取中: {current}/{total}")
            self.controls.progress_label.setVisible(True)

    def _on_batch_finished(self, success: int, total: int) -> None:
        self.progress_bar.setStyleSheet(PROGRESS_BAR_HIDDEN)
        self.progress_bar.setValue(total)
        self.controls.btn_range.setEnabled(True)
        self.controls.progress_label.setVisible(False)
        self.statusBar().showMessage(f"批次截取完成！共 {success}/{total} 張")

    # =========================================================================
    # 影片擷取
    # =========================================================================

    def _on_img_format_change(self) -> None:
        is_jpeg = self.img_format_combo.currentText() == "JPEG"
        self.quality_label.setEnabled(is_jpeg)
        self.quality_spin.setEnabled(is_jpeg)

    def _capture_video(self) -> None:
        start_time = self.vid_start_edit.text().strip()
        end_time = self.vid_end_edit.text().strip()

        start_sec, end_sec, err = self.ctrl.validate_export_times(start_time, end_time)
        if err:
            if self.isVisible():
                QMessageBox.warning(self, "提示", err)
            return
        assert start_sec is not None and end_sec is not None

        self.ctrl.update_capture_settings(self.settings.replace(
            output_dir=self.output_path_label.text(),
            prefix=self.prefix_edit.text().strip() or "capture",
            video_format=self.vid_format_combo.currentText(),
            keep_audio=self.keep_audio_check.isChecked(),
            use_stream_copy=self.copy_codec_check.isChecked(),
            video_codec=self.vid_codec_combo.currentText(),
            video_crf=self.vid_crf_spin.value(),
            video_preset=self.vid_preset_combo.currentText(),
            video_scale_width=self.vid_scale_combo.currentText(),
        ))

        self.progress_bar.setStyleSheet("")
        self.progress_bar.setMaximum(0)
        self.controls.btn_export.setEnabled(False)

        self.ctrl.start_video_export(start_sec, end_sec, self.settings)
        self.statusBar().showMessage("正在擷取影片...")

    def _on_export_finished(self, success: bool, message: str) -> None:
        self.progress_bar.setStyleSheet(PROGRESS_BAR_HIDDEN)
        self.progress_bar.setMaximum(100)
        self.controls.btn_export.setEnabled(True)
        self.controls.progress_label.setVisible(False)

        if success:
            self.statusBar().showMessage(f"影片擷取完成！{message}")
            if self.isVisible():
                QMessageBox.information(self, "完成", f"影片擷取完成！\n{message}")
        else:
            self.statusBar().showMessage("影片擷取失敗")
            if self.isVisible():
                QMessageBox.critical(self, "擷取失敗", message)

    # =========================================================================
    # GPS 功能
    # =========================================================================

    def _on_gps_position_updated(self, lat: float, lon: float) -> None:
        """收到 GPS 位置更新，同步地圖標記"""
        if self.map_view and self.map_view.isVisible():
            self.map_view.update_position(lat, lon)

    def _on_gps_ready(self, video_path: str, has_gps: bool, track_count: int) -> None:
        """GPS 背景偵測完成，更新選單/地圖/狀態列"""
        self._has_gps = has_gps
        self._update_gps_menu_state(has_gps)
        # 更新狀態列（保留硬體加速資訊）
        hw = self.ctrl.hw_backends
        hw_str = ", ".join(f"{name} ({dev})" for name, dev in hw) if hw else "無（軟體解碼）"
        if has_gps:
            gps_status = f"GPS: {track_count} 個點"
            self.statusBar().showMessage(f"就緒  |  硬體加速: {hw_str}  |  {gps_status}")
            if self.settings.show_gps_map:
                self._show_gps_map()
        else:
            self.statusBar().showMessage(f"就緒  |  硬體加速: {hw_str}  |  GPS: 無")

    def _toggle_gps_map(self, checked: bool) -> None:
        """開關 GPS 地圖顯示"""
        self.settings.show_gps_map = checked
        self._schedule_save()
        if checked:
            if not self._has_gps and self.ctrl.has_gps_manager:
                self.ctrl.start_gps_ocr_detection()
            else:
                self._show_gps_map()
        else:
            self._hide_gps_map()

    def _toggle_gps_skip(self, checked: bool) -> None:
        """開關跳過照片 GPS 疊加"""
        self.settings.skip_gps_overlay = checked
        self._schedule_save()

    def _show_gps_map(self) -> None:
        """顯示 GPS 地圖並載入軌跡"""
        if not self._has_gps or not self.map_view:
            return
        tracks = self.ctrl.get_gps_tracks()
        if not tracks:
            return
        tracks_dict = [t.to_dict() if hasattr(t, "to_dict") else t for t in tracks]
        self.map_view.setVisible(True)
        self.map_view.load_tracks(
            tracks_dict,
            provider=self.settings.map_provider,
            google_maps_key=self.settings.google_maps_api_key,
        )

    def _hide_gps_map(self) -> None:
        """隱藏 GPS 地圖"""
        if self.map_view:
            self.map_view.setVisible(False)

    def _update_gps_menu_state(self, has_gps: bool) -> None:
        """更新 GPS 選單啟用狀態"""
        self._gps_toggle_action.setEnabled(True)
        self._gps_export_action.setEnabled(has_gps)
        self._gps_roi_action.setEnabled(True)

    def _export_gpx(self) -> None:
        """匯出 GPX 軌跡檔"""
        if not self._has_gps:
            return
        path, _ = QFileDialog.getSaveFileName(self, "匯出 GPX", "", "GPX 檔案 (*.gpx)")
        if not path:
            return
        if self.ctrl.export_gpx(path):
            QMessageBox.information(self, "匯出完成", f"GPX 已儲存:\n{path}")
        else:
            QMessageBox.warning(self, "匯出失敗", "無法匯出 GPX 檔案")

    def _open_gps_roi(self) -> None:
        """開啟 GPS 文字位置設定對話框"""
        self.ctrl.open_roi_adjust_dialog()

    def _open_gps_settings(self) -> None:
        """開啟 GPS 設定對話框"""
        dlg = GPSSettingsDialog(self.settings, parent=self)
        if dlg.exec_():
            s = dlg.get_result()
            self.ctrl.update_capture_settings(s)
            self._gps_toggle_action.setChecked(s.show_gps_map)
            self._gps_skip_action.setChecked(s.skip_gps_overlay)
            if s.show_gps_map and self._has_gps:
                self._show_gps_map()
            else:
                self._hide_gps_map()
            self._schedule_save()

    # =========================================================================
    # 預覽模式：設定對話框
    # =========================================================================

    def _open_output_settings(self) -> None:
        dlg = OutputSettingsDialog(self.settings, parent=self)
        if dlg.exec_():
            s = dlg.get_result()
            self.ctrl.update_capture_settings(s)
            self.fps_spin.setValue(s.fps)
            self.res_combo.setCurrentIndex(max(0, self.res_combo.findText(s.resolution.value)))
            self.prefix_edit.setText(s.prefix)
            self.img_format_combo.setCurrentIndex(0 if s.img_format == "JPEG" else 1)
            self.quality_spin.setValue(s.jpeg_quality)
            self.output_path_label.setText(s.output_dir)
            self._on_img_format_change()
            self._schedule_save()

    def _open_capture_settings(self) -> None:
        dlg = CaptureSettingsDialog(
            self.settings,
            start_time=self.vid_start_edit.text(),
            end_time=self.vid_end_edit.text(),
            parent=self,
        )
        if dlg.exec_():
            s = dlg.get_result()
            self.ctrl.update_capture_settings(s)
            self.vid_format_combo.setCurrentIndex(0 if s.video_format == "MP4" else 1)
            self.keep_audio_check.setChecked(s.keep_audio)
            self.copy_codec_check.setChecked(s.use_stream_copy)
            self.vid_codec_combo.setCurrentIndex(0 if s.video_codec == "libx264" else 1)
            self.vid_crf_spin.setValue(s.video_crf)
            self.vid_preset_combo.setCurrentIndex(
                max(0, self.vid_preset_combo.findText(s.video_preset)),
            )
            self.vid_scale_combo.setCurrentIndex(
                max(0, self.vid_scale_combo.findText(s.video_scale_width)),
            )
            self.vid_start_edit.setText(dlg.start_time)
            self.vid_end_edit.setText(dlg.end_time)
            self._schedule_save()

    # =========================================================================
    # 工具
    # =========================================================================

    def _browse_output(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "選擇輸出資料夾", self.output_path_label.text())
        if d:
            self.output_path_label.setText(d)

    def _show_log(self) -> None:
        log_path = get_log_path()
        if not log_path or not os.path.exists(log_path):
            if self.isVisible():
                QMessageBox.information(self, "除錯日誌", "無日誌記錄")
            return

        try:
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            if self.isVisible():
                QMessageBox.warning(self, "錯誤", f"無法讀取日誌檔:\n{e}")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("除錯日誌")
        dlg.resize(720, 520)
        layout = QVBoxLayout(dlg)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setFont(QFont("Consolas", 9))
        text.setPlainText(content)
        layout.addWidget(text)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_close = QPushButton("關閉")
        btn_close.clicked.connect(dlg.accept)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)
        dlg.exec_()

    # =========================================================================
    # 關閉
    # =========================================================================

    def closeEvent(self, event) -> None:
        self.map_view.cleanup()
        self._save_settings()
        self.ctrl.shutdown()
        event.accept()

    def _quit(self) -> None:
        self.close()
