from __future__ import annotations

from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from dashcam_player.models.capture_settings import CaptureSettings
from dashcam_player.models.resolution import Resolution


class OutputSettingsDialog(QDialog):
    def __init__(
        self,
        settings: CaptureSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._original = settings
        self._result: CaptureSettings | None = None
        self.setWindowTitle("輸出設定")
        self.setMinimumWidth(380)

        layout = QGridLayout(self)
        layout.setSpacing(6)

        row = 0
        layout.addWidget(QLabel("每秒擷取幀數:"), row, 0)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 9999)
        self.fps_spin.setValue(settings.fps)
        layout.addWidget(self.fps_spin, row, 1)

        row += 1
        layout.addWidget(QLabel("輸出解析度:"), row, 0)
        self.res_combo = QComboBox()
        self.res_combo.addItems(Resolution.values())
        res_val = settings.resolution
        if isinstance(res_val, Resolution):
            res_str = res_val.value
        else:
            res_str = str(res_val)
        idx = max(0, self.res_combo.findText(res_str))
        self.res_combo.setCurrentIndex(idx)
        layout.addWidget(self.res_combo, row, 1)

        row += 1
        layout.addWidget(QLabel("檔名前綴 (選用):"), row, 0)
        self.prefix_edit = QLineEdit(settings.prefix)
        self.prefix_edit.setToolTip(
            "選用前標，會加在來源影片名稱之前。\n"
            "來源檔名與幀編號會自動加入檔名以識別來源，無須在此填寫。"
        )
        layout.addWidget(self.prefix_edit, row, 1)

        row += 1
        layout.addWidget(QLabel("截圖格式:"), row, 0)
        self.img_format_combo = QComboBox()
        self.img_format_combo.addItems(["JPEG", "PNG"])
        self.img_format_combo.setCurrentIndex(0 if settings.img_format == "JPEG" else 1)
        self.img_format_combo.currentIndexChanged.connect(self._on_format_change)
        layout.addWidget(self.img_format_combo, row, 1)

        row += 1
        self.quality_label = QLabel("JPEG 品質:")
        layout.addWidget(self.quality_label, row, 0)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(settings.jpeg_quality)
        layout.addWidget(self.quality_spin, row, 1)
        self._on_format_change()

        row += 1
        layout.addWidget(QLabel("輸出資料夾:"), row, 0)
        path_layout = QHBoxLayout()
        self.output_label = QLabel(settings.output_dir)
        self.output_label.setFont(QFont("Consolas", 9))
        self.output_label.setWordWrap(True)
        btn_browse = QPushButton("瀏覽...")
        btn_browse.clicked.connect(self._browse)
        path_layout.addWidget(self.output_label, 1)
        path_layout.addWidget(btn_browse)
        layout.addLayout(path_layout, row, 1)

        row += 1
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self._accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box, row, 0, 1, 2)

    def _on_format_change(self) -> None:
        is_jpeg = self.img_format_combo.currentText() == "JPEG"
        self.quality_label.setEnabled(is_jpeg)
        self.quality_spin.setEnabled(is_jpeg)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self,
            "選擇輸出資料夾",
            self.output_label.text(),
        )
        if d:
            self.output_label.setText(d)

    def _accept(self) -> None:
        self._result = self._original.replace(
            fps=self.fps_spin.value(),
            resolution=Resolution.from_value(self.res_combo.currentText()),
            prefix=self.prefix_edit.text().strip(),
            img_format=self.img_format_combo.currentText(),
            jpeg_quality=self.quality_spin.value(),
            output_dir=self.output_label.text(),
        )
        self.accept()

    def get_result(self) -> CaptureSettings:
        return self._result if self._result is not None else self._original
