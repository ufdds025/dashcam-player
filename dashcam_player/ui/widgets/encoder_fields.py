"""編碼器選項欄位工廠——主視窗（唯讀顯示）與 CaptureSettingsDialog 共用。

單一來源：格式/編碼清單、CRF 範圍與 tooltip、preset 清單、縮放寬度選項。
工廠只負責「欄位形狀」（items / range / tooltip）；初始值與版面配置
由呼叫端依 CaptureSettings 套用，避免工廠耦合特定佈局。
"""

from __future__ import annotations

from PyQt5.QtWidgets import QComboBox, QSpinBox

from dashcam_player.models.capture_settings import SCALE_ORIGINAL

VIDEO_FORMATS: list[str] = ["MP4", "MKV"]
VIDEO_CODECS: list[str] = ["libx264", "libx265"]

CODEC_TOOLTIP = "libx264=廣泛相容, libx265=壓縮率更高但較慢"

CRF_MIN = 0
CRF_MAX = 51
CRF_TOOLTIP = (
    "0=無損, 18=視覺無損(預設), 23=好畫質, 28=可接受\n"
    "libx264 建議 18-23, libx265 建議 22-28"
)

PRESETS: list[str] = [
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
PRESET_TOOLTIP = "越快→檔案越大, 越慢→檔案越小"

SCALE_WIDTHS: list[str] = [SCALE_ORIGINAL, "1920", "1280", "854", "640"]
SCALE_TOOLTIP = "以寬度為基準等比例縮小（高度自動計算），維持原始比例不變形"


def make_format_combo() -> QComboBox:
    w = QComboBox()
    w.addItems(VIDEO_FORMATS)
    return w


def make_codec_combo() -> QComboBox:
    w = QComboBox()
    w.addItems(VIDEO_CODECS)
    w.setToolTip(CODEC_TOOLTIP)
    return w


def make_crf_spin() -> QSpinBox:
    w = QSpinBox()
    w.setRange(CRF_MIN, CRF_MAX)
    w.setToolTip(CRF_TOOLTIP)
    return w


def make_preset_combo() -> QComboBox:
    w = QComboBox()
    w.addItems(PRESETS)
    w.setToolTip(PRESET_TOOLTIP)
    return w


def make_scale_combo() -> QComboBox:
    w = QComboBox()
    w.addItems(SCALE_WIDTHS)
    w.setToolTip(SCALE_TOOLTIP)
    return w
