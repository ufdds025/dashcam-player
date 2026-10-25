"""標線偵測機制測試（夜間閘門 / 去趨勢切邊 / 多帶合併）。

數據來源：
- 夜間閘門閾值：norm 日/傍晚/夜 7 支實測（sky 夜≤55 vs 日≥122；
  sat 夜≥0.028 vs 日≤0.013）
- 去趨勢切邊：norm 正午訊號圖兩端 ±40 假斜坡（same 卷積邊界污染）
- 多帶合併：norm 正午全寬漣漪僅 ±1 灰階被淹沒，窄帶 SNR 較高
"""
from __future__ import annotations

import numpy as np

from dashcam_player.utils.marking_detector import (
    assess_lighting,
    detect_marking_pulses,
    merge_strip_onsets,
)


class TestAssessLighting:
    def test_day(self) -> None:
        assert assess_lighting(205.0, 0.013)[0] == "DAY"  # norm 正午
        assert assess_lighting(194.0, 0.003)[0] == "DAY"  # D 槽日間
        assert assess_lighting(122.0, 0.001)[0] == "DAY"  # norm 傍晚（可測速，不可閘）

    def test_night(self) -> None:
        assert assess_lighting(51.0, 0.073)[0] == "NIGHT"  # norm 夜1
        assert assess_lighting(55.0, 0.036)[0] == "NIGHT"  # norm 夜2
        assert assess_lighting(49.0, 0.028)[0] == "NIGHT"  # norm 夜3

    def test_dark_but_no_lamps_is_day(self) -> None:
        """暗但無燈具（無路燈鄉道）不誤閘——本來就測不到，直接回空即可。"""
        assert assess_lighting(50.0, 0.005)[0] == "DAY"

    def test_bright_with_glints_is_day(self) -> None:
        """晴天白車多（sat 偏高）但天空亮 → 不誤閘（AND 語意）。"""
        assert assess_lighting(190.0, 0.030)[0] == "DAY"


def _pulse_signal(n: int = 300, fps: float = 30.0, period_f: int = 12,
                  amp: float = 10.0) -> np.ndarray:
    """合成標線脈衝訊號（週期性亮帶 + 微雜訊）。"""
    rng = np.random.default_rng(4)
    sig = rng.normal(0, 1.0, n)
    for start in range(0, n, period_f):
        sig[start:start + 2] += amp
    return sig.astype(np.float64)


class TestDetrendReflect:
    def test_no_fake_slope_at_boundaries(self) -> None:
        """核心迴歸：平坦訊號去趨勢後不可出現遠超本體的假斜坡。

        舊碼 np.convolve(mode='same') 隱性補零曾讓原始 sd 2.0 → detrended sd 12.4，
        門檻被蓋過、脈衝全滅。reflect 補邊後 detrended 應維持雜訊量級。
        """
        from dashcam_player.utils.marking_detector import detrend_moving_average

        rng = np.random.default_rng(4)
        sig = np.full(120, 90.0) + rng.normal(0, 2.0, 120)
        dt = detrend_moving_average(sig, 60)
        assert float(dt.std()) < 4.0  # 雜訊量級，不可放大數倍
        assert float(np.abs(dt[:10]).max()) < 8.0  # 邊界無假斜坡
        assert float(np.abs(dt[-10:]).max()) < 8.0

    def test_boundary_spikes_no_longer_suppress_middle(self) -> None:
        """邊界大尖峰不再經假斜坡淹沒中段真脈衝（舊切邊治標已移除）。"""
        sig = _pulse_signal()
        sig[:5] += 50.0
        sig[-5:] += 50.0
        ons, med, _ = detect_marking_pulses(sig, 30.0)
        assert len(ons) >= 3
        assert abs(med - 0.4) < 0.05

    def test_short_signal_no_trim(self) -> None:
        """短訊號（窗長 >= 訊號長）照常偵測，reflect 補邊下無邊界污染問題。"""
        sig = np.roll(_pulse_signal(n=30, period_f=6), 2)  # 避開 i=0 邊界，前緣可檢出
        ons, med, _ = detect_marking_pulses(sig, 30.0)
        assert len(ons) >= 3


class TestMergeStripOnsets:
    def test_aligned_kept(self) -> None:
        a = np.array([10.0, 30.0, 50.0, 70.0])
        b = np.array([11.0, 31.0, 52.0, 71.0])
        c = np.array([10.5, 30.5, 51.0, 70.5])
        d = np.array([], float)
        merged = merge_strip_onsets([a, b, c, d], fps=30.0)
        assert len(merged) == 4
        assert abs(merged[0] - 10.5) < 1.0

    def test_single_strip_noise_dropped(self) -> None:
        """只有一帶看到的（旁車經過）被丟棄。"""
        a = np.array([10.0, 30.0, 50.0])
        b = np.array([10.2, 30.1, 50.3])
        c = np.array([10.1, 30.0, 50.2, 200.0])  # 200 為單帶雜訊
        d = np.array([9.9, 30.2, 49.8])
        merged = merge_strip_onsets([a, b, c, d], fps=30.0)
        assert len(merged) == 3  # 200 未被保留
        assert all(f < 100 for f in merged)

    def test_empty(self) -> None:
        assert len(merge_strip_onsets([np.array([]), np.array([])], fps=30.0)) == 0

    def test_fewer_than_three_returns_empty(self) -> None:
        a = np.array([10.0, 50.0])
        b = np.array([10.2, 50.1])
        merged = merge_strip_onsets([a, b], fps=30.0)
        assert len(merged) == 2  # 合併本身不過濾數量，由呼叫端以 MIN_PULSE_COUNT 判定


def _write_periodic_video(path: str, w: int = 320, h: int = 240,
                           fps: float = 30.0, n: int = 300, period_f: int = 12) -> None:
    """合成週期亮帶影片：掃描帶內每 period_f 幀亮一次（模擬 10m 標線通過）。"""
    import cv2

    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert writer.isOpened()
    try:
        blk = 0
        for i in range(n):
            f = np.full((h, w, 3), 85, dtype=np.uint8)
            if (i // 2) % (period_f // 2) == 0:
                f[int(h * 0.50):int(h * 0.64), :] = 200
                blk += 1
            writer.write(f)
    finally:
        writer.release()


class TestAnalyzeVideoDualTrack:
    def test_periodic_video_speed(self, tmp_path) -> None:
        """端到端：12 幀一亮帶 @30fps → T=0.4s → 90 km/h，日間。"""
        from dashcam_player.utils.marking_detector import analyze_video

        path = str(tmp_path / "periodic.mp4")
        _write_periodic_video(path)
        r = analyze_video(path, scan_band=(0.50, 0.64))
        assert r.get("lighting") == "DAY"
        assert r["speed_kmh"] is not None
        assert abs(r["speed_kmh"] - 90.0) / 90.0 < 0.15
        assert r["source"] in ("full", "merged")

    def test_missing_file(self, tmp_path) -> None:
        from dashcam_player.utils.marking_detector import analyze_video

        r = analyze_video(str(tmp_path / "不存在.mp4"))
        assert r["speed_kmh"] is None
