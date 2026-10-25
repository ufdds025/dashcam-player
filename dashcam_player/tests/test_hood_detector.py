"""車體上緣偵測測試（Phase B）。

- 純函式 `detect_hood_top_from_profile`：合成 V 曲線（路面→車體階躍）
- 端到端 `detect_hood_top`：程式生成合成影片（上暗下亮，車體在 0.65）
- 實片回歸：專案根目錄 PIC*.AVI（.gitignore 私人樣本，缺檔則 skip）
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from dashcam_player.utils.hood_detector import (
    CONSERVATIVE_FALLBACK_Y,
    detect_hood_top,
    detect_hood_top_from_profile,
    median_v_profile,
)

H = 1440
STEP_AT = 0.65


def _synth_profile(step_y: float = STEP_AT, road_v: float = 85.0,
                   hood_v: float = 130.0, h: int = H) -> np.ndarray:
    """合成中央欄 V 曲線：路面均值 + 車體階躍 + 微雜訊（固定種子）。"""
    rng = np.random.default_rng(7)
    p = np.full(h, road_v, dtype=np.float64)
    p[int(step_y * h):] = hood_v
    p += rng.normal(0, 2.0, h)
    return p


class TestDetectFromProfile:
    def test_step_detected(self) -> None:
        got = detect_hood_top_from_profile(_synth_profile())
        assert got is not None
        assert abs(got - STEP_AT) < 0.02

    def test_takes_first_step_not_strongest(self) -> None:
        """0.65 小階躍 + 0.78 更大階躍 → 必須取第一個（Phase A 關鍵語意）。"""
        p = _synth_profile()
        p[int(0.78 * H):] += 40.0  # 第二階躍更強
        got = detect_hood_top_from_profile(p)
        assert got is not None
        assert abs(got - STEP_AT) < 0.02

    def test_flat_returns_none(self) -> None:
        rng = np.random.default_rng(3)
        p = np.full(H, 85.0) + rng.normal(0, 2.0, H)
        assert detect_hood_top_from_profile(p) is None

    def test_dark_below_returns_none(self) -> None:
        """下方是暗區（儀表板）而非亮車體 → 不誤判。"""
        rng = np.random.default_rng(5)
        p = np.full(H, 85.0) + rng.normal(0, 2.0, H)
        p[int(0.70 * H):] = 50.0
        assert detect_hood_top_from_profile(p) is None

    def test_too_short_returns_none(self) -> None:
        assert detect_hood_top_from_profile(np.full(40, 85.0)) is None

    def test_weak_step_returns_none(self) -> None:
        p = _synth_profile(road_v=85.0, hood_v=100.0)  # ΔV=15 < 門檻 25
        assert detect_hood_top_from_profile(p) is None

    def test_gradual_ramp_caught_by_wide_window(self) -> None:
        """緩坡車體（D 槽 091049-F 實測）：+40 攤在 0.10H 上，窄窗內僅 +12。"""
        rng = np.random.default_rng(9)
        p = np.full(H, 95.0, dtype=np.float64)
        y0, y1 = int(0.65 * H), int(0.75 * H)
        p[y0:y1] = np.linspace(95.0, 135.0, y1 - y0)
        p[y1:] = 135.0
        p += rng.normal(0, 2.0, H)
        got = detect_hood_top_from_profile(p)
        assert got is not None
        assert 0.63 <= got <= 0.72

    def test_wide_window_only_scanned_once(self) -> None:
        """傳入窗寬已 >= 寬窗時只掃一次（同樣檢出，不重複掃描）。"""
        got = detect_hood_top_from_profile(_synth_profile(), step_window_ratio=0.06)
        assert got is not None
        assert abs(got - STEP_AT) < 0.03

    def test_detect_with_step_returns_actual_window_step(self) -> None:
        """_detect_with_step 回傳實際檢出窗的 step（緩坡的窄窗 step 僅 +12，
        confidence 不可用窄窗重算，否則寬窗檢出會被誤算成 0）。"""
        from dashcam_player.utils.hood_detector import _detect_with_step

        rng = np.random.default_rng(9)
        p = np.full(H, 95.0, dtype=np.float64)
        y0, y1 = int(0.65 * H), int(0.75 * H)
        p[y0:y1] = np.linspace(95.0, 135.0, y1 - y0)
        p[y1:] = 135.0
        p += rng.normal(0, 2.0, H)
        y, step = _detect_with_step(p)
        assert y is not None and step is not None
        assert step >= 20.0  # 寬窗 step 達寬窗門檻（窄窗僅 ~12）


class TestMedianVProfile:
    def test_needs_min_samples(self) -> None:
        assert median_v_profile([]) is None
        one = [np.zeros((100, 100, 3), dtype=np.uint8)]
        assert median_v_profile(one) is None

    def test_filters_transient(self) -> None:
        """移動亮塊（暫態車輛）應被時間中位數濾除。"""
        base_road = np.full((200, 300, 3), 85, dtype=np.uint8)
        base_hood = np.full((200, 300, 3), 130, dtype=np.uint8)
        frames = []
        for i in range(5):
            f = np.vstack([base_road[:130], base_hood[:70]])
            if i == 2:  # 單幀在路面區加亮塊（模擬經過的白車）
                f[40:80, 100:200] = 200
            frames.append(f)
        p = median_v_profile(frames)
        assert p is not None
        # 路面區中位數不受單幀亮塊污染
        assert float(np.median(p[40:80])) < 100.0

    def test_mixed_shapes_drops_mismatch(self) -> None:
        """中途解析度變化時丟棄異形幀（不拋 ValueError）。"""
        good = [np.full((200, 300, 3), 85, dtype=np.uint8) for _ in range(4)]
        bad = [np.full((100, 300, 3), 85, dtype=np.uint8)]
        p = median_v_profile(good + bad)
        assert p is not None
        assert len(p) == 200
        # 剩餘不足 MIN_SAMPLES 時回 None
        assert median_v_profile(bad + good[:1]) is None


def _write_synth_video(path: str, w: int = 640, h: int = 480, n: int = 60) -> None:
    """寫合成影片：上 65% 暗路面、下 35% 亮車體（皆 BGR 灰階等值）。"""
    import cv2

    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (w, h))
    assert writer.isOpened()
    try:
        rng = np.random.default_rng(11)
        for _ in range(n):
            f = np.full((h, w, 3), 85, dtype=np.uint8)
            f[int(0.65 * h):] = 130
            noise = rng.integers(-3, 4, (h, w, 1)).astype(np.int16)
            f = np.clip(f.astype(np.int16) + noise, 0, 255).astype(np.uint8)
            writer.write(f)
    finally:
        writer.release()


class TestDetectEndToEnd:
    def test_synth_video(self, tmp_path) -> None:
        path = str(tmp_path / "hood_synth.mp4")
        _write_synth_video(path)
        res = detect_hood_top(path)
        assert res is not None
        assert abs(res.hood_top_y - 0.65) < 0.03
        assert 0.0 <= res.confidence <= 1.0
        assert res.n_samples >= 3

    def test_gradual_ramp_video_confidence_positive(self, tmp_path) -> None:
        """緩坡車體影片：寬窗檢出的 confidence 必須 >0（不可用窄窗重算）。"""
        import cv2

        path = str(tmp_path / "hood_ramp.mp4")
        w, h, n = 320, 240, 36
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (w, h))
        assert writer.isOpened()
        try:
            rng = np.random.default_rng(21)
            col = np.full((h, 1), 95.0)
            y0, y1 = int(0.65 * h), int(0.75 * h)
            col[y0:y1, 0] = np.linspace(95.0, 135.0, y1 - y0)
            col[y1:, 0] = 135.0
            for _ in range(n):
                f = np.repeat(col.astype(np.uint8), w, axis=1)
                f = np.stack([f, f, f], axis=-1)
                noise = rng.integers(-3, 4, (h, w, 1)).astype(np.int16)
                f = np.clip(f.astype(np.int16) + noise, 0, 255).astype(np.uint8)
                writer.write(f)
        finally:
            writer.release()
        res = detect_hood_top(path)
        assert res is not None
        assert 0.60 <= res.hood_top_y <= 0.78
        assert res.confidence > 0.0

    def test_missing_file_returns_none(self, tmp_path) -> None:
        assert detect_hood_top(str(tmp_path / "不存在.mp4")) is None


# --- 實片回歸（私人樣本，缺檔 skip；不進 CI 斷言門檻，僅防迴歸） ---
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REAL_CASES = [  # (檔名, 期望 hood_top, 容忍)
    ("PIC20260825-180655-F.AVI", 0.650, 0.04),
    ("PIC20260825-180755-F.AVI", 0.668, 0.04),
    ("PIC20260826-091548-F.AVI", 0.643, 0.045),
    ("PIC20260826-092148-F.AVI", 0.653, 0.04),
]


@pytest.mark.parametrize("name,expected,tol", _REAL_CASES)
def test_real_front_videos(name: str, expected: float, tol: float) -> None:
    path = os.path.join(_PROJ_ROOT, name)
    if not os.path.exists(path):
        pytest.skip(f"私人樣本缺檔：{name}")
    res = detect_hood_top(path)
    assert res is not None, f"{name} 應偵測到車體上緣"
    assert abs(res.hood_top_y - expected) <= tol


def test_real_rear_video_no_false_positive() -> None:
    path = os.path.join(_PROJ_ROOT, "PIC20260826-091548-R.AVI")
    if not os.path.exists(path):
        pytest.skip("私人樣本缺檔：PIC20260826-091548-R.AVI")
    assert detect_hood_top(path) is None


def test_fallback_constant_sane() -> None:
    """保守預設須在人工框底邊最小值附近（0.643）之下。"""
    assert 0.60 <= CONSERVATIVE_FALLBACK_Y <= 0.65


class TestUnionClamp:
    """SpeedROIDetector union ROI 車體 clamp（Phase C）。"""

    def _det(self):
        from dashcam_player.utils.speed_roi_detector import SpeedROIDetector

        return SpeedROIDetector("dummy.mp4")

    def test_bottom_clamped_above_hood(self) -> None:
        # 候選框底邊 0.88（D 槽實測的滿寬 union 狀況），hood 0.66
        boxes = [[0, int(0.40 * 1440), 2560, int(0.48 * 1440)]]
        (rx, ry, rw, rh), cut = self._det()._compute_union_roi(
            boxes, 2560, 1440, hood_top_y=0.66
        )
        assert ry + rh <= 0.66 - 0.02 + 1e-9
        assert cut > 0.30  # 切掉大半 → 呼叫端應降信心

    def test_no_hood_no_clamp(self) -> None:
        """無車體（後鏡頭）不限縮。"""
        boxes = [[100, 800, 400, 260]]
        (rx, ry, rw, rh), cut = self._det()._compute_union_roi(
            boxes, 2560, 1440, hood_top_y=None
        )
        assert cut == 0.0
        assert ry + rh > 0.70  # 保持原 union（含 padding）

    def test_empty_returns_default(self) -> None:
        roi, cut = self._det()._compute_union_roi([], 2560, 1440, hood_top_y=0.66)
        assert roi == (0.05, 0.40, 0.90, 0.45)
        assert cut == 0.0


class TestWorkerRoiValidation:
    """SpeedWorker 存檔 ROI 跨機器污染驗證（Phase C）。"""

    def _worker(self, roi):
        from dashcam_player.core.speed_worker import SpeedWorker

        w = SpeedWorker()
        w._path = "dummy.mp4"
        w._roi = roi
        return w

    def _hood(self, y):
        from dashcam_player.utils.hood_detector import HoodDetectionResult

        return HoodDetectionResult(video_path="x", hood_top_y=y, confidence=0.8, n_samples=12)

    def test_polluted_roi_discarded(self, monkeypatch) -> None:
        import dashcam_player.core.speed_worker as sw

        monkeypatch.setattr(sw, "detect_hood_top", lambda _p: self._hood(0.65))
        w = self._worker((0.05, 0.45, 0.90, 0.30))  # 底邊 0.75，深入車體
        w._validate_roi_against_hood()
        assert w._roi is None

    def test_clean_roi_kept(self, monkeypatch) -> None:
        import dashcam_player.core.speed_worker as sw

        monkeypatch.setattr(sw, "detect_hood_top", lambda _p: self._hood(0.65))
        roi = (0.42, 0.55, 0.40, 0.10)  # 底邊 0.65，人工框等級
        w = self._worker(roi)
        w._validate_roi_against_hood()
        assert w._roi == roi

    def test_no_hood_signature_keeps_roi(self, monkeypatch) -> None:
        """無 hood 簽名（後鏡頭）→ 不驗證，保留存檔框。"""
        import dashcam_player.core.speed_worker as sw

        monkeypatch.setattr(sw, "detect_hood_top", lambda _p: None)
        roi = (0.05, 0.45, 0.90, 0.30)
        w = self._worker(roi)
        w._validate_roi_against_hood()
        assert w._roi == roi

    def test_no_saved_roi_no_detection(self, monkeypatch) -> None:
        """無存檔 ROI 時不跑偵測（省 12 幀解碼）。"""
        import dashcam_player.core.speed_worker as sw

        called: list[str] = []

        def _spy(p: str):
            called.append(p)
            return self._hood(0.65)

        monkeypatch.setattr(sw, "detect_hood_top", _spy)
        w = self._worker(None)
        w._validate_roi_against_hood()
        assert called == []
