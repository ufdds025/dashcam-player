from __future__ import annotations

import bisect
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum

import cv2
from PyQt5.QtCore import QThread, pyqtSignal

from dashcam_player.models.video_info import VideoInfo
from dashcam_player.utils.cv_utils import decode_fourcc, open_video_capture
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.pts_loader import (
    VideoMetadata,
    load_pts_and_keyframes_sync,
    probe_video_metadata,
)

log = get_logger()

# 被丟棄但仍運行中的 VideoWorker 保留區（Controller 於極端逾時場景會新建 worker 取代舊的，
# 舊 worker 仍在背景解碼時需持強參考，避免 QThread 於運行中被 GC 而噴
# "QThread: Destroyed while thread is still running"）。finished 後自動移出。
_worker_graveyard: set = set()


def _retire_worker(worker) -> None:
    _worker_graveyard.discard(worker)


def _resolve_playback_meta(
    pts_list: list[float],
    pts_quality: str,
    probe: VideoMetadata | None,
    opencv_total: int,
    header_fps: float,
) -> tuple[int, float, float, bool]:
    """決定 (frame_count, fps, duration, 是否採用真實 PTS)。

    動機：部分行車記錄器 AVI 檔頭 nb_frames/avg_frame_rate 被誤寫
    （樣本檔宣稱 26477 幀/600fps，實際只解得出 223 幀/44.13s ≈ 5.05fps）。
    因此**不可**直接信任 cap.get(CAP_PROP_FRAME_COUNT / CAP_PROP_FPS)，
    須以真實資料交叉驗證。優先序：

    1. 真實逐幀 PTS（EXACT/DEGRADED，且長度與 nb_read_frames 相符）
       → frame_count = len(PTS)、fps = 時間跨度反推的平均 fps、保留 VFR 時間戳
    2. ffprobe -count_frames 的 nb_read_frames + format.duration
       → nb_read_frames 為 demuxer 實際可解封包數，唯一可靠的真實幀數
    3. 容器檔頭（ffprobe 不存在時；由呼叫端另行二分驗證）

    Returns:
        (frame_count, fps, duration, use_real_pts)
    """
    meta_count = probe.actual_frames if probe else 0
    pts_matches = (
        meta_count <= 0
        or abs(len(pts_list) - meta_count) <= max(2, meta_count * 0.20)
    )
    if pts_list and pts_quality in ("EXACT", "DEGRADED") and pts_matches:
        total = len(pts_list)
        if total >= 2 and pts_list[-1] > pts_list[0]:
            avg_interval = (pts_list[-1] - pts_list[0]) / (total - 1)
            fps = 1.0 / avg_interval if 0 < avg_interval < 1.0 else header_fps
            duration = pts_list[-1] + avg_interval
        else:
            fps = header_fps
            duration = pts_list[-1] if pts_list else 0.0
        return total, fps, duration, True

    if probe is not None:
        count, fps, duration = probe.estimate()
        if count > 0:
            return count, fps, duration, False

    fps = header_fps if header_fps > 0 else 30.0
    total = opencv_total
    duration = total / fps if fps > 0 else 0.0
    return total, fps, duration, False


def _compute_frame_interval(
    pts_list: list[float] | None,
    current_idx: int,
    fps: float,
    target_fps: float,
) -> float:
    """計算下一幀應等待的顯示間隔（秒）。

    間隔來源一律由偵測值推導，無任何硬編碼幀率假設：
    有 PTS 表時取相鄰 PTS 差（VFR 準確）再除以速度倍率
    （``target_fps / fps``，``target_fps`` 已含使用者倍速）；
    無表或間隔異常（``<=0`` 或 ``>1s``）時回退 ``1/target_fps``。
    """
    speed_factor = target_fps / fps if fps > 0 else 1.0
    if pts_list and current_idx + 1 < len(pts_list):
        current_pts = pts_list[current_idx] if current_idx < len(pts_list) else 0
        interval = (pts_list[current_idx + 1] - current_pts) / speed_factor
        if 0 < interval <= 1.0:
            return interval
    return 1.0 / target_fps if target_fps > 0 else 0.033


class _CmdType(Enum):
    PLAY = "play"
    PAUSE = "pause"
    SEEK = "seek"
    STEP_FWD = "step_fwd"
    STEP_BWD = "step_bwd"
    STOP = "stop"
    RELEASE_CAP = "release_cap"


@dataclass
class _Command:
    """統一指令，由主執行緒 push、run() loop 單一消費。"""

    type: _CmdType
    frame: int = -1  # SEEK 用


class VideoWorker(QThread):
    """背景解碼引擎：播放、seek、單步前進/後退

    Thread safety:
    - 所有播放/seek/step 指令經由 self._cmd_queue 統一投遞，run() 單一消費，
      消除舊版 _play / _seek_frame 雙信號競爭。
    - _current_idx: 僅 run() loop 寫入（push 後自行更新），讀取端（controller）經信號取得。
    - _stop: 由主執行緒 close() 寫入，run() 讀取以結束 loop。
    - _cmd_queue 為 queue.Queue（thread-safe），主執行緒 push、run() loop 消費。
    """

    frame_ready = pyqtSignal(object, int)  # (frame_bgr, frame_index)
    position_changed = pyqtSignal(float, int)  # (當前秒數, 幀號)
    load_failed = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)  # (title, message) — seek/播放等非載入失敗的錯誤
    play_finished = pyqtSignal()  # 播放到尾端（EOF）
    video_info_ready = pyqtSignal(object)  # VideoInfo instance

    def __init__(
        self,
        parent: object | None = None,
        hw_backends: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.cap: cv2.VideoCapture | None = None
        self._path: str = ""
        self._stop: bool = False
        # 世代計數器：每次 open() 遞增。舊 run() loop 與 keyframe loader 以此比對，
        # 判斷結果/執行是否屬於當前影片，杜絕舊執行緒污染新影片（Phase 1.2/2.1）。
        self._epoch: int = 0
        self._open_epoch: int = 0
        self._pts_lock: threading.Lock = threading.Lock()
        self._fps: float = 30.0
        self._target_fps: float = 30.0
        self._total_frames: int = 0
        self._current_idx: int = 0
        self._hw_backends = hw_backends or []
        self._cmd_queue: queue.Queue[_Command] = queue.Queue()
        self._keyframe_times: list[float] | None = None  # 關鍵影格時間（秒），用於精確 seek
        self._keyframe_loaded: bool = False  # A2: 背景載入完成 flag
        self._pts_list: list[float] = []
        self._pts_quality: str = "UNSAFE"
        # 倍速切換時請求重置播放 pacing 錨點（主執行緒寫入、run loop 消費）。
        self._pace_reset_requested: bool = False

    def open(self, path: str) -> None:
        self._path = path
        self._stop = False
        self._current_idx = 0
        self._keyframe_loaded = False
        self._keyframe_times = None
        with self._pts_lock:
            self._pts_list = []
            self._pts_quality = "UNSAFE"
        # 遞增世代：舊 run() loop 若仍在背景收尾，會發現 epoch 不符而自行退出
        self._epoch += 1
        self._open_epoch = self._epoch
        # 背景預載關鍵影格時間表 + 全幀 PTS，避免雙重 ffprobe
        self._start_keyframe_loader()
        # 清空任何舊指令，開啟後停在首幀（idle）
        self._drain_commands()
        self._cmd_queue.put(_Command(_CmdType.STOP))
        self.start()

    def _start_keyframe_loader(self) -> None:
        """背景執行緒預載關鍵影格時間表 + 全幀 PTS (P1c)。

        合併原有 keyframe loader 與新的 pts_loader，
        避免雙重 ffprobe 呼叫。
        seek 不阻塞：表格就緒前 `_do_seek` 直接交給 cv2 內部 seek。
        影片已切換時丟棄結果（以世代計數器比對，取代舊版僅比對 path）。
        """
        path = self._path
        epoch = self._open_epoch
        if not path:
            return

        def _load() -> None:
            pts_list, keyframe_times, quality = load_pts_and_keyframes_sync(path, timeout=30.0)
            if self._epoch != epoch or self._path != path:
                return  # 已切換影片或後續重新開啟，丟棄過期結果
            with self._pts_lock:
                self._pts_list = pts_list
                self._keyframe_times = keyframe_times
                self._pts_quality = quality
                self._keyframe_loaded = True
            if keyframe_times:
                log.info(f"關鍵影格索引載入完成: {len(keyframe_times)} 個")
            if pts_list:
                log.info(f"PTS 陣列載入完成: {len(pts_list)} 幀, quality={quality}")

        threading.Thread(target=_load, name="pts-keyframe-loader", daemon=True).start()

    def close(self) -> None:
        self._stop = True
        # push STOP 喚醒可能阻塞在 msleep 的 loop，確保盡快退出
        # （queue 為 unbounded，put_nowait 不會 raise Full）
        self._cmd_queue.put_nowait(_Command(_CmdType.STOP))

    def _drain_commands(self) -> None:
        while True:
            try:
                self._cmd_queue.get_nowait()
            except queue.Empty:
                break

    def release_cap(self) -> None:
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def set_target_fps(self, fps: float) -> None:
        self._target_fps = fps
        # 倍速/換片重套時請 run loop 重置 deadline 錨點，避免舊錨點造成叢發或停頓
        self._pace_reset_requested = True

    def seek(self, frame_idx: int) -> None:
        if self._total_frames <= 0:
            return
        idx = max(0, min(frame_idx, self._total_frames - 1))
        self._cmd_queue.put(_Command(_CmdType.SEEK, idx))

    def set_play(self, on: bool) -> None:
        self._cmd_queue.put(_Command(_CmdType.PLAY if on else _CmdType.PAUSE))

    def step_forward(self) -> None:
        self._cmd_queue.put(_Command(_CmdType.STEP_FWD))

    def step_backward(self) -> None:
        self._cmd_queue.put(_Command(_CmdType.STEP_BWD))

    def _nearest_keyframe_idx(self, target: int) -> int:
        """回傳 <= target 的最近關鍵影格幂號（A2: keyframe-aware seek）。

        有 keyframe 時間表時：找 <= target 時間的最大 keyframe，從該處向前解碼，
        避免 cv2 在關鍵影格稀疏影片上向後回溯導致長片卡頓。
        無 keyframe 表時回退為 target 本身（沿用 cv2 內部行為）。

        優先使用 PTS 陣列計算目標秒數（VFR 準確），回退用 fps 估算。
        執行緒安全：複製 pts/keyframe 引用後使用區域變數，避免與 loader 競態。
        """
        with self._pts_lock:
            pts_list = self._pts_list
            kf_times = self._keyframe_times
        if not kf_times or self._fps <= 0:
            return target

        if pts_list and target < len(pts_list):
            target_sec = pts_list[target]
        else:
            target_sec = target / self._fps

        pos = bisect.bisect_right(kf_times, target_sec) - 1
        if pos < 0:
            return 0
        kf_sec = kf_times[pos]

        if pts_list:
            idx = bisect.bisect_left(pts_list, kf_sec)
            return max(0, min(self._total_frames - 1, idx))
        else:
            return max(0, min(self._total_frames - 1, int(round(kf_sec * self._fps))))

    def _do_seek(self, target: int) -> None:
        """執行跳轉並 emit 結果（run loop 內呼叫）。A2: 先跳最近 keyframe 再向前解碼。"""
        if not self.cap:
            return
        # 檢查是否有更新的 seek 指令排隊，避免前向解碼期間 UI 卡頓
        def _has_pending_seek() -> bool:
            # 使用 queue.mutex 保護，避免與主執行緒 put() 競態
            with self._cmd_queue.mutex:
                for cmd in self._cmd_queue.queue:
                    if cmd.type in (_CmdType.SEEK, _CmdType.STEP_FWD, _CmdType.STEP_BWD, _CmdType.STOP):
                        return True
            return False

        # A2: keyframe 表由背景執行緒預載；尚未就緒時直接交給 cv2 內部 seek（不阻塞）
        with self._pts_lock:
            keyframe_loaded = self._keyframe_loaded
        start = self._nearest_keyframe_idx(target) if keyframe_loaded else target
        if not self.cap.set(cv2.CAP_PROP_POS_FRAMES, start):
            log.warning(f"seek 到 keyframe {start} 失敗，從頭循序解碼")
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            start = 0
        # 從 keyframe 向前解碼到 target（每 20 幀檢查是否被中斷）
        idx = start
        ret = True
        frame = None
        while idx < target:
            if self._stop or _has_pending_seek():
                log.info(f"seek 至 {target} 被中斷（已解到 {idx}）")
                return
            ret, frame = self.cap.read()
            if not ret or frame is None:
                break
            idx += 1
            if idx % 20 == 0 and _has_pending_seek():
                log.info(f"seek 至 {target} 被新指令中斷")
                return
        if idx < target:
            msg = f"無法跳轉至幀 {target}（僅解到 {idx}）"
            log.warning(msg)
            self.error_occurred.emit("跳轉失敗", msg)
            return
        if self._stop or _has_pending_seek():
            return
        ret, frame = self.cap.read()
        if ret:
            self._current_idx = target
            self.frame_ready.emit(frame, target)
            # 使用 PTS 精確時間（若可用）
            with self._pts_lock:
                pts_list = self._pts_list
            if pts_list and target < len(pts_list):
                pos = pts_list[target]
            else:
                pos = target / self._fps if self._fps > 0 else 0.0
            self.position_changed.emit(pos, target)
        else:
            msg = f"跳轉至幀 {target} 後讀取失敗"
            log.warning(msg)
            self.error_occurred.emit("跳轉失敗", msg)

    def release_cap_async(self) -> None:
        """A9: 釋放解碼器（批次截圖時避免雙重 VideoCapture 佔用資源）。

        非同步推入指令，run loop 消費後關閉 cap；播放時 set_play(True) 會自動重建。
        """
        self._cmd_queue.put(_Command(_CmdType.RELEASE_CAP))

    def _ensure_cap(self) -> bool:
        """確保 cap 已開啟（A9: 批次釋放後重新播放時重建）。"""
        if self.cap is not None:
            return True
        if not self._path:
            return False
        self.cap = open_video_capture(self._path, self._hw_backends)
        if not self.cap.isOpened():
            log.error(f"重建解碼器失敗: {self._path}")
            self.cap = None
            return False
        # 跳回目前幀位置
        if self._current_idx > 0:
            if not self.cap.set(cv2.CAP_PROP_POS_FRAMES, self._current_idx):
                log.warning(f"重建解碼器後 seek 至 {self._current_idx} 失敗，重置至首幀")
                self._current_idx = 0
        return True

    def run(self) -> None:
        self.cap = open_video_capture(self._path, self._hw_backends)
        try:
            # cap 建立後任何路徑（early return / 例外）都必須釋放解碼器；
            # 舊版三個 early return 發生在 try 之前，會洩漏 VideoCapture
            self._run_loop()
        finally:
            if self.cap:
                self.cap.release()
                self.cap = None

    def _find_last_decodable_frame(self, time_budget_ms: int = 800) -> int:
        """二分搜尋實際可解碼的最後一幀（檔頭 nb_frames 膨脹時使用）。

        以 CAP_PROP_POS_FRAMES 二分：set(mid) 成功且 read 回傳幀代表該幀存在。
        回傳最後可解碼幀索引（-1 表示無任何可解碼幀）。
        time_budget_ms 限制總耗時，避免在主解碼執行緒阻塞過久（破圖診斷檔 26477 幀需 1.5s）。
        """
        if not self.cap:
            return -1
        lo, hi = 0, max(0, self._total_frames - 1)
        start = time.monotonic()
        iters = 0
        while lo < hi and not self._stop:
            # 時間預算檢查 — 超時直接返回當前 lo，避免無限阻塞跑條卡死
            if (time.monotonic() - start) * 1000 > time_budget_ms:
                log.warning(f"二分搜尋逾時 ({time_budget_ms}ms)，提前返回 lo={lo}")
                break
            iters += 1
            if iters > 20:  # 26477 需 ~15 次，防異常膨脹
                break
            mid = (lo + hi + 1) // 2
            if not self.cap.set(cv2.CAP_PROP_POS_FRAMES, mid):
                hi = mid - 1
                continue
            ret_t, _ = self.cap.read()
            if ret_t:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _frame_msec(self, frame_idx: int) -> float:
        """讀取指定幀並回傳其時間戳（秒）。

        FFmpeg 後端對真實 dts 的檔案（即使檔頭 fps 損壞）會回傳正確的
        POS_MSEC —— 以此反推平均 fps，無 ffprobe 時的最後手段。
        """
        if not self.cap:
            return 0.0
        if not self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx):
            return 0.0
        ret_t, _ = self.cap.read()
        if not ret_t:
            return 0.0
        return float(self.cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0

    def _run_loop(self) -> None:
        if self.cap is None:  # run() 必已賦值；此守衛同時供型別窄化
            return
        if not self.cap.isOpened():
            log.error(f"無法開啟影片: {self._path}")
            self.load_failed.emit(f"無法開啟影片: {self._path}")
            return

        # 快照 PTS 表（若 loader 已完成則直接使用，否則用空表）
        with self._pts_lock:
            pts_list_snapshot = list(self._pts_list) if self._pts_list else []
            pts_quality_snapshot = self._pts_quality

        src_fps = self.cap.get(cv2.CAP_PROP_FPS)
        opencv_total = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self._fps = src_fps if src_fps > 0 else 30.0
        self._total_frames = opencv_total
        self._target_fps = self._fps

        # ------------------------------------------------------------------
        # 真實時間資訊決策（PTS > ffprobe metadata > 檔頭 + 解碼驗證）
        #
        # 動機：部分行車記錄器 AVI 檔頭 nb_frames/avg_frame_rate 被誤寫
        # （樣本檔宣稱 26477 幀/600fps，實際只解得出 223 幀/44.13s ≈ 5.05fps）。
        # OpenCV 照單全收 CAP_PROP_FRAME_COUNT/CAP_PROP_FPS，直接使用會造成
        # 播放快轉、seek 卡死、批次截圖/測速/GPS 全部算錯。
        # 因此以 ffprobe 的真實資料為準，檔頭僅作為最後手段。
        # ------------------------------------------------------------------
        probe = probe_video_metadata(self._path, timeout=10.0)
        if probe is not None:
            log.info(
                f"ffprobe metadata: header={probe.header_frames}幀/{probe.header_fps:.1f}fps "
                f"actual={probe.actual_frames}幀 duration={probe.duration:.3f}s ({probe.format_name})"
            )

        self._total_frames, self._fps, duration, use_real_pts = _resolve_playback_meta(
            pts_list_snapshot, pts_quality_snapshot, probe, opencv_total, src_fps if src_fps > 0 else 30.0
        )
        self._fps = self._fps if 1.0 <= self._fps <= 120.0 else 30.0
        self._target_fps = self._fps
        if not use_real_pts:
            pts_list_snapshot = []

        # 無真實 PTS 時：快速驗證檔頭 frame_count 是否真的可解碼。
        # 損壞 AVI 即使檔頭比例「巧合一致」，seek 到最後一幀仍會失敗
        # （樣本檔宣稱 26477 幀，set(26476) 成功但 read 回傳 False）。
        # 優先使用 ffprobe actual_frames（逐封包計數，真值 223），僅在無 probe 時二分。
        # 二分已加 800ms 預算，避免在主執行緒阻塞破圖卡頓。
        if not use_real_pts and self._total_frames > 2 and self._fps > 0:
            last_ok = False
            if self.cap.set(cv2.CAP_PROP_POS_FRAMES, self._total_frames - 1):
                ret_t, _ = self.cap.read()
                last_ok = bool(ret_t)
            if not last_ok:
                # 優先：probe 的逐封包真值（毫秒級，無解碼阻塞）
                if probe is not None and probe.actual_frames > 0:
                    probe_last = probe.actual_frames - 1
                    # 若 probe 真值與當前 _total_frames 差異顯著，直接採納
                    if abs(probe.actual_frames - self._total_frames) > max(2, self._total_frames * 0.20):
                        actual_last = probe_last
                        log.warning(
                            f"檔頭可疑（fps={src_fps:.0f} count={self._total_frames}）："
                            f"probe 真值 {probe.actual_frames} 幀，直接修正（免二分，無破圖）"
                        )
                    else:
                        actual_last = self._find_last_decodable_frame()
                else:
                    actual_last = self._find_last_decodable_frame()
                if actual_last > 0 and actual_last + 1 != self._total_frames:
                    old_total = self._total_frames
                    self._total_frames = actual_last + 1
                    # 若已有 probe duration，優先用 actual/duration 反推，避免 POS_MSEC 二次阻塞
                    if probe is not None and probe.duration > 0 and probe.actual_frames > 0:
                        self._fps = probe.actual_frames / probe.duration
                        self._target_fps = self._fps
                    else:
                        t0 = self._frame_msec(0)
                        t1 = self._frame_msec(actual_last)
                        if t1 > t0:
                            self._fps = actual_last / (t1 - t0)
                            self._target_fps = self._fps
                    duration = self._total_frames / self._fps if self._fps > 0 else duration
                    pts_list_snapshot = [i / self._fps for i in range(self._total_frames)] if self._fps > 0 else []
                    with self._pts_lock:
                        self._pts_list = pts_list_snapshot
                    log.warning(
                        f"檔頭可疑（fps={src_fps:.0f} count={old_total}）：二分驗證 "
                        f"實際 {self._total_frames} 幀 / {duration:.2f}s / {self._fps:.2f}fps"
                    )
                else:
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

        fourcc_val = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        codec_str = decode_fourcc(fourcc_val)
        width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        log.info(
            f"開啟成功  codec={codec_str}  {width}x{height}  "
            f"frames={self._total_frames}  fps={self._fps:.2f}  duration={duration:.2f}s"
        )

        if self._total_frames <= 0:
            log.error("幀數為 0")
            self.load_failed.emit("影片無效或無法讀取幀數")
            return

        # 解第一幀
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ret, frame = self.cap.read()
        if not ret:
            log.error("第一幀讀取失敗")
            self.load_failed.emit("無法解碼影片第一幀")
            return

        self._current_idx = 0
        self.frame_ready.emit(frame, 0)
        with self._pts_lock:
            pts0 = self._pts_list[0] if self._pts_list else 0.0
        self.position_changed.emit(pts0, 0)

        self.video_info_ready.emit(
            VideoInfo(
                path=self._path,
                fps=self._fps,
                frame_count=self._total_frames,
                fourcc=codec_str,
                width=width,
                height=height,
                duration=duration,
            )
        )

        playing = False
        # 下一幀應出現的 wall-clock 時刻（deadline 排程器錨點）。
        # 為 None 時表示需重置（初始/PLAY/SEEK/STEP/倍速切換/PTS 修正後），
        # 重置為當下時刻，避免舊錨點落後導致多幀零等待叢發播出。
        next_deadline: float | None = None
        # 記錄開啟當下的世代：run() 執行期間若 open() 被重複呼叫（epoch 已遞增），
        # 表示此 run 已被取代，應盡快退出不再處理舊指令，避免用舊影片狀態回應新 UI。
        run_epoch = self._open_epoch

        # PTS 載入修正 pending：背景 PTS loader 完成後首次進入主迴圈時，
        # 修正 _total_frames / _fps / _target_fps / VideoInfo（解決 race condition）
        pts_correction_pending = not pts_list_snapshot  # 初始 snapshot 為空 → pending

        while not self._stop and self._epoch == run_epoch:
            # PTS 載入修正：背景 loader 完成後，修正 _total_frames / _fps / _target_fps
            if pts_correction_pending:
                with self._pts_lock:
                    pts_now = list(self._pts_list) if self._pts_list else []
                    pts_quality_now = self._pts_quality
                if pts_now:
                    pts_correction_pending = False
                    probe_now = probe_video_metadata(self._path, timeout=10.0)
                    corrected_total, corrected_fps, corrected_duration, keep_real_pts = _resolve_playback_meta(
                        pts_now, pts_quality_now, probe_now, opencv_total, src_fps if src_fps > 0 else 30.0
                    )
                    if corrected_fps <= 0 or not 1.0 <= corrected_fps <= 120.0:
                        corrected_fps = self._fps
                    # 合成 PTS（linear fallback 依錯誤檔頭產生）長度不符 → 以真實資料重建
                    if not keep_real_pts:
                        new_pts = [i / corrected_fps for i in range(corrected_total)] if corrected_fps > 0 else []
                        with self._pts_lock:
                            self._pts_list = new_pts
                        pts_now = new_pts
                        log.info(
                            f"PTS loader 完成：合成 PTS 已以 probe 資料重建 "
                            f"({corrected_total} 幀, {corrected_fps:.2f}fps, {corrected_duration:.2f}s)"
                        )
                    else:
                        log.info(
                            f"PTS loader 完成：採用真實 PTS "
                            f"({corrected_total} 幀, 平均 {corrected_fps:.2f}fps, {corrected_duration:.2f}s)"
                        )

                    changed = False
                    if corrected_total != self._total_frames:
                        log.warning(
                            f"PTS loader 完成：修正 frame_count {self._total_frames} → {corrected_total}"
                        )
                        self._total_frames = corrected_total
                        changed = True
                    if abs(corrected_fps - self._fps) > 0.1 and corrected_fps > 0:
                        log.warning(
                            f"PTS loader 完成：修正 fps {self._fps:.2f} → {corrected_fps:.2f}"
                        )
                        self._fps = corrected_fps
                        self._target_fps = self._fps
                        changed = True
                    if changed:
                        # ⚠️ 不在此處重置 next_deadline——下方 _pace_reset_requested
                        # 機制會在下一輪迴圈統一處理，避免 PTS 修正與倍速切換雙重重置
                        # 造成時間錨點不連續（5fps 每秒一次的「額外停頓」根因）
                        # 重新 emit video_info 供 UI 更新
                        fourcc_val = int(self.cap.get(cv2.CAP_PROP_FOURCC)) if self.cap else 0
                        self.video_info_ready.emit(
                            VideoInfo(
                                path=self._path,
                                fps=self._fps,
                                frame_count=self._total_frames,
                                fourcc=decode_fourcc(fourcc_val),
                                width=int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)) if self.cap else 0,
                                height=int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) if self.cap else 0,
                                duration=corrected_duration,
                            )
                        )
                        log.info(
                            f"PTS 修正完成  frames={self._total_frames}  fps={self._fps:.2f}"
                        )

            try:
                cmd = self._cmd_queue.get_nowait()
            except queue.Empty:
                cmd = None

            if cmd is not None:
                if cmd.type == _CmdType.STOP:
                    playing = False
                elif cmd.type == _CmdType.PAUSE:
                    playing = False
                elif cmd.type == _CmdType.PLAY:
                    # A9: 批次釋放後重建解碼器
                    if not self._ensure_cap():
                        log.warning("play 但解碼器重建失敗，放棄播放")
                        playing = False
                    else:
                        playing = True
                        # 從暫停恢復或重建解碼器（含 seek 耗時）後重置錨點
                        next_deadline = None
                elif cmd.type == _CmdType.RELEASE_CAP:
                    # A9: 釋放解碼器資源（批次截圖期間）
                    self.release_cap()
                    playing = False
                elif cmd.type == _CmdType.SEEK:
                    # 維持播放狀態：拖動進度條後繼續播放（單步仍暫停）
                    self._do_seek(cmd.frame)
                    next_deadline = None
                    continue
                elif cmd.type == _CmdType.STEP_FWD:
                    playing = False
                    target = self._current_idx + 1
                    if 0 <= target < self._total_frames:
                        self._do_seek(target)
                    next_deadline = None
                    continue
                elif cmd.type == _CmdType.STEP_BWD:
                    playing = False
                    target = self._current_idx - 1
                    if 0 <= target < self._total_frames:
                        self._do_seek(target)
                    next_deadline = None
                    continue

            # 倍速切換（set_target_fps）請求重置錨點
            if self._pace_reset_requested:
                self._pace_reset_requested = False
                next_deadline = None

            # === 播放模式（deadline 排程器） ===
            if playing:
                # 使用 PTS 計算幀間隔（每次鎖讀最新表，loader 可能中途完成）
                with self._pts_lock:
                    pts_list_cur = self._pts_list
                frame_interval = _compute_frame_interval(
                    pts_list_cur, self._current_idx, self._fps, self._target_fps
                )

                now = time.monotonic()
                if next_deadline is None:
                    next_deadline = now
                # 已落後超過一個間隔（慢解碼/切換延遲）：丟棄欠帳、從現在重排，
                # 避免多幀零等待叢發一次播完；不跳幀，幀序列保持完整
                if now - next_deadline > frame_interval:
                    next_deadline = now
                if now < next_deadline:
                    remaining = next_deadline - now
                    # 睡到差 2ms 再 busy-wait（Windows QPC 精度 ~1ms，
                    # msleep 可能提前喚醒或oversleep，尾段忙等兜底）
                    if remaining > 0.002:
                        self.msleep(int((remaining - 0.002) * 1000))
                    continue

                ret, frame = self.cap.read()
                if not ret:
                    log.debug(f"播放結束 (idx={self._current_idx})")
                    playing = False
                    self.play_finished.emit()
                    continue

                self._current_idx += 1
                self.frame_ready.emit(frame, self._current_idx)
                # 復用迴圈頂部已鎖讀的 PTS（播放中 PTS 表極少變動，避免重複 lock）
                if pts_list_cur and self._current_idx < len(pts_list_cur):
                    pos = pts_list_cur[self._current_idx]
                else:
                    pos = self._current_idx / self._fps if self._fps > 0 else 0.0
                self.position_changed.emit(pos, self._current_idx)
                # 累加排程：解碼耗時不計入間隔，每幀均勻分佈（如 5fps → 每 0.2s 一幀）
                next_deadline += frame_interval
            else:
                self.msleep(20)
