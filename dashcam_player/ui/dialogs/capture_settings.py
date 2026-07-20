from __future__ import annotations

from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from dashcam_player.models.capture_settings import CaptureSettings


class CaptureSettingsDialog(QDialog):
    def __init__(
        self,
        settings: CaptureSettings,
        start_time: str = "",
        end_time: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._original = settings
        self._result: CaptureSettings | None = None
        self.setWindowTitle("影片擷取設定")
        self.setMinimumWidth(380)

        layout = QGridLayout(self)
        layout.setSpacing(6)

        row = 0
        layout.addWidget(QLabel("輸出格式:"), row, 0)
        self.vid_format_combo = QComboBox()
        self.vid_format_combo.addItems(["MP4", "MKV"])
        self.vid_format_combo.setCurrentIndex(
            0 if settings.video_format == "MP4" else 1,
        )
        layout.addWidget(self.vid_format_combo, row, 1)

        row += 1
        self.keep_audio_check = QCheckBox("保留音軌")
        self.keep_audio_check.setChecked(settings.keep_audio)
        layout.addWidget(self.keep_audio_check, row, 0, 1, 2)

        row += 1
        self.copy_codec_check = QCheckBox("使用原始編碼（stream copy）")
        self.copy_codec_check.setChecked(settings.use_stream_copy)
        layout.addWidget(self.copy_codec_check, row, 0, 1, 2)

        self._enc_widgets: list[QWidget] = []

        row += 1
        lbl = QLabel("── 重新編碼設定 ──")
        layout.addWidget(lbl, row, 0, 1, 2)
        self._enc_widgets.append(lbl)

        row += 1
        lbl = QLabel("編碼格式:")
        layout.addWidget(lbl, row, 0)
        self._enc_widgets.append(lbl)
        self.vid_codec_combo = QComboBox()
        self.vid_codec_combo.addItems(["libx264", "libx265"])
        self.vid_codec_combo.setCurrentIndex(
            0 if settings.video_codec == "libx264" else 1,
        )
        self.vid_codec_combo.setToolTip("libx264=廣泛相容, libx265=壓縮率更高但較慢")
        layout.addWidget(self.vid_codec_combo, row, 1)
        self._enc_widgets.append(self.vid_codec_combo)

        row += 1
        lbl = QLabel("CRF 品質:")
        layout.addWidget(lbl, row, 0)
        self._enc_widgets.append(lbl)
        self.vid_crf_spin = QSpinBox()
        self.vid_crf_spin.setRange(0, 51)
        self.vid_crf_spin.setValue(settings.video_crf)
        self.vid_crf_spin.setToolTip(
            "0=無損, 18=視覺無損(預設), 23=好畫質, 28=可接受\n"
            "libx264 建議 18-23, libx265 建議 22-28"
        )
        layout.addWidget(self.vid_crf_spin, row, 1)
        self._enc_widgets.append(self.vid_crf_spin)

        row += 1
        lbl = QLabel("編碼預設:")
        layout.addWidget(lbl, row, 0)
        self._enc_widgets.append(lbl)
        self.vid_preset_combo = QComboBox()
        self.vid_preset_combo.addItems(
            [
                "ultrafast",
                "superfast",
                "veryfast",
                "faster",
                "fast",
                "medium",
                "slow",
                "slower",
                "veryslow",
            ]
        )
        idx = max(0, self.vid_preset_combo.findText(settings.video_preset))
        self.vid_preset_combo.setCurrentIndex(idx)
        self.vid_preset_combo.setToolTip("越快→檔案越大, 越慢→檔案越小")
        layout.addWidget(self.vid_preset_combo, row, 1)
        self._enc_widgets.append(self.vid_preset_combo)

        row += 1
        lbl = QLabel("縮放寬度:")
        layout.addWidget(lbl, row, 0)
        self._enc_widgets.append(lbl)
        self.vid_scale_combo = QComboBox()
        self.vid_scale_combo.addItems(["原始", "1920", "1280", "854", "640"])
        idx = max(0, self.vid_scale_combo.findText(settings.video_scale_width))
        self.vid_scale_combo.setCurrentIndex(idx)
        self.vid_scale_combo.setToolTip(
            "以寬度為基準等比例縮小（高度自動計算），維持原始比例不變形",
        )
        layout.addWidget(self.vid_scale_combo, row, 1)
        self._enc_widgets.append(self.vid_scale_combo)

        def _toggle_enc(checked: bool) -> None:
            for w in self._enc_widgets:
                w.setVisible(not checked)

        self.copy_codec_check.toggled.connect(_toggle_enc)
        _toggle_enc(self.copy_codec_check.isChecked())

        row += 1
        layout.addWidget(QLabel("開始時間:"), row, 0)
        self.vid_start_edit = QLineEdit(start_time)
        self.vid_start_edit.setPlaceholderText("hh:mm:ss（空=用標記）")
        layout.addWidget(self.vid_start_edit, row, 1)

        row += 1
        layout.addWidget(QLabel("結束時間:"), row, 0)
        self.vid_end_edit = QLineEdit(end_time)
        self.vid_end_edit.setPlaceholderText("hh:mm:ss（空=用標記）")
        layout.addWidget(self.vid_end_edit, row, 1)

        row += 1
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self._accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box, row, 0, 1, 2)

    def _accept(self) -> None:
        self._result = self._original.replace(
            video_format=self.vid_format_combo.currentText(),
            keep_audio=self.keep_audio_check.isChecked(),
            use_stream_copy=self.copy_codec_check.isChecked(),
            video_codec=self.vid_codec_combo.currentText(),
            video_crf=self.vid_crf_spin.value(),
            video_preset=self.vid_preset_combo.currentText(),
            video_scale_width=self.vid_scale_combo.currentText(),
        )
        self.accept()

    def get_result(self) -> CaptureSettings:
        return self._result if self._result is not None else self._original

    @property
    def start_time(self) -> str:
        return self.vid_start_edit.text()

    @property
    def end_time(self) -> str:
        return self.vid_end_edit.text()
