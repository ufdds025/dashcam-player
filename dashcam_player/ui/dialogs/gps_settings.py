from __future__ import annotations

from PyQt5.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dashcam_player.models.capture_settings import CaptureSettings


class GPSSettingsDialog(QDialog):
    def __init__(
        self,
        settings: CaptureSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._original = settings
        self._result: CaptureSettings | None = None
        self.setWindowTitle("GPS 設定")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.show_map_check = QCheckBox("顯示 GPS 軌跡地圖")
        self.show_map_check.setChecked(settings.show_gps_map)
        layout.addWidget(self.show_map_check)

        self.skip_overlay_check = QCheckBox("影片已有 GPS 浮水印 (跳過照片疊加)")
        self.skip_overlay_check.setChecked(settings.skip_gps_overlay)
        self.skip_overlay_check.setToolTip(
            "若影片已內建 GPS 座標浮水印，勾選可避免疊加重複",
        )
        layout.addWidget(self.skip_overlay_check)

        layout.addSpacing(8)

        filter_group = QGroupBox("GPS 訊號過濾")
        filter_layout = QVBoxLayout(filter_group)

        self.filter_check = QCheckBox("啟用 GPS 訊號漂移過濾")
        self.filter_check.setChecked(settings.gps_filter_enabled)
        self.filter_check.setToolTip(
            "忽略與前後位置差距過大的異常 GPS 訊號（GPS 飄移）",
        )
        self.filter_check.toggled.connect(self._on_filter_toggle)
        filter_layout.addWidget(self.filter_check)

        dist_layout = QHBoxLayout()
        dist_layout.addWidget(QLabel("最大允許距離 (公尺):"))
        self.max_dist_spin = QDoubleSpinBox()
        self.max_dist_spin.setRange(1.0, 100000.0)
        self.max_dist_spin.setDecimals(0)
        self.max_dist_spin.setSingleStep(100.0)
        self.max_dist_spin.setValue(settings.gps_filter_max_distance)
        self.max_dist_spin.setSuffix(" m")
        self.max_dist_spin.setEnabled(settings.gps_filter_enabled)
        dist_layout.addWidget(self.max_dist_spin)
        dist_layout.addStretch()
        filter_layout.addLayout(dist_layout)

        layout.addWidget(filter_group)

        layout.addSpacing(8)

        speed_group = QGroupBox("視覺測速")
        speed_layout = QVBoxLayout(speed_group)

        self.speed_enabled_check = QCheckBox("啟動視覺測速（地面標線偵測）")
        self.speed_enabled_check.setChecked(settings.speed.enabled)
        self.speed_enabled_check.setToolTip("使用光流法偵測地面標線移动速度")
        speed_layout.addWidget(self.speed_enabled_check)

        dist_layout2 = QHBoxLayout()
        dist_layout2.addWidget(QLabel("每次掃描距離:"))
        self.speed_distance_spin = QDoubleSpinBox()
        self.speed_distance_spin.setRange(1.0, 100.0)
        self.speed_distance_spin.setDecimals(1)
        self.speed_distance_spin.setSingleStep(5.0)
        self.speed_distance_spin.setValue(settings.speed.distance_per_cycle)
        self.speed_distance_spin.setSuffix(" m")
        self.speed_distance_spin.setEnabled(settings.speed.enabled)
        dist_layout2.addWidget(self.speed_distance_spin)
        dist_layout2.addStretch()
        speed_layout.addLayout(dist_layout2)

        smooth_layout = QHBoxLayout()
        smooth_layout.addWidget(QLabel("平滑窗口:"))
        self.speed_smooth_spin = QSpinBox()
        self.speed_smooth_spin.setRange(1, 30)
        self.speed_smooth_spin.setValue(settings.speed.smoothing_window)
        self.speed_smooth_spin.setEnabled(settings.speed.enabled)
        smooth_layout.addWidget(self.speed_smooth_spin)
        smooth_layout.addStretch()
        speed_layout.addLayout(smooth_layout)

        self.speed_burn_check = QCheckBox("匯出時燒入速度文字")
        self.speed_burn_check.setChecked(settings.speed.burn_speed_on_export)
        self.speed_burn_check.setEnabled(settings.speed.enabled)
        speed_layout.addWidget(self.speed_burn_check)

        self.speed_enabled_check.toggled.connect(self._on_speed_toggle)

        layout.addWidget(speed_group)

        layout.addSpacing(8)

        map_group = QGroupBox("地圖來源")
        map_layout = QVBoxLayout(map_group)

        self.osm_radio = QPushButton("OpenStreetMap (預設，免費)")
        self.osm_radio.setCheckable(True)
        self.osm_radio.setChecked(settings.map_provider == "osm")
        map_layout.addWidget(self.osm_radio)

        self.google_radio = QPushButton("Google Maps (需 API Key)")
        self.google_radio.setCheckable(True)
        self.google_radio.setChecked(settings.map_provider == "google")
        map_layout.addWidget(self.google_radio)

        self._radio_group = QButtonGroup()
        self._radio_group.addButton(self.osm_radio)
        self._radio_group.addButton(self.google_radio)

        api_layout = QHBoxLayout()
        api_layout.addWidget(QLabel("API Key:"))
        self.api_key_edit = QLineEdit(settings.google_maps_api_key)
        self.api_key_edit.setPlaceholderText("輸入 Google Maps API Key")
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setEnabled(settings.map_provider == "google")
        api_layout.addWidget(self.api_key_edit)
        self._show_key_btn = QPushButton("顯示")
        self._show_key_btn.setFixedWidth(40)
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.toggled.connect(self._toggle_api_key_visible)
        api_layout.addWidget(self._show_key_btn)
        map_layout.addLayout(api_layout)

        layout.addWidget(map_group)

        self.osm_radio.toggled.connect(self._on_provider_change)
        self.google_radio.toggled.connect(self._on_provider_change)

        layout.addStretch()

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self._accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _on_provider_change(self) -> None:
        is_google = self.google_radio.isChecked()
        self.api_key_edit.setEnabled(is_google)

    def _on_filter_toggle(self, checked: bool) -> None:
        self.max_dist_spin.setEnabled(checked)

    def _on_speed_toggle(self, checked: bool) -> None:
        self.speed_distance_spin.setEnabled(checked)
        self.speed_smooth_spin.setEnabled(checked)
        self.speed_burn_check.setEnabled(checked)

    def _toggle_api_key_visible(self, checked: bool) -> None:
        self.api_key_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        self._show_key_btn.setText("隱藏" if checked else "顯示")

    def _accept(self) -> None:
        self._result = self._original.replace(
            show_gps_map=self.show_map_check.isChecked(),
            skip_gps_overlay=self.skip_overlay_check.isChecked(),
            map_provider="google" if self.google_radio.isChecked() else "osm",
            google_maps_api_key=self.api_key_edit.text().strip(),
            gps_filter_enabled=self.filter_check.isChecked(),
            gps_filter_max_distance=self.max_dist_spin.value(),
            speed_enabled=self.speed_enabled_check.isChecked(),
            speed_distance_per_cycle=self.speed_distance_spin.value(),
            speed_smoothing_window=self.speed_smooth_spin.value(),
            speed_burn_speed_on_export=self.speed_burn_check.isChecked(),
        )
        self.accept()

    def get_result(self) -> CaptureSettings:
        return self._result if self._result is not None else self._original
