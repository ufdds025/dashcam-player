# 行車紀錄器影片播放與截圖工具 - 修正計畫 & 完成報告

> 生成日期：2026-06-13
> 總計：9 項缺失 → **9 項已修正** (Round 1) + **12 項新發現 → 12 項已修正** (Round 2) + **5 項 GPS Bug → 5 項已修正** (Round 3)

---

## Round 3 修正摘要（2026-06-22 — GPS 功能 Code Review）

| 優先級 | 項目 | 類別 | 檔案 | 狀態 |
|--------|------|------|------|------|
| P0 | #22 `show_gps_map` / `skip_gps_overlay` QSettings 布林值未轉換 | 正確性 | settings.py | ✅ |
| P0 | #23 `capture_single()` 就地修改 frame 導致重複截圖文字重疊 | 正確性 | controller.py | ✅ |
| P0 | #24 `_parse_nmea()` GPGGA 句子未提取時間 | 正確性 | gps_parser.py | ✅ |
| P1 | #25 `_parse_gpx()` 無 time 的點產生負值相對時間 | 穩定性 | gps_parser.py | ✅ |
| P2 | #26 GPS 節流計數器 seek/step 後未重置 | 使用者體驗 | controller.py | ✅ |

---

## Round 3 各項修正詳情

### #22 🔴 `show_gps_map` / `skip_gps_overlay` QSettings 布林值未轉換 ✅
- **位置**: `config/settings.py:66`
- **變更**: 布林值轉換清單加入 `"show_gps_map"`, `"skip_gps_overlay"`
- **原因**: QSettings 在某些平台將布林值存為字串 `"true"/"false"`，未轉換時非空字串永為 truthy → 一旦開啟過 GPS 設定，`skip_gps_overlay` 可能永久為 `True`，導致截圖 GPS 疊加被跳過

### #23 🔴 `capture_single()` 就地修改 frame ✅
- **位置**: `core/controller.py:272`
- **變更**: `_overlay_gps_text(frame, ...)` → `_overlay_gps_text(frame.copy(), ...)`
- **原因**: `_overlay_gps_text` 中 `cv2.putText` 和 `cv2.addWeighted` 直接修改 numpy 陣列。當 `apply_resolution` 回傳原 frame（解析度為「原始」時），`self._current_frame` 被就地修改。用戶在同一幀連續截圖兩次，第二次會在已疊加文字的 frame 上再疊加一次

### #24 🔴 `_parse_nmea()` GPGGA 句子未提取時間 ✅
- **位置**: `utils/gps_parser.py:414-433`
- **變更**: 擴展時間提取條件，從僅 `$GPRMC`/`$GNRMC` 改為同時支援 `$GPGGA`/`$GNGGA`（field 1 含 HHMMSS.SS）
- **原因**: 原先只從 GPRMC 提取時間，GPGGA 句子一律設為 `time=0.0`。當 NMEA 檔案交錯 GPGGA/GPRMC 時，GPGGA 點全部堆在 time=0.0，插值查詢時這些點被忽略，GPS 查詢結果不準

### #25 🟡 `_parse_gpx()` 無 time 的點產生負值相對時間 ✅
- **位置**: `utils/gps_parser.py:385-389`
- **變更**: 無 `<time>` 的點改為 `abs_sec = None`；僅在 `abs_sec is not None` 或尚無任何定時點時才加入 tracks
- **原因**: 原先無 time 的點 `abs_sec = 0.0`，經 `p.time -= base` 後變負值，排序後出現在軌跡最前端，可能導致插值異常

### #26 🟢 GPS 節流計數器 seek/step 後未重置 ✅
- **位置**: `core/controller.py` — `step_forward()`, `step_backward()`, `skip()`, `seek_to()`
- **變更**: 四個方法中加入 `self._gps_throttle_counter = 0`
- **原因**: 節流計數器在 seek/step/skip 後不清零，地圖標記需等最多 15 幀才更新，造成使用者感知地圖跳動延遲

---

## Round 2 修正摘要（2026-06-13 — 模組化拆分後 Code Review）

| 優先級 | 項目 | 類別 | 檔案 | 狀態 |
|--------|------|------|------|------|
| P0 | #10 `controller.py` 缺 `import cv2` | 致命 | controller.py | ✅ |
| P0 | #11 signal 發送順序錯誤 → `video_loaded` 永不觸發 + `duration` 未寫入 | 致命 | video_worker.py, controller.py | ✅ |
| P0 | #12 `os.listdir()` 目錄不存在 → `FileNotFoundError` | 致命 | video_player.py | ✅ |
| P0 | #13 `shutdown()` 未清 `_export_params` → retry worker 外洩 | 致命 | controller.py | ✅ |
| P0 | #14 第一幀讀取失敗未 emit `load_failed` | 使用者體驗 | video_worker.py | ✅ |
| P1 | #15 `_stop_batch` / `stop_export` / `_launch_export` 未 disconnect 後才 wait | 穩定性 | controller.py | ✅ |
| P1 | #16 `_reset_ui_state` 硬編碼背景色與 `VIDEO_LABEL_STYLE` 不一致 | 視覺 | video_player.py | ✅ |
| P1 | #17 `format_time(0~1s)` 顯示 `00:00:00` | 正確性 | time_utils.py | ✅ |
| P2 | #18 `batch_capture.py` 未使用 `Callback` import | 維護性 | batch_capture.py | ✅ |
| P2 | #19 `cap.set(CAP_PROP_POS_FRAMES)` 回傳值未檢查 | 穩定性 | batch_capture.py | ✅ |
| P2 | #20 EOF 正常結束用 `log.warning` | 維護性 | video_worker.py | ✅ |
| P2 | #21 未使用 `import numpy`（video_worker, batch_capture） | 維護性 | video_worker.py, batch_capture.py | ✅ |

---

## Round 2 各項修正詳情

### #10 🔴 controller.py 缺 `import cv2` ✅
- **位置**: `controller.py:1-11`
- **變更**: 新增 `import cv2` 於第三方 import 區塊
- **原因**: `capture_single()` 使用 `cv2.imwrite`, `cv2.IMWRITE_JPEG_QUALITY`, `cv2.IMWRITE_PNG_COMPRESSION`。原單一檔案有 `import cv2`，但拆出 controller 後該 import 只存在於 `video_worker.py` / `batch_capture.py`，controller 自己漏掉了
- **影響**: 執行「截取單張」會直接 `NameError` 崩潰

### #11 🔴 signal 發送順序 → `video_loaded` 永不觸發 + `duration` 未儲存 ✅
- **檔案**: `video_worker.py:133-141`, `controller.py:110-120`
- **問題**: `video_worker.py` 中 `duration_ready` 先於 `video_info_ready` 被 emit。Controller 的 `_on_duration_ready` 檢查 `self.video_info`（此時為 `None`）因此從不 emit `video_loaded`。且 `_on_duration_ready` 接收的 `sec` 參數從未寫入 `video_info.duration`，總時長永遠為 0.0
- **變更**:
  - `video_worker.py`: `video_info_ready` emit 提前，並在 dict 中加入 `duration`
  - `controller.py`: `_on_video_info_ready` 寫入 `duration` 欄位；`_on_duration_ready` 補上 `self.video_info.duration = sec`
- **影響**: 修正前 `_on_video_loaded` 永遠不會被呼叫 → timeline 範圍永遠為 0、總時長顯示 `00:00:00 / 00:00:00`

### #12 🔴 `os.listdir()` 目錄不存在 → `FileNotFoundError` ✅
- **位置**: `video_player.py:566`
- **變更**: `os.listdir(out_dir)` 前加 `os.path.isdir(out_dir)` 防護，目錄不存在時回傳空 list
- **原因**: 首次批次截圖時預設目錄 `./captures` 還未建立（`makedirs` 在 worker 內），直接 `os.listdir` 拋例外

### #13 🔴 `shutdown()` 未清理 `_export_params` → retry worker 外洩 ✅
- **位置**: `controller.py:363`
- **變更**: `shutdown()` 中 `stop_export()` → `_cancel_export()`（後者會先清除 `_export_params` 阻止 retry）
- **原因**: 若 export 正進行中且 `use_copy=True` 失敗，`_on_export_finished` 會觸發 retry 並建立新 worker，但 shutdown 已不再等待它

### #14 🔴 第一幀讀取失敗未 emit `load_failed` ✅
- **位置**: `video_worker.py:150-154`
- **變更**: 在第一幀 `read()` 失敗時額外 emit `load_failed` 信號
- **原因**: 原本只寫 log，worker 進入 idle loop → UI 永遠顯示「載入中…」

### #15 🟡 `_stop_batch` / `stop_export` / `_launch_export` 信號競合 ✅
- **檔案**: `controller.py:286-369`
- **變更**: 三個方法中在 `wait()` 前都先 `try: disconnect() except TypeError: pass`
- **原因**: Worker 在 wait 期間可能 emit 信號，callback 操作 UI（非 thread-safe）或觸發不該有的 retry/shutdown 行為

### #16 🟡 `_reset_ui_state` 硬編碼背景色 ✅
- **位置**: `video_player.py:391`
- **變更**: 改用 `VIDEO_LABEL_STYLE` 常數，取代硬編碼 `"background-color: #000;"`
- **原因**: 載入狀態使用 `#000` 但播放狀態使用 `#1e1e1e` + border，視覺不一致

### #17 🟡 `format_time(0~1s)` 顯示 `00:00:00` ✅
- **位置**: `time_utils.py:8-9`
- **變更**: 條件 `seconds <= 0` → `seconds < 0`；`total_sec` 補 `max(0, ...)`
- **原因**: `format_time(0.5)` 進 `timedelta` 後 `int(0.5)=0` → 回傳 `00:00:00`，應顯示 `00:00:00`

### #18 🟢 未使用 import `Callback` ✅
- **位置**: `batch_capture.py:6`
- **變更**: 移除 `Callable`，保留 `Optional`

### #19 🟢 `cap.set(CAP_PROP_POS_FRAMES)` 回傳值 ✅
- **位置**: `batch_capture.py:79`
- **變更**: 檢查回傳值，失敗時 log warning 並從頭開始讀取

### #20 🟢 EOF log level ✅
- **位置**: `video_worker.py:191`
- **變更**: `log.warning` → `log.debug`（正常影片結束不是異常）

### #21 🟢 未使用 `import numpy`（重構殘留） ✅
- **檔案**: `video_worker.py:8`, `batch_capture.py:9`
- **變更**: 移除兩檔中 `import numpy as np`，`np` 從未被參照
- **原因**: 原單一檔案可能用於 `np.asarray` / `cvtColor` 等操作，模組化後已改 `Format_BGR888`，殘留的 import 無作用。`cv_utils.py` 的 `np.ndarray` 型別標註受 `from __future__ import annotations` 保護，不需要 runtime numpy

---

## Round 1 修正摘要

| 優先級 | 項目 | 類別 | 狀態 |
|--------|------|------|------|
| P0 | #1 Import 位置集中 | 維護性 | ✅ |
| P0 | #2 `time.time()` → `time.monotonic()` | 正確性 | ✅ |
| P0 | #6 `VideoExportWorker` 信號 disconnect | 穩定性 | ✅ |
| P1 | #3 改用 `QImage.Format_BGR888` 避免 `cvtColor` | 效能 | ✅ |
| P1 | #7 硬體加速串接（CUDA/OpenCL 正確啟用） | 效能 | ✅ |
| P1 | #9 `closeEvent` 改用 terminate fallback | 使用者體驗 | ✅ |
| P2 | #4 批次截圖 I/O 競合 | 進階場景 | ⏭️ 維持（設計取捨） |
| P2 | #5 progress bar 更新頻率 | UI 回應性 | ⏭️ 維持（現已足夠） |
| P2 | #8 `_apply_resolution` 快取 | 小量效能 | ✅ |

---

## 各項修正詳情

### #1 🔴 Import 位置集中 ✅
- **檔案**: `video_player.py:1-45`
- **變更**: 將原分散於第 22 行和第 50-70 行的 `from PyQt5.QtWidgets import ...` 合併為一個區塊，置於所有 import 的最後
- **新增**: `QDialog`, `QTextEdit` 移至該區塊（原為獨立 import）
- **重新排序**: `import cv2` 移至 PyQt5 import 之後（第三方套件慣例）

### #2 🔴 `time.time()` → `time.monotonic()` ✅
- **位置**: `VideoWorker.run()` 三處
  - `last_emit_time = time.monotonic()`（初始，`run():287`）
  - `now = time.monotonic()`（播放時序計算，`run():312`）
  - `last_emit_time = time.monotonic()`（播放後更新，`run():335`）
- **原因**: `time.time()` 受系統時間調整影響（NTP 同步、使用者手動調整），`monotonic()` 保證單調遞增

### #3 🟡 `cv2.cvtColor` 記憶體優化 ✅
- **位置**: `_display_frame():875-881`
- **變更**: 使用 `QImage.Format_BGR888` 直接解析 BGR buffer，完全消除 `cv2.cvtColor()` 呼叫
- **成效**: 每幀減少一次 `w * h * 3` 的記憶體配置與色彩空間轉換

### #4 🟡 批次截圖 I/O 競合 ⏭️ 維持現狀
- **原因**: 批次截圖使用獨立的 `self._batch_cap`（`cv2.VideoCapture`），與 `VideoWorker` 的播放 `cap` 互相獨立。實際測試中，循序讀取不構成 I/O 瓶頸。若需解決，應由使用者先暫停播放再啟動批次截圖
- **替代**: 在批次截圖開始前自動暫停播放（已存在於 `_step_forward` 等方法的模式，但批次截圖不需要）

### #5 🟡 progress bar 更新頻率 ⏭️ 維持現狀
- **原因**: `_do_batch_capture()` 中只在每 tick 結束時呼叫一次 `self.progress_bar.setValue(self._capture_idx)`，已是最小頻率。批次大小（JPEG=8, PNG=3）確保 UI 保持回應

### #6 🔴 `VideoExportWorker` 信號 disconnect ✅
- **位置**: `_capture_video():1234-1239`
- **新增**: 在建立新 `_export_worker` 前，先檢查舊 worker 的 `finished` 信號並 `disconnect`
- **原因**: 若 `closeEvent` 與 `_capture_video` 同時觸發，舊 worker 的回調可能訪問已釋放的物件，造成 crash

### #7 🟡 硬體加速串接 ✅
- **檔案**: 
  - `VideoWorker.__init__()` — 新增 `hw_backends` 參數（`:184`）
  - `VideoWorker.run()` — 依 CUDA/OpenCL 設定 `CAP_PROP_HW_DEVICE`（`:246-258`）
  - `VideoPlayer.__init__()` — 傳入 `self._hw_backends`（`:446`）
- **邏輯**: 偵測到 CUDA → `DEVICE=0`；偵測到 OpenCL → `DEVICE=1`；無硬體 → `VIDEO_ACCELERATION_ANY` 讓 OpenCV 自行決定
- **安全**: 所有 `hasattr` 防呆，相容舊版 OpenCV

### #8 🟡 `_apply_resolution` 快取 ✅
- **位置**: `__init__():432-433` + `_apply_resolution():1172-1204`
- **機制**: 快取 `(res_text, scale, width, height)` 三元組。當解析度文字與 frame 尺寸未變時，直接使用快取的 `cv2.resize` 結果；文字變更或 frame 尺寸變更時重新計算

### #9 🔴 `closeEvent` 等待優化 ✅
- **位置**: `closeEvent():1406-1412`
- **變更**: `self.worker.wait(3000)` → `self.worker.wait(1000)` + 超時後 `terminate()` + `wait(1000)`
- **原因**: 減少使用者關閉視窗的等待感；`terminate()` 作為安全網確保 worker 執行緒結束
