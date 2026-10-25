"""SpeedWorker - 背景視覺測速掃描執行緒

負責離線/背景掃描影片產出 SpeedTrack
"""

from __future__ import annotations

import queue
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

if TYPE_CHECKING:
    pass

from dashcam_player.utils.cv_utils import open_video_capture
from dashcam_player.utils.hood_detector import (
    CONSERVATIVE_FALLBACK_Y,
    detect_hood_top,
)
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.pts_loader import load_pts_and_keyframes
from dashcam_player.utils.vision_speed import MARKING_CYCLE_LENGTH_M  # noqa: E402

log = get_logger()


class _CmdType(Enum):
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"


@dataclass
class _Command:
    type: _CmdType


class SpeedWorker(QThread):
    """背景視覺測速 Worker

    兩段式自適應取樣：
    Stage 1: 前 8 秒全幀率掃描估算 v_p95
    Stage 2: 自適應 FPS 主掃 + 護網重掃
    """

    result_ready = pyqtSignal(object)  # (SpeedTrack)
    progress = pyqtSignal(int, int)  # (current, total)
    error_occurred = pyqtSignal(str)  # error message
    stage_changed = pyqtSignal(str)  # "stage1" / "stage2" / "rescan"
    status_message = pyqtSignal(str)  # 狀態列訊息（ROI 污染棄用等通知經此轉發）
    speed_updated = pyqtSignal(float, float, float, str)  # (time_sec, speed_kmh, confidence, source)

    def __init__(
        self,
        parent: Any | None = None,
        hw_backends: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._path: str = ""
        self._hw_backends = hw_backends or []
        self._stop = False
        self._pause = False
        self._cmd_queue: queue.Queue[_Command] = queue.Queue()

        # 設定參數（台灣高速規範：一組黑白 10m，非可調）
        self._distance_per_cycle = MARKING_CYCLE_LENGTH_M
        self._pixels_per_meter = 50.0  # 像素到公尺轉換因子（校準值）
        self._suitability_threshold = "LIMITED"
        self._roi: tuple[float, float, float, float] | None = None  # (x, y, w, h) relative
        self._side: str = "both"  # "left" / "right" / "both"

        # 兩段式參數
        self._stage1_duration = 8.0  # 秒
        self._target_samples_per_cycle = 6
        self._safety_factor = 1.2
        self._min_fps = 6
        self._max_fps = 30

        # 護網重掃
        self._rescan_threshold = 2.5  # 週期 < 2.5 * sample_interval
        self._rescan_done = False

        # PTS/幀時間
        self._pts_list: list[float] = []
        self._keyframe_times: list[float] = []
        self._fps: float = 30.0
        self._total_frames: int = 0
        self._duration: float = 0.0

        # 狀態
        self._current_stage = "idle"
        self._current_frame_idx = 0

        # 結果
        self._vision_points: list = []  # VisionPoint 列表

        # 即時速度追蹤（用於播放中顯示）
        self._realtime_speed: float | None = None
        self._realtime_confidence: float = 0.0

        # 幀到位移追蹤
        self._prev_marking_y: float | None = None  # 上一幀標線 Y 位置 (ROI 內)
        self._displacements: list[float] = []       # 幀間位移 (像素)
        self._wiper_mask: np.ndarray | None = None  # 雨刷遮罩
        self._wiper_filter_enabled: bool = False    # 雨刷過濾開關（預設關閉）

    def open(
        self,
        path: str,
        distance_per_cycle: float = MARKING_CYCLE_LENGTH_M,
        roi: tuple[float, float, float, float] | None = None,
        side: str = "both",
        pixels_per_meter: float = 50.0,
    ) -> None:
        self._path = path
        self._distance_per_cycle = distance_per_cycle
        self._roi = roi  # (x, y, w, h) relative 0.0-1.0
        self._side = side  # "left" / "right" / "both"
        self._pixels_per_meter = pixels_per_meter
        self._stop = False
        self._pause = False
        self._rescan_done = False
        self._vision_points = []
        self._cmd_queue = queue.Queue()  # 重置佇列
        self.start()

    def close(self) -> None:
        self._stop = True
        self._cmd_queue.put_nowait(_Command(_CmdType.STOP))

    def pause(self) -> None:
        self._pause = True
        self._cmd_queue.put_nowait(_Command(_CmdType.PAUSE))

    def resume(self) -> None:
        self._pause = False
        self._cmd_queue.put_nowait(_Command(_CmdType.RESUME))

    def _drain_commands(self) -> None:
        while True:
            try:
                self._cmd_queue.get_nowait()
            except queue.Empty:
                break

    def set_sample_budget(self, budget: float) -> None:
        """供 SpeedScheduler 調節 CPU 預算時呼叫，避免直接改寫私有屬性"""
        try:
            self._target_samples_per_cycle = max(3, int(6 * max(0.05, min(1.0, budget))))
        except Exception:
            pass

    def run(self) -> None:
        # 載入 PTS + keyframe（有快取，不會重複 ffprobe）
        log.info(f"SpeedWorker: 開始載入 PTS - {self._path}")
        pts_result = self._load_pts()

        if pts_result.pts_list:
            self._pts_list = pts_result.pts_list
            self._keyframe_times = pts_result.keyframe_times
            self._total_frames = len(pts_result.pts_list)
            # VFR 精確 fps：用 duration 中位數間隔而非首幀間隔
            if len(pts_result.pts_list) >= 2:
                # 中位數間隔更穩健，末幀補平均間隔估 duration
                intervals = [pts_result.pts_list[i+1] - pts_result.pts_list[i] for i in range(len(pts_result.pts_list)-1)]
                intervals = [iv for iv in intervals if 0 < iv < 1.0]
                if intervals:
                    median_interval = float(sorted(intervals)[len(intervals)//2])
                    self._fps = 1.0 / median_interval if median_interval > 0 else 30.0
                    self._duration = pts_result.pts_list[-1] + median_interval
                else:
                    self._fps = 30.0
                    self._duration = pts_result.pts_list[-1]
            else:
                self._fps = 30.0
                self._duration = pts_result.pts_list[-1] if pts_result.pts_list else 0.0
        else:
            # PTS 失敗 → 用 ffprobe 取 fps + nb_frames 做 fallback
            log.warning("PTS 載入失敗，使用 fps fallback")
            fps_info = self._probe_fps_info()
            if fps_info is None:
                self.error_occurred.emit("無法載入影片時間資訊")
                return
            self._fps, self._total_frames, self._duration = fps_info
            self._pts_list = []  # 空：使用 frame_idx / fps 計算時間
            self._keyframe_times = []

        log.info(
            f"SpeedWorker: 時間載入完成 - {self._total_frames} 幀, {self._duration:.1f}s, {self._fps:.1f}fps"
        )

        # 開啟影片
        cap = open_video_capture(self._path, self._hw_backends)
        if not cap.isOpened():
            self.error_occurred.emit(f"無法開啟影片: {self._path}")
            return

        try:
            # 存檔 ROI 跨機器污染驗證（背景執行，不卡 UI）：QSettings key 僅綁
            # 解析度+格式，同解析度不同機器/角度會撞 key。若存檔框底邊深入本次
            # 影片的車體（底邊 > hood_top + 0.02），視為他機舊框，直接棄用走預設。
            # 無 hood 簽名（後鏡頭等）時不驗證，保留存檔框。
            self._validate_roi_against_hood()

            # Stage 1: 粗探
            self._current_stage = "stage1"
            self.stage_changed.emit("stage1")
            self._run_stage1(cap)

            if self._stop:
                return

            # Stage 2: 主掃
            self._current_stage = "stage2"
            self.stage_changed.emit("stage2")
            self._run_stage2(cap)

            if self._stop:
                return

            # 完成
            self._finish()

        finally:
            if cap:
                cap.release()

    def _load_pts(self):
        """載入 PTS + keyframe (使用 pts_loader，有快取)"""
        return load_pts_and_keyframes(self._path, timeout=15.0)

    def _probe_fps_info(self) -> tuple[float, int, float] | None:
        """Fallback: 用 ffprobe 取得真實 fps + 實際幀數 + duration。

        以往直接解析 r_frame_rate/nb_frames —— 對檔頭損壞的 AVI 這兩欄都不可信
        （樣本檔宣稱 31fps/26477 幀，實際 5.05fps/223 幀），改用 -count_frames
        的 nb_read_frames + format.duration 反推，才能得到正確的測速時間基準。
        """
        from dashcam_player.utils.pts_loader import probe_video_metadata

        meta = probe_video_metadata(self._path, timeout=15.0)
        if meta is None:
            return None
        count, fps, duration = meta.estimate()
        if count <= 0 or fps <= 0 or duration <= 0:
            return None
        return fps, count, duration

    def _get_time(self, frame_idx: int) -> float:
        """取得幀對應的時間 (秒)，優先用 PTS，fallback 用 idx/fps"""
        if self._pts_list and frame_idx < len(self._pts_list):
            return self._pts_list[frame_idx]
        return frame_idx / self._fps if self._fps > 0 else 0.0

    def _validate_roi_against_hood(self) -> None:
        """存檔 ROI 跨機器污染驗證。污染時將 self._roi 清除（走預設路徑）
        並經 status_message 通知 UI；無存檔 ROI 或無 hood 簽名時不動作。"""
        if not self._roi:
            return
        try:
            hood_res = detect_hood_top(self._path)
        except Exception as e:
            log.warning(f"SpeedWorker: hood 驗證失敗（{e}），保留存檔 ROI")
            return
        if hood_res is None:
            return
        roi_bottom = self._roi[1] + self._roi[3]
        if roi_bottom > hood_res.hood_top_y + 0.02:
            log.warning(
                f"SpeedWorker: 存檔 ROI 底邊 {roi_bottom:.3f} 深入車體 "
                f"(hood_top={hood_res.hood_top_y:.3f})，疑似跨機器舊框，棄用走預設"
            )
            self._roi = None
            try:
                self.status_message.emit("存檔偵測區域與本車角度不符，已改用預設區域")
            except Exception:
                pass

    def _extract_roi(self, gray: np.ndarray) -> np.ndarray:
        """從灰階影像提取 ROI 區域（使用設定的 ROI 或預設範圍）"""
        h, w = gray.shape
        if self._roi:
            rx, ry, rw, rh = self._roi
            # 儲存的 ROI 寬度 1.0（日誌 567 boxes → union 滿寬）表示誤判，退回預設路面 ROI
            if rw >= 0.98 and rh >= 0.40:
                log.warning(f"Speed ROI 滿寬異常 {self._roi}，退回預設 0.45-{CONSERVATIVE_FALLBACK_Y}")
                return gray[int(h * 0.45) : int(h * CONSERVATIVE_FALLBACK_Y), :]
            x1 = int(rx * w)
            y1 = int(ry * h)
            x2 = int((rx + rw) * w)
            y2 = int((ry + rh) * h)
            return gray[max(0, y1) : min(h, y2), max(0, x1) : min(w, x2)]
        # 預設：路面中段 45%-64%（避開上方天空、下方車體）。
        # Phase A/B 實測：前鏡頭車體上緣 hood_top_y ≈ 0.65，舊值 0.75 含 0.65-0.75
        # 車體反光帶（誤判來源）；CONSERVATIVE_FALLBACK_Y=0.64 為人工框底邊最小值再扣一點。
        # 舊值 0.55-0.92 含引擎蓋（靜態、幀間差僅 ~1.4）污染位移統計；0.40-0.85 又含過多天空。
        return gray[int(h * 0.45) : int(h * CONSERVATIVE_FALLBACK_Y), :]

    def _extract_temporal_roi(self, gray: np.ndarray) -> np.ndarray:
        """時間域 100m 法專用：薄參考帶 0.55-0.62（實測有效，見 verify 回歸）"""
        h, w = gray.shape
        y1 = int(h * 0.55)
        y2 = int(h * 0.62)
        y2 = max(y1 + 10, y2)
        return gray[y1:y2, :]

    def _run_stage1(self, cap: cv2.VideoCapture) -> None:
        """Stage 1: 建立雨刷遮罩 + 幀到位移追蹤估算 v_p95"""
        log.info("SpeedWorker: Stage 1 開始 - 幀到位移追蹤")
        self.progress.emit(0, 100)

        from dashcam_player.utils.vision_speed import build_wiper_mask, detect_marking_displacement

        # === 雨刷遮罩（可選，預設關閉） ===
        if self._wiper_filter_enabled:
            wiper_frames = []
            for i in range(min(5, self._total_frames)):
                cap.set(cv2.CAP_PROP_POS_FRAMES, i * max(1, self._total_frames // 10))
                ret, frame = cap.read()
                if ret and frame is not None:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    roi = self._extract_roi(gray)
                    if roi is not None and roi.size > 0:
                        wiper_frames.append(roi)

            if wiper_frames:
                combined = wiper_frames[0]
                for f in wiper_frames[1:]:
                    if f.shape == combined.shape:
                        combined = cv2.max(combined, f)
                self._wiper_mask = build_wiper_mask(combined, n_frames=len(wiper_frames))
                masked_px = int(np.count_nonzero(self._wiper_mask == 0))
                log.info(f"雨刷遮罩建立: {masked_px} pixels masked")
            else:
                self._wiper_mask = None
        else:
            self._wiper_mask = None
            log.info("雨刷過濾已關閉")

        # === 幀到位移追蹤 ===
        stage1_frames = int(self._stage1_duration * self._fps)
        stage1_frames = min(stage1_frames, self._total_frames)

        prev_roi = None
        self._displacements = []
        speeds = []

        # 效能修正：Stage1 是逐幀連續讀取（idx 每次僅 +1），
        # 不需要也不應該呼叫 cap.set(POS_FRAMES) —— 對 H.264 這類有 GOP
        # 結構的編碼格式，每次 seek 都可能觸發「回溯關鍵幀 + 往前重新解碼」，
        # 實測比連續 read() 慢 10 倍以上，會導致背景掃描遠跟不上播放進度。
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

        for idx in range(stage1_frames):
            if self._stop:
                return
            while self._pause:
                self.msleep(100)
                if self._stop:
                    return

            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            curr_roi = self._extract_roi(gray)

            if (curr_roi is not None and curr_roi.size > 0
                    and prev_roi is not None and prev_roi.size > 0):
                disp = detect_marking_displacement(
                    prev_roi, curr_roi,
                    wiper_mask=self._wiper_mask,
                    apply_morph=self._wiper_filter_enabled,
                )
                if disp is not None and abs(disp) > 0.5:
                    self._displacements.append(disp)
                    speed_mps = disp * self._fps / self._pixels_per_meter
                    speed_kmh = speed_mps * 3.6
                    if idx < 150:  # 前5秒詳細log
                        log.debug(f"Stage1 disp={disp:.2f}px, speed={speed_kmh:.1f}km/h, roi={curr_roi.shape}")
                    if 5.0 <= speed_kmh <= 200.0:
                        speeds.append(speed_kmh)

            prev_roi = curr_roi

            if idx % max(1, stage1_frames // 20) == 0:
                self.progress.emit(int(idx / stage1_frames * 100), 100)

        self.progress.emit(100, 100)

        if speeds:
            speeds.sort()
            v_p95 = speeds[int(len(speeds) * 0.95)]
            target_fps = min(
                max(int(v_p95 / self._distance_per_cycle * self._target_samples_per_cycle * 1.2), 6),
                30,
            )
            log.info(f"Stage 1 完成: v_p95={v_p95:.1f} km/h, 建議 FPS={target_fps}")
        else:
            target_fps = 15
            log.info("Stage 1 完成: 無有效位移，使用預設 FPS=15")

        self._stage2_fps = target_fps

    def _run_stage2(self, cap: cv2.VideoCapture) -> None:
        """Stage 2: 自適應 FPS 主掃 + 時間域 100m 計時法（長窗平均抗雜，諧波投票）"""
        log.info(f"SpeedWorker: Stage 2 開始 - 主掃 @ {self._stage2_fps}fps")

        from dashcam_player.utils.vision_speed import (
            _find_peaks_robust,
            build_temporal_brightness_signal,
            detect_marking_displacement,
            estimate_speed_from_displacements,
            estimate_speed_from_temporal_peaks,
        )

        frame_interval = max(1, int(self._fps / self._stage2_fps))
        self._vision_points = []
        prev_roi = None
        window_displacements: list[float] = []
        window_pts: list[float] = []
        # 時間域 100m 計時法：收集薄帶 ROI 以建立亮度時間訊號
        temporal_rois: list[np.ndarray] = []
        temporal_times: list[float] = []

        # 效能修正：不用 cap.set(POS_FRAMES) 逐次 seek（對 H.264 實測慢 3.5~8 倍），
        # 改用 cap.grab()（只解碼跳過、不做色彩轉換）快轉到目標幀，
        # 只在真正需要的取樣幀呼叫 cap.read() 完整解碼。
        # cursor 追蹤目前 cap 內部實際指向的幀號，只重置一次（Stage1 結束後位置不確定，
        # 保險起見統一從頭 seek 一次）。
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        cursor = 0

        idx = 0
        while idx < self._total_frames:
            if self._stop:
                return
            while self._pause:
                self.msleep(100)
                if self._stop:
                    return

            # 跳過中間不需要的幀（只 grab，不解碼成影像，比 set()+read() 快很多）
            skip_failed = False
            while cursor < idx:
                if not cap.grab():
                    skip_failed = True
                    break
                cursor += 1
            if skip_failed:
                break

            ret, frame = cap.read()
            cursor += 1
            if not ret or frame is None:
                idx += frame_interval
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            curr_roi = self._extract_roi(gray)
            t = self._get_time(idx)
            # 收集時間域訊號用薄帶
            try:
                temporal_roi = self._extract_temporal_roi(gray)
                temporal_rois.append(temporal_roi.copy())
                temporal_times.append(t)
            except Exception:
                pass

            # 直接使用時間域 100m 計時法（長窗平均抗雜，中位數間隔直推）
            # 每幀累積 temporal_roi，定期從全序列偵測峰值並計算速度
            pass
            if (curr_roi is not None and curr_roi.size > 0
                    and prev_roi is not None and prev_roi.size > 0):
                disp = detect_marking_displacement(
                    prev_roi, curr_roi,
                    wiper_mask=self._wiper_mask,
                    apply_morph=self._wiper_filter_enabled,
                )
                if disp is not None and abs(disp) > 0.5:
                    window_displacements.append(disp)
                    window_pts.append(t)
                    if len(window_displacements) <= 5:
                        log.debug(f"Stage2 disp={disp:.2f}px, roi={curr_roi.shape} (fallback displacement)")

            prev_roi = curr_roi

            # 每 ~2 秒輸出一次速度
            if len(window_pts) >= 2 and (t - window_pts[0]) >= 2.0:
                v_points = estimate_speed_from_displacements(
                    window_displacements, self._fps, self._pixels_per_meter, window_pts
                )
                for vp in v_points:
                    self._vision_points.append(vp)
                    if vp.raw_speed is not None:
                        self._realtime_speed = vp.raw_speed
                        self._realtime_confidence = vp.confidence
                        self.speed_updated.emit(t, vp.raw_speed * 3.6, vp.confidence, "vision")

                window_displacements = []
                window_pts = []

            idx += frame_interval

            if idx % max(1, self._total_frames // 100) == 0:
                self.progress.emit(int(idx / self._total_frames * 100), 100)

        # 輸出最後窗口
        if window_displacements:
            v_points = estimate_speed_from_displacements(
                window_displacements, self._fps, self._pixels_per_meter, window_pts
            )
            for vp in v_points:
                self._vision_points.append(vp)

        # ── 時間域 100m 計時法（固定起點數 10 週期=100m，長窗平均抗雜，諧波+6:4 duty 投票） ──
        try:
            if len(temporal_rois) >= int(self._fps * 5):
                signal = build_temporal_brightness_signal(temporal_rois)
                if signal is not None and len(signal) >= 30:
                    # 峰計數：先找全序列峰
                    min_dist = max(5, int(self._fps * 0.2))
                    peaks = _find_peaks_robust(signal, min_distance=min_dist)
                    log.info(f"時間域訊號: len={len(signal)} peaks={len(peaks)}")
                    if len(peaks) >= 12:
                        # 滑窗每 5 個週期步進，產生多個 100m 速度點，對應使用者手動法
                        step = 5  # 間隔 5 週期 ≈50m 步進
                        for start_idx in range(0, len(peaks) - 10, step):
                            window_peaks = peaks[start_idx : start_idx + 11]  # 11 峰 =10 週期
                            # 用中位週期法估該窗速度，含諧波與 duty 加權
                            vps = estimate_speed_from_temporal_peaks(
                                window_peaks, self._fps, self._distance_per_cycle, 10, signal
                            )
                            for vp in vps:
                                # 校正時間：窗中點時間
                                mid_t = float((window_peaks[5] / self._fps) if len(window_peaks) > 5 else temporal_times[start_idx])
                                # 用 PTS 時間校正
                                if temporal_times and start_idx < len(temporal_times):
                                    # 嘗試用 temporal_times 對齊
                                    try:
                                        mid_idx = int(window_peaks[5]) if len(window_peaks) > 5 else int(window_peaks[len(window_peaks)//2])
                                        if 0 <= mid_idx < len(temporal_times):
                                            mid_t = float(temporal_times[mid_idx])
                                    except Exception:
                                        pass
                                vp.time = mid_t
                                self._vision_points.append(vp)
                                if vp.raw_speed is not None:
                                    self.speed_updated.emit(mid_t, vp.raw_speed * 3.6, vp.confidence, "vision")
                        log.info(f"時間域 100m 法產生 {len([p for p in self._vision_points if p.period_hypothesis.startswith('x')])} 個速度點")
                    else:
                        log.info(f"時間域峰數不足 {len(peaks)}，跳過 100m 計時")
        except Exception as e:
            log.warning(f"時間域 100m 計時法失敗: {e}", exc_info=True)

        self.progress.emit(100, 100)
        log.info(f"Stage 2 完成: {len(self._vision_points)} 個速度點")

    def _finish(self) -> None:
        """完成掃描，產出 SpeedTrack"""
        from dashcam_player.models.speed import SpeedPoint, SpeedTrack

        # 建立 SpeedTrack
        points = []
        for vp in self._vision_points:
            if vp:
                points.append(
                    SpeedPoint(
                        time=vp.time,
                        raw_vision=vp.raw_speed,
                        corrected=None,
                        fused=vp.raw_speed,
                        confidence=vp.confidence,
                        source="vision",
                        period_hypothesis=vp.period_hypothesis,
                        strip_votes=vp.strip_votes,
                        reject_reason=vp.reject_reason,
                    )
                )

        if not points:
            track = SpeedTrack(points=[], status="NO_VISION_SPEED")
        else:
            track = SpeedTrack(
                points=points,
                status="OK" if len(points) > 10 else "PARTIAL",
            )

        self.result_ready.emit(track)
        log.info(f"SpeedWorker 完成: {len(points)} points, status={track.status}")
