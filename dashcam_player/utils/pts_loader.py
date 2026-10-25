"""PTS + Keyframe 探測器

合併 keyframe 與全幀 PTS 探測，供 VideoWorker 與 SpeedWorker 共用
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass

from dashcam_player.utils.ffmpeg import find_ffmpeg
from dashcam_player.utils.log import get_logger

log = get_logger()


@dataclass
class PTSResult:
    """PTS 探測結果"""
    pts_list: list[float]          # 每幀真實時間 (秒)
    keyframe_times: list[float]    # 關鍵幀時間 (秒)
    source: str                    # "pts" / "best_effort" / "linear"
    quality: str                   # "EXACT" / "DEGRADED" / "UNSAFE"


@dataclass
class VideoMetadata:
    """ffprobe 偵測的影片真實 metadata（相對於容器檔頭的可信資料）。

    動機：行車記錄器/監視器常把 AVI 檔頭寫壞（nb_frames 膨脹、avg_frame_rate
    被誤寫成時間軸刻度數），但封包層的真實時間戳（dts）與 format.duration
    仍是對的。單次 ffprobe -count_frames 可同時取得三組資料交叉驗證。
    """

    header_fps: float = 0.0   # avg_frame_rate（檔頭宣稱的幀率）
    header_frames: int = 0    # nb_frames（檔頭宣稱的幀數）
    actual_frames: int = 0    # nb_read_frames（demuxer 實際可解出的封包數，逐封包計數）
    duration: float = 0.0     # format.duration（真實總長，秒）
    format_name: str = ""

    @property
    def header_consistent(self) -> bool:
        """檔頭三個數字彼此對得上（nb_frames/fps ≈ duration 且 fps 合理）才可信任。

        注意：某些損壞 AVI 的 nb_frames/fps 會「巧合地」等於 duration
        （如 26477/600 = 44.13s = 真實 duration），因此僅檢查比例一致不夠，
        還須比對 nb_read_frames 與 nb_frames 的差異（見 estimate()）。
        """
        if self.header_fps <= 0 or self.header_frames <= 0 or self.duration <= 0:
            return False
        if not (1.0 <= self.header_fps <= 120.0):
            return False
        reported = self.header_frames / self.header_fps
        return abs(reported - self.duration) / self.duration <= 0.10

    def estimate(self) -> tuple[int, float, float]:
        """回傳 (frame_count, fps, duration) 的最可靠估計。

        決策規則：
        1. frame_count：nb_read_frames > 0 時採納（實際可解碼數）；否則採 nb_frames。
        2. duration：format.duration（容器真實總長）。
        3. fps：
           - header 一致「且」nb_read_frames 與 nb_frames 接近（差異 <=20%）
             → 採 header_fps（正常 CFR 片）
           - 否則 → actual_frames / duration（檔頭損壞，用真實資料反推，
             如 223 幀 / 44.13s ≈ 5.05fps）
        """
        count = self.actual_frames or self.header_frames
        same_count = (
            self.actual_frames <= 0
            or self.header_frames <= 0
            or abs(self.actual_frames - self.header_frames) <= max(2, self.header_frames * 0.20)
        )
        if self.header_consistent and same_count:
            fps = self.header_fps
        elif self.actual_frames > 0 and self.duration > 0:
            fps = self.actual_frames / self.duration
        elif self.header_frames > 0 and self.duration > 0:
            fps = self.header_frames / self.duration
        else:
            fps = self.header_fps
        return count, fps, self.duration


# ---------------------------------------------------------------------------
# Thread-safe PTS result cache to avoid duplicate ffprobe calls
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_pts_cache: dict[str, PTSResult] = {}
_meta_cache: dict[str, VideoMetadata] = {}


def _cache_key(video_path: str) -> str:
    """Normalise path for cache key（包含檔大小與修改時間，避免覆蓋後命中舊快取）"""
    import os
    base = os.path.normcase(os.path.normpath(video_path))
    try:
        st = os.stat(video_path)
        return f"{base}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return base


def invalidate_pts_cache(video_path: str | None = None) -> None:
    """清除 PTS 快取。video_path 為 None 時清除全部。"""
    with _cache_lock:
        if video_path is None:
            _pts_cache.clear()
            _meta_cache.clear()
        else:
            ck = _cache_key(video_path)
            _pts_cache.pop(ck, None)
            _meta_cache.pop(ck, None)


def _parse_rate(value: object) -> float:
    """解析 ffprobe 幀率字串（"600/1" / "30000/1001" / "0/0" / "N/A"）→ float"""
    if not value:
        return 0.0
    s = str(value)
    if s in ("N/A", "nan", "0/0"):
        return 0.0
    if "/" in s:
        try:
            num, den = s.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_float(value: object) -> float:
    if not value:
        return 0.0
    try:
        return float(str(value).strip()) if str(value).strip() not in ("N/A", "") else 0.0
    except ValueError:
        return 0.0


def probe_video_metadata(video_path: str, timeout: float = 10.0) -> VideoMetadata | None:
    """以 ffprobe 取得影片真實 metadata（帶快取）。

    兩階段設計（避免對正常影片付出 -count_frames 的昂貴代價）：
    1. 快速檔頭探測（無 -count_frames，毫秒級）— 取得 nb_frames / avg_frame_rate /
       r_frame_rate / format.duration。檔頭一致且合理（fps 在 1~120 且
       nb_frames/fps ≈ duration）時直接採用，不跑慢速計數。
    2. 檔頭可疑（fps 超出合理範圍、nb_frames 膨脹）時才跑 -count_frames
       逐封包計數 —— 大檔案可能耗時 10~30s（實測 177MB AVI 需 25s），
       因此只在必要時執行。

    動機：行車記錄器 AVI 檔頭 nb_frames/avg_frame_rate 被誤寫時
    （樣本檔宣稱 26477 幀/600fps，實際只解得出 223 幀/44.13s ≈ 5.05fps），
    nb_read_frames 是唯一可靠的真實幀數。

    失敗或 ffprobe 不存在時回傳 None，呼叫端須回退檔頭值。
    """
    ck = _cache_key(video_path)
    with _cache_lock:
        cached = _meta_cache.get(ck)
    if cached is not None:
        return cached

    _, ffprobe_path = find_ffmpeg()
    if ffprobe_path is None:
        return None
    try:
        # Phase 1: 快速檔頭探測
        fast_cmd = [
            ffprobe_path,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_frames,avg_frame_rate,r_frame_rate,duration",
            "-show_entries", "format=duration,format_name",
            "-of", "json",
            video_path,
        ]
        result = subprocess.run(fast_cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        streams = data.get("streams") or []
        fmt = data.get("format") or {}
        if not streams:
            return None
        stream = streams[0]
        meta = VideoMetadata(
            header_fps=_parse_rate(stream.get("avg_frame_rate"))
            or _parse_rate(stream.get("r_frame_rate")),
            header_frames=int(stream.get("nb_frames") or 0),
            actual_frames=int(stream.get("nb_frames") or 0),  # 先假設一致，Phase 2 覆寫
            duration=_parse_float(fmt.get("duration"))
            or _parse_float(stream.get("duration"))
            or 0.0,
            format_name=str(fmt.get("format_name") or ""),
        )
        if meta.header_frames <= 0:
            return None

        # 檔頭一致 → 直接採用（正常影片，毫秒完成）
        if meta.header_consistent:
            with _cache_lock:
                _meta_cache[ck] = meta
            return meta

        # Phase 2: 檔頭可疑 → 慢速 -count_frames 逐封包計數
        log.info(f"ffprobe metadata: 檔頭可疑（{meta.header_frames}幀/{meta.header_fps:.1f}fps），"
                 f"進行 -count_frames 逐封包計數（可能耗時）")
        slow_cmd = [
            ffprobe_path,
            "-v", "error",
            "-count_frames",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames",
            "-of", "json",
            video_path,
        ]
        try:
            result2 = subprocess.run(slow_cmd, capture_output=True, timeout=timeout)
            if result2.returncode == 0:
                data2 = json.loads(result2.stdout.decode("utf-8", errors="replace"))
                streams2 = data2.get("streams") or []
                if streams2:
                    meta.actual_frames = int(streams2[0].get("nb_read_frames") or 0)
                    log.info(
                        f"ffprobe metadata: -count_frames 完成，實際可解碼 "
                        f"{meta.actual_frames} 幀（檔頭宣稱 {meta.header_frames} 幀）"
                    )
        except subprocess.TimeoutExpired:
            log.warning(f"ffprobe -count_frames 逾時 ({timeout:.0f}s)，改用檔頭值")
        with _cache_lock:
            _meta_cache[ck] = meta
        return meta
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, ValueError):
        return None


def load_pts_and_keyframes(
    video_path: str,
    timeout: float = 15.0,
    duration_budget_factor: float = 0.1,
) -> PTSResult:
    """
    一次探測同時取得全幀 PTS + 關鍵幀時間（帶快取）

    Args:
        video_path: 影片路徑
        timeout: 基礎超時秒數 (預設 15s，AVI 檔案 ffprobe 較慢)
        duration_budget_factor: 影片長度 × 此係數 = 額外預算 (秒)

    Returns:
        PTSResult (pts_list, keyframe_times, source, quality)
    """
    # 快取命中直接回傳
    ck = _cache_key(video_path)
    with _cache_lock:
        cached = _pts_cache.get(ck)
    if cached is not None:
        log.info(f"PTS 快取命中: {video_path} ({len(cached.pts_list)} 幀, {cached.source})")
        return cached

    log.info(f"PTS 載入開始: {video_path}")

    # AVI 容器對 frame 級 ffprobe 極慢（日誌 30s 逾時 ×2），直接走 linear fallback
    ext = video_path.lower().rsplit(".", 1)[-1] if "." in video_path else ""
    is_avi = ext in ("avi", "mts", "m2ts")

    ffmpeg_path, ffprobe_path = find_ffmpeg()

    # 先用 ffprobe 取得影片長度以計算動態 timeout（需 ffprobe）
    if ffprobe_path is not None:
        try:
            dur_cmd = [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                       "-of", "csv=p=0", video_path]
            dur_result = subprocess.run(dur_cmd, capture_output=True, timeout=10, text=True)
            duration = float(dur_result.stdout.strip()) if dur_result.stdout.strip() else 0.0
        except Exception as e:
            log.warning(f"PTS 載入：取得影片長度失敗: {e}")
            duration = 0.0
    else:
        duration = 0.0

    # 動態 timeout 預先計算
    dynamic_timeout = max(timeout, duration * duration_budget_factor)
    if not is_avi:
        dynamic_timeout = min(dynamic_timeout, 20.0)

    log.info(f"PTS 載入：影片長度 {duration:.1f}s, timeout {dynamic_timeout:.0f}s")

    if is_avi:
        # AVI 檔頭一致（fps 合理且 nb_frames/fps ≈ duration）時直接以檔頭值生成
        # 線性 PTS —— 避免 frame 級 ffprobe 對大檔的昂貴代價
        # （實測 177MB/60s AVI 需 25s，而檔頭值已正確）。
        meta_probe = probe_video_metadata(video_path, timeout=timeout)
        if meta_probe is not None and meta_probe.header_consistent:
            pts_list = [i / meta_probe.header_fps for i in range(meta_probe.header_frames)]
            log.info(
                f"PTS 載入：AVI 檔頭一致，直接生成線性 PTS "
                f"({meta_probe.header_frames} 幀, {meta_probe.header_fps:.2f}fps, "
                f"{meta_probe.duration:.1f}s)"
            )
            result = PTSResult(pts_list, [], "linear_fallback", "UNSAFE")
            with _cache_lock:
                _pts_cache[ck] = result
            return result

        # 檔頭可疑（樣本檔 CH13_*.avi 宣稱 26477 幀/600fps，實際 223 幀/44.13s）
        # → 必須 frame 探測才能拿到真實時間戳（此類檔案封包層 dts 正確且探測快）。
        # 以短逾時嘗試，逾時或無資料才回退 linear（linear 會重用 -count_frames 結果）。
        first_timeout = min(dynamic_timeout, 8.0)

        # AVI 通常沒有 pkt_pts_time 但保留 pkt_dts_time/best_effort → 優先試 best_effort
        pts_result = _probe_pts(video_path, ffprobe_path, first_timeout, "best_effort_timestamp_time")
        if pts_result.pts_list:
            pts_result.quality = "DEGRADED"
            pts_result.source = "best_effort"
            log.info(f"PTS 載入成功 (best_effort-avi): {len(pts_result.pts_list)} 幀")
            with _cache_lock:
                _pts_cache[ck] = pts_result
            return pts_result

        # 再試 pkt_pts_time（部分 AVI 有寫）
        pts_result = _probe_pts(video_path, ffprobe_path, first_timeout, "pkt_pts_time")
        if pts_result.pts_list:
            log.info(f"PTS 載入成功 (pkt_pts_time-avi): {len(pts_result.pts_list)} 幀")
            with _cache_lock:
                _pts_cache[ck] = pts_result
            return pts_result

        log.info("PTS 載入：AVI frame 探測無資料，走 linear fallback")
        pts_result = _linear_fallback(video_path, ffprobe_path, min(dynamic_timeout, 10), format_duration=duration)
        if pts_result.pts_list:
            pts_result.quality = "UNSAFE"
            pts_result.source = "linear"
            log.info(f"PTS 載入成功 (linear-avi): {len(pts_result.pts_list)} 幀")
            with _cache_lock:
                _pts_cache[ck] = pts_result
            return pts_result
        # linear fallback 也失敗，回傳空結果
        log.error(f"PTS 載入失敗: AVI linear fallback 失敗 ({video_path})")
        return PTSResult([], [], "none", "UNSAFE")

    # 縮短兩次探測逾時：第一次 8s 快速失敗，第二次再用剩餘預算
    first_timeout = min(dynamic_timeout, 8.0) if not is_avi else dynamic_timeout
    pts_result = _probe_pts(video_path, ffprobe_path, first_timeout, "pkt_pts_time")
    if pts_result.pts_list:
        log.info(f"PTS 載入成功 (pkt_pts_time): {len(pts_result.pts_list)} 幀")
        with _cache_lock:
            _pts_cache[ck] = pts_result
        return pts_result

    # 2. 嘗試 best_effort_timestamp_time
    pts_result = _probe_pts(video_path, ffprobe_path, dynamic_timeout, "best_effort_timestamp_time")
    if pts_result.pts_list:
        pts_result.quality = "DEGRADED"
        pts_result.source = "best_effort"
        log.info(f"PTS 載入成功 (best_effort): {len(pts_result.pts_list)} 幀")
        with _cache_lock:
            _pts_cache[ck] = pts_result
        return pts_result

    # 3. 回退線性累加：用 r_frame_rate + nb_frames 估算
    pts_result = _linear_fallback(video_path, ffprobe_path, dynamic_timeout, format_duration=duration)
    if pts_result.pts_list:
        pts_result.quality = "UNSAFE"
        pts_result.source = "linear"
        log.info(f"PTS 載入成功 (linear fallback): {len(pts_result.pts_list)} 幀")
        with _cache_lock:
            _pts_cache[ck] = pts_result
        return pts_result

    log.error(f"PTS 載入失敗: 所有方法皆無法取得 PTS ({video_path})")
    return PTSResult([], [], "none", "UNSAFE")


def _probe_pts(
    video_path: str,
    ffprobe_path: str | None,
    timeout: float,
    entry: str,
) -> PTSResult:
    """探測 PTS"""
    if ffprobe_path is None:
        return PTSResult([], [], "none", "UNSAFE")
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", f"frame={entry},pict_type",
        "-of", "json",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            stderr_msg = result.stderr.decode("utf-8", errors="replace")[:200] if result.stderr else ""
            log.warning(f"ffprobe PTS 探測失敗 (returncode={result.returncode}): {stderr_msg}")
            return PTSResult([], [], "none", "UNSAFE")

        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        frames = data.get("frames", [])

        if not frames:
            log.warning(f"ffprobe PTS 探測：無幀資料 (entry={entry})")
            return PTSResult([], [], "none", "UNSAFE")

        pts_list: list[float] = []
        keyframe_times: list[float] = []
        for f in frames:
            if entry in f:
                try:
                    pts = float(f[entry])
                    pts_list.append(pts)
                    if f.get("pict_type") == "I":
                        keyframe_times.append(pts)
                except (ValueError, TypeError):
                    pass

        if pts_list:
            quality = "EXACT" if entry == "pkt_pts_time" else "DEGRADED"
            return PTSResult(pts_list, keyframe_times, entry, quality)
        else:
            log.warning(f"ffprobe PTS 探測：無有效 PTS (entry={entry}, frames={len(frames)})")
    except subprocess.TimeoutExpired:
        log.warning(f"ffprobe PTS 探測逾時 ({timeout:.0f}s, entry={entry})")
    except json.JSONDecodeError as e:
        log.warning(f"ffprobe PTS 探測 JSON 解析失敗: {e}")
    except OSError as e:
        log.warning(f"ffprobe PTS 探測 OS 錯誤: {e}")

    return PTSResult([], [], "none", "UNSAFE")


def _linear_fallback(
    video_path: str,
    ffprobe_path: str | None,
    timeout: float,
    format_duration: float = 0.0,
) -> PTSResult:
    """最後 fallback：用真實平均 fps + 實際幀數估算 PTS（無逐幀時間戳時）。

    format_duration: ffprobe format-level duration（秒），用於交叉驗證 nb_frames 與 fps。
                     部分 HEVC-in-AVI 容器的 r_frame_rate 與 nb_frames 皆不準確，
                     需同時交叉驗證兩者。

    A9: 優先使用 probe_video_metadata 的 nb_read_frames（demuxer 實際可解封包數）
    + format.duration 反推平均 fps —— 對檔頭損壞（nb_frames 膨脹）的容器，
    這比 r_frame_rate/nb_frames 更可靠，能產生正確長度的線性 PTS。
    """
    if ffprobe_path is None:
        return PTSResult([], [], "none", "UNSAFE")

    # 先以 -count_frames 的 ground truth 為準（帶快取，通常只多一次快速 ffprobe）
    meta = probe_video_metadata(video_path, timeout=timeout)
    if meta is not None:
        count, fps, dur = meta.estimate()
        if count > 0 and 1.0 <= fps <= 120.0 and dur > 0:
            log.info(
                f"PTS linear fallback: 以 probe metadata 生成線性 PTS "
                f"({count} 幀, {fps:.2f}fps, {dur:.1f}s, "
                f"header={meta.header_frames}幀/{meta.header_fps:.1f}fps, "
                f"actual={meta.actual_frames}幀)"
            )
            pts_list = [i / fps for i in range(count)]
            return PTSResult(pts_list, [], "linear_fallback", "UNSAFE")

    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,nb_frames",
        "-of", "json",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            return PTSResult([], [], "none", "UNSAFE")

        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        streams = data.get("streams", [])
        if not streams:
            return PTSResult([], [], "none", "UNSAFE")

        stream = streams[0]
        # 解析 r_frame_rate (如 "30000/1001")
        rfr = stream.get("r_frame_rate", "0/1")
        if "/" in rfr:
            num, den = map(int, rfr.split("/"))
            fps = num / den if den else 0.0
        else:
            fps = float(rfr)

        nb_frames = int(stream.get("nb_frames", 0)) or 0

        # 交叉驗證：nb_frames / fps 與 format_duration 差距 >20% 時修正
        # （部分 HEVC-in-AVI 容器的 r_frame_rate 與 nb_frames 皆不正確）
        # 先檢查原始值是否明顯不合理（fps>120 或 nb_frames 遠超合理上限 表示 metadata 完全不可信）
        # 這種情況下即便比例一致也不該信任 metadata（如 600fps/26477幀 對 44s 影片）
        if fps > 0 and nb_frames > 0 and format_duration > 0:
            # 檢查原始 fps 合理性：dashcam 影片 fps 通常 1~60，>120 明顯錯誤
            if fps > 120.0:
                log.warning(
                    f"PTS linear fallback: 原始 fps={fps:.1f} 不合理 (>120)，"
                    f"metadata 不可信，改用 OpenCV 測量"
                )
                return PTSResult([], [], "none", "UNSAFE")

            # 檢查 nb_frames 合理性：dashcam 影片最大合理幀數 = duration * 120fps
            # 若 nb_frames 遠超此上限，metadata 嚴重損壞（如 44s 影片有 26477 幀，max 應為 5292）
            max_reasonable_frames = int(format_duration * 120)
            if nb_frames > max_reasonable_frames:
                log.warning(
                    f"PTS linear fallback: nb_frames={nb_frames} 遠超合理上限 "
                    f"(max={max_reasonable_frames} for {format_duration:.1f}s @ 120fps)，"
                    f"metadata 不可信，改用 OpenCV 測量"
                )
                return PTSResult([], [], "none", "UNSAFE")

            reported_duration = nb_frames / fps
            if abs(reported_duration - format_duration) / format_duration > 0.2:
                # 兩種修正策略：
                # A) nb_frames 錯誤 → corrected_nb = format_duration * fps
                # B) fps 錯誤 → corrected_fps = nb_frames / format_duration
                # 選擇修正後 fps 在合理範圍 (1~120) 的策略
                corrected_nb_a = int(format_duration * fps)
                corrected_fps_b = nb_frames / format_duration

                if 1.0 <= corrected_fps_b <= 120.0:
                    # 策略 B：fps 不準（如 r_frame_rate=31 但實際5fps）
                    log.warning(
                        f"PTS linear fallback: r_frame_rate={fps:.1f} 異常 "
                        f"(nb_frames/fps={reported_duration:.1f}s vs format={format_duration:.1f}s)，"
                        f"修正 fps 為 {corrected_fps_b:.2f}"
                    )
                    fps = corrected_fps_b
                    # nb_frames 保持不動（用原始值，可能也不準，後續由 _run_loop 用 OpenCV 驗證）
                else:
                    # 策略 A：nb_frames 遠大於實際
                    # 但若 corrected_nb_a 仍暗示不合理 fps（>120），表示兩者皆錯，
                    # 無法從 metadata 修正，回傳空結果由 video_worker 用解碼測量修正
                    implied_fps = corrected_nb_a / format_duration
                    if implied_fps > 120.0:
                        log.warning(
                            f"PTS linear fallback: nb_frames={nb_frames} 與 r_frame_rate={fps:.1f} "
                            f"皆異常（修正後仍暗示 fps={implied_fps:.0f}），"
                            f"回傳空結果由解碼測量修正（A9）"
                        )
                        return PTSResult([], [], "none", "UNSAFE")
                    log.warning(
                        f"PTS linear fallback: nb_frames={nb_frames} 異常 "
                        f"(reported {reported_duration:.1f}s vs actual {format_duration:.1f}s)，"
                        f"修正為 {corrected_nb_a}"
                    )
                    nb_frames = corrected_nb_a

        # A9: 關鍵檢查——如果修正後的 nb_frames 仍與 OpenCV 可解碼幀數相差極大，
        # 表示 r_frame_rate 與 nb_frames 皆不適合該容器類型（如本例中的 AVI）。
        # 直接回傳空結果，由 video_worker._run_loop 使用 OpenCV 幀數 + 實際解碼時間重新測量。
        if fps > 0 and nb_frames > 0 and format_duration > 0:
            # 使用 OpenCV 獲取實際可解碼的 frame count
            import cv2
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                opencv_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                cap.release()
            else:
                opencv_total = 0

            # 若修正後的 nb_frames 仍遠大於 OpenCV 可解碼幀數（>2倍），
            # 表示 metadata 皆不正確，回傳空結果讓 video_worker 使用 OpenCV 測量
            if opencv_total > 0 and nb_frames > opencv_total * 2:
                log.warning(
                    f"PTS linear fallback: corrected nb_frames={nb_frames} 仍遠超 "
                    f"OpenCV 可解碼={opencv_total}，直接回傳空結果供 OpenCV 測量"
                )
                return PTSResult([], [], "none", "UNSAFE")

        if fps > 0 and nb_frames > 0:
            pts_list = [i / fps for i in range(nb_frames)]
            return PTSResult(pts_list, [], "linear_fallback", "UNSAFE")
    except Exception:
        pass

    return PTSResult([], [], "none", "UNSAFE")


def get_keyframe_positions(video_path: str) -> list[float]:
    """取得關鍵幀時間位置（秒）- 直接用 ffprobe 取得，不遞迴呼叫 load_pts_and_keyframes"""
    # 先查快取
    ck = _cache_key(video_path)
    with _cache_lock:
        cached = _pts_cache.get(ck)
    if cached is not None and cached.keyframe_times:
        return cached.keyframe_times

    # 快取有 PTS 但無 keyframe → 補取
    if cached is not None and cached.pts_list and not cached.keyframe_times:
        kft = _probe_keyframe_times(video_path)
        if kft:
            cached.keyframe_times = kft
        return kft

    # 無快取 → 直接 ffprobe 取 keyframe（不觸發完整 PTS 探測）
    return _probe_keyframe_times(video_path)


def _probe_keyframe_times(video_path: str) -> list[float]:
    """直接用 ffprobe 取得關鍵幀時間（最小化 ffprobe 呼叫）"""
    _, ffprobe_path = find_ffmpeg()
    if ffprobe_path is None:
        return []

    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "frame=pts_time,pict_type",
        "-of", "json",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        frames = data.get("frames", [])
        keyframe_times: list[float] = []
        for f in frames:
            if f.get("pict_type") == "I" and "pts_time" in f:
                try:
                    keyframe_times.append(float(f["pts_time"]))
                except (ValueError, TypeError):
                    pass
        return keyframe_times
    except Exception:
        return []


# 便利函數
def load_pts_and_keyframes_sync(
    video_path: str,
    timeout: float = 15.0,
) -> tuple[list[float], list[float], str]:
    """同步載入 PTS 和 keyframe，回傳 (pts_list, keyframe_times, quality)"""
    result = load_pts_and_keyframes(video_path, timeout)
    return result.pts_list, result.keyframe_times, result.quality


def get_reliable_fps_count(video_path: str, timeout: float = 10.0) -> tuple[float, int] | None:
    """回傳 (fps, frame_count) 的可靠值（ffprobe 交叉驗證，帶快取）。

    供 gps_parser / gps_ocr / gps_roi_detector / speed_roi_detector /
    marking_detector 等取樣與時間計算共用 —— 這些模組以往直接信賴
    cap.get(CAP_PROP_FPS/CAP_PROP_FRAME_COUNT)，遇到檔頭損壞的 AVI
    （宣稱 600fps/26477 幀，實際 5fps/223 幀）會把取樣/速度全部算錯。

    無法取得可靠值（ffprobe 不存在/逾時/資料異常）時回傳 None，
    呼叫端仍可回退 OpenCV 檔頭值。
    """
    meta = probe_video_metadata(video_path, timeout=timeout)
    if meta is None:
        return None
    count, fps, _ = meta.estimate()
    if count <= 0 or not (1.0 <= fps <= 120.0):
        return None
    return fps, count
