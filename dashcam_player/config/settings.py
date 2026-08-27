from __future__ import annotations

import functools
from typing import cast

from PyQt5.QtCore import QByteArray, QSettings

from dashcam_player.models.capture_settings import (
    CaptureSettings,
    ExportSettings,
    GpsDisplaySettings,
    ScreenshotSettings,
    SpeedSettings,
)
from dashcam_player.models.gps_roi import GPSROISetting
from dashcam_player.models.speed import CalibrationProfile


@functools.lru_cache(maxsize=1)
def _flat_capture_fields() -> dict[str, str]:
    """平鋪欄位名 → 型別註記字串（彙整自四個 sub-dataclass）。

    CaptureSettings 為組合式 dataclass，自身欄位只有
    screenshot/export/gps/speed；實際設定的平鋪名稱（fps、prefix…）
    定義在 sub-dataclass 中。
    """
    fields: dict[str, str] = {}
    for cls in (ScreenshotSettings, ExportSettings, GpsDisplaySettings, SpeedSettings):
        for f in cls.__dataclass_fields__.values():
            fields[f.name] = str(f.type)
    return fields


def _coerce_by_field_type(val: object, field_type: object) -> object:
    """依 dataclass 欄位型別還原 QSettings 讀回的值。

    Windows 原生後端會把 bool/int/float 存成字串（如 "true"/"95"），
    直接塞進 dataclass 會在下游（cv2.imencode 等）爆炸。
    還原失敗時回傳 None，呼叫端略過該 key 改用 dataclass 預設值。
    """
    type_name = (
        field_type if isinstance(field_type, str) else getattr(field_type, "__name__", "")
    )
    if isinstance(val, str):
        if type_name == "bool":
            return val.lower() in ("true", "1", "yes")
        if type_name == "int":
            try:
                return int(float(val))
            except ValueError:
                return None
        if type_name == "float":
            try:
                return float(val)
            except ValueError:
                return None
    # QSettings 可能回傳 float 而 dataclass 宣告 int
    if type_name == "int" and isinstance(val, float):
        return int(val)
    return val


class AppSettings:
    """應用設定持久化（使用 QSettings）"""

    ORGANIZATION = "DashcamTools"
    APPLICATION = "DashcamPlayer"

    def __init__(self) -> None:
        self._store = QSettings(self.ORGANIZATION, self.APPLICATION)

    # ---- 一般設定 ----

    @property
    def last_open_dir(self) -> str:
        return self._store.value("last_open_dir", "", type=str)

    @last_open_dir.setter
    def last_open_dir(self, value: str) -> None:
        self._store.setValue("last_open_dir", value)

    @property
    def last_output_dir(self) -> str:
        return self._store.value("last_output_dir", "", type=str)

    @last_output_dir.setter
    def last_output_dir(self, value: str) -> None:
        self._store.setValue("last_output_dir", value)

    @property
    def window_geometry(self) -> QByteArray | None:
        data = self._store.value("window_geometry")
        return data if isinstance(data, QByteArray) else None

    @window_geometry.setter
    def window_geometry(self, value: QByteArray) -> None:
        self._store.setValue("window_geometry", value)

    @property
    def window_state(self) -> QByteArray | None:
        data = self._store.value("window_state")
        return data if isinstance(data, QByteArray) else None

    @window_state.setter
    def window_state(self, value: QByteArray) -> None:
        self._store.setValue("window_state", value)

    # ---- Capture 設定 ----

    def load_capture_settings(self) -> CaptureSettings:
        """載入截圖設定。

        ⚠️ 平鋪欄位定義在 sub-dataclass（Screenshot/Export/GpsDisplay/Speed）
        而非 CaptureSettings 自身——後者的 __dataclass_fields__ 只有
        screenshot/export/gps/speed 四個組合欄位，直接迭代會一個設定都載不到。
        """
        data: dict = {}
        skip_keys = {"start_frame", "end_frame", "start_time", "end_time", "window_geometry"}
        flat_fields = _flat_capture_fields()
        for key in flat_fields:
            if key in skip_keys:
                continue
            val = self._store.value(f"capture/{key}")
            if val is not None:
                # QSettings 會把 bool/int/float 存成字串，依欄位型別還原
                coerced = _coerce_by_field_type(val, flat_fields[key])
                if coerced is not None:
                    data[key] = coerced
        return CaptureSettings.from_dict(data)

    def save_capture_settings(self, settings: CaptureSettings) -> None:
        for key, val in settings.to_dict().items():
            self._store.setValue(f"capture/{key}", val)

    # ---- Speed 設定 ----

    def load_speed_settings(self) -> SpeedSettings:
        """載入軟體測速設定"""
        data: dict = {}
        flat_fields = _flat_capture_fields()
        for key in flat_fields:
            if key in SpeedSettings.__dataclass_fields__:
                val = self._store.value(f"capture/{key}")
                if val is not None:
                    coerced = _coerce_by_field_type(val, flat_fields[key])
                    if coerced is not None:
                        data[key] = coerced
        return SpeedSettings(**data) if data else SpeedSettings()

    def save_speed_settings(self, settings: SpeedSettings) -> None:
        for key, val in settings.to_dict().items():
            if key in SpeedSettings.__dataclass_fields__:
                self._store.setValue(f"capture/{key}", val)

    # ---- GPS ROI 設定 ----

    def _roi_key(self, resolution_w: int, resolution_h: int, file_format: str) -> str:
        fmt = file_format.upper().lstrip(".")
        return f"gps_roi/{resolution_w}x{resolution_h}_{fmt}"

    def load_gps_roi(
        self, resolution_w: int, resolution_h: int, file_format: str
    ) -> GPSROISetting | None:
        key = self._roi_key(resolution_w, resolution_h, file_format)
        roi_x = self._store.value(f"{key}/roi_x")
        if roi_x is None:
            return None
        roi_y = self._store.value(f"{key}/roi_y")
        roi_w = self._store.value(f"{key}/roi_w")
        roi_h = self._store.value(f"{key}/roi_h")
        if None in (roi_y, roi_w, roi_h):
            return None

        return GPSROISetting(
            resolution_w=resolution_w,
            resolution_h=resolution_h,
            file_format=file_format,
            roi_x=float(roi_x),
            roi_y=float(roi_y),
            roi_w=float(roi_w),
            roi_h=float(roi_h),
            corner=self._store.value(f"{key}/corner", "", type=str),
            layout=self._store.value(f"{key}/layout", "", type=str),
            source=self._store.value(f"{key}/source", "user_adjusted", type=str),
            confidence=float(self._store.value(f"{key}/confidence", 1.0, type=float)),
            timestamp=float(self._store.value(f"{key}/timestamp", 0.0, type=float)),
        )

    def save_gps_roi(self, setting: GPSROISetting) -> None:
        key = self._roi_key(setting.resolution_w, setting.resolution_h, setting.file_format)
        self._store.setValue(f"{key}/roi_x", setting.roi_x)
        self._store.setValue(f"{key}/roi_y", setting.roi_y)
        self._store.setValue(f"{key}/roi_w", setting.roi_w)
        self._store.setValue(f"{key}/roi_h", setting.roi_h)
        self._store.setValue(f"{key}/corner", setting.corner)
        self._store.setValue(f"{key}/layout", setting.layout)
        self._store.setValue(f"{key}/source", setting.source)
        self._store.setValue(f"{key}/confidence", setting.confidence)
        self._store.setValue(f"{key}/timestamp", setting.timestamp)
        self._store.sync()

    # ---- Speed ROI 設定 ----

    def _speed_roi_key(self, resolution_w: int, resolution_h: int, file_format: str) -> str:
        fmt = file_format.upper().lstrip(".")
        return f"speed_roi/{resolution_w}x{resolution_h}_{fmt}"

    def load_speed_roi(
        self, resolution_w: int, resolution_h: int, file_format: str
    ) -> dict | None:
        key = self._speed_roi_key(resolution_w, resolution_h, file_format)
        x = self._store.value(f"{key}/x")
        if x is None:
            return None
        y = self._store.value(f"{key}/y")
        w = self._store.value(f"{key}/w")
        h = self._store.value(f"{key}/h")
        if None in (y, w, h):
            return None
        return {
            "x": float(x),
            "y": float(y),
            "w": float(w),
            "h": float(h),
            "corner": self._store.value(f"{key}/corner", "", type=str),
        }

    def save_speed_roi(self, roi: dict) -> None:
        # roi dict should have: resolution_w, resolution_h, file_format, x, y, w, h, corner
        key = self._speed_roi_key(roi["resolution_w"], roi["resolution_h"], roi["file_format"])
        self._store.setValue(f"{key}/x", roi["x"])
        self._store.setValue(f"{key}/y", roi["y"])
        self._store.setValue(f"{key}/w", roi["w"])
        self._store.setValue(f"{key}/h", roi["h"])
        self._store.setValue(f"{key}/corner", roi.get("corner", ""))
        self._store.sync()

    # ---- Speed Calibration 設定 ----

    def _calib_key(self, camera_profile: str, resolution_w: int, resolution_h: int, file_format: str) -> str:
        fmt = file_format.upper().lstrip(".")
        return f"speed_calibration/{camera_profile}/{resolution_w}x{resolution_h}_{fmt}"

    def load_speed_calibration(
        self, camera_profile: str, resolution_w: int, resolution_h: int, file_format: str
    ) -> CalibrationProfile | None:
        key = self._calib_key(camera_profile, resolution_w, resolution_h, file_format)
        ratio = self._store.value(f"{key}/ratio")
        if ratio is None:
            return None
        return CalibrationProfile(
            camera_profile=camera_profile,
            ratio=float(ratio),
            offset=float(self._store.value(f"{key}/offset", 0.0)),
            a=float(self._store.value(f"{key}/a", 1.0)),
            sample_count=int(self._store.value(f"{key}/sample_count", 0)),
            ratio_mad=float(self._store.value(f"{key}/ratio_mad", 0.0)),
            ratio_std=float(self._store.value(f"{key}/ratio_std", 0.0)),
            source_coverage=float(self._store.value(f"{key}/source_coverage", 0.0)),
            speed_min=float(self._store.value(f"{key}/speed_min", 0.0)),
            speed_max=float(self._store.value(f"{key}/speed_max", 0.0)),
            confidence=float(self._store.value(f"{key}/confidence", 0.0)),
            updated_at=float(self._store.value(f"{key}/updated_at", 0.0)),
        )

    def save_speed_calibration(self, profile: CalibrationProfile) -> None:
        key = self._calib_key(profile.camera_profile, profile.resolution_w, profile.resolution_h, profile.file_format)
        self._store.setValue(f"{key}/ratio", profile.ratio)
        self._store.setValue(f"{key}/offset", profile.offset)
        self._store.setValue(f"{key}/a", profile.a)
        self._store.setValue(f"{key}/sample_count", profile.sample_count)
        self._store.setValue(f"{key}/ratio_mad", profile.ratio_mad)
        self._store.setValue(f"{key}/ratio_std", profile.ratio_std)
        self._store.setValue(f"{key}/source_coverage", profile.source_coverage)
        self._store.setValue(f"{key}/speed_min", profile.speed_min)
        self._store.setValue(f"{key}/speed_max", profile.speed_max)
        self._store.setValue(f"{key}/confidence", profile.confidence)
        self._store.setValue(f"{key}/updated_at", profile.updated_at)
        self._store.setValue(f"{key}/resolution_w", profile.resolution_w)
        self._store.setValue(f"{key}/resolution_h", profile.resolution_h)
        self._store.setValue(f"{key}/file_format", profile.file_format)
        self._store.sync()

    # ---- GPS 訊號過濾 ----

    def load_gps_filter(self) -> tuple[bool, float]:
        """讀取 GPS 飄移過濾設定 (enabled, max_distance_m)。

        ⚠️ 不可用 value(..., type=float)：損毀字串會讓 PyQt 拋
        TypeError 而非回傳預設值；交由 _coerce_by_field_type 還原。
        """
        enabled = _coerce_by_field_type(
            self._store.value("capture/gps_filter_enabled", False), bool
        )
        max_d_raw = _coerce_by_field_type(
            self._store.value("capture/gps_filter_max_distance", 2000.0), float
        )
        max_d_candidate = cast("float | int | str | None", max_d_raw)
        try:
            max_d = float(max_d_candidate) if max_d_candidate is not None else 2000.0
        except (TypeError, ValueError):
            max_d = 2000.0
        if max_d <= 0:
            max_d = 2000.0
        return bool(enabled), max_d
