# 行車紀錄器影片播放與截圖工具 - 修正計畫 & 完成報告

> 生成日期：2026-06-13
> 總計：9 項缺失 → **9 項已修正** (Round 1) + **12 項新發現 → 12 項已修正** (Round 2) + **5 項 GPS Bug → 5 項已修正** (Round 3) + **7 項 GPS 範圍設定 Bug + 1 新功能 → 全部完成** (Round 4)

---

## Round 4 修正摘要（2026-08-16 — GPS 文字偵測範圍設定 & OCR 辨識品質）

| 優先級 | 項目 | 類別 | 檔案 | 狀態 |
|--------|------|------|------|------|
| P0 | #27 ROI 選取框拖曳壓扁成 1x1 / 縮放失效（`parentItem()` 為 None） | 正確性 | gps_roi_adjust.py | ✅ |
| P0 | #28 設定的 ROI 未套用於全片 OCR 提取（`extract_tracks` 忽略 roi） | 正確性 | gps_ocr.py, gps_parser.py | ✅ |
| P0 | #29 ROI 裁切強制 2x 放大傷害辨識（`24.338535` → `24.53585355`） | 正確性 | gps_ocr.py | ✅ |
| P0 | #30 緊貼文字的 ROI 辨識失敗（Otsu 全域門檻偏移） | 正確性 | gps_ocr.py | ✅ |
| P1 | #31 ROI 辨識失敗時無全畫面備援（AGENTS.md 記載但未實作） | 穩定性 | gps_parser.py | ✅ |
| P1 | #32 繪製模式舊 ROI 方框殘留 / `scene.addItem` 重複加入警告 | 使用者體驗 | gps_roi_adjust.py | ✅ |
| P2 | #33 GPS 訊號漂移過濾（新功能：可忽略與偵測位置差距過大的異常點） | 新功能 | gps_parser.py, settings.py, gps_settings.py, capture_settings.py | ✅ |

---

## Round 4 各項修正詳情

### #27 🔴 ROI 選取框拖曳/縮放失效 ✅
- **位置**: `ui/dialogs/gps_roi_adjust.py` — `_display_frame()` / `_on_view_mouse_release()`
- **變更**: `_ResizableRectItem` 改以 `parent=image_item` 建立（child 自動加入 scene）
- **原因**: 選取框直接 `scene.addItem()` 加入時 `parentItem()` 為 `None`，clamp 範圍退化成 `QRectF(0,0,1,1)` → 拖曳瞬間方框被壓扁成 1x1 像素、縮放因 `width>20` 檢查永遠失敗，範圍無法設定
- **實測**: 拖曳/縮放/邊界 clamp 全部恢復正常

### #28 🔴 設定的 ROI 未套用於全片提取 ✅
- **位置**: `utils/gps_ocr.py:159` `extract_tracks()` / `utils/gps_parser.py` `_extract_from_ocr()`
- **變更**:
  - `GPSOCR.extract_tracks()` 新增 `roi` 參數並傳入每幀 `ocr_frame(..., roi=roi)`
  - `GPSParser._ocr_roi` 記錄偵測階段使用的 ROI（已知設定或自動偵測結果），`_extract_from_ocr()` 帶入提取
- **原因**: 全片軌跡提取呼叫 `ocr_frame(frame)` 完全沒帶 ROI，只有偵測探測階段使用 → 使用者框好的範圍在實際提取時被忽略，GPS 偵測不到

### #29 🔴 ROI 裁切強制 2x 放大傷害辨識 ✅
- **位置**: `utils/gps_ocr.py` `_recognize()`
- **變更**: 移除無條件 `cv2.resize(fx=2, INTER_CUBIC)`，改為多策略依序嘗試：**CLAHE 灰階直送**（Tesseract 內部自適應二值化，實測最準）→ Otsu 二值化 → Otsu 放大 2x/3x（僅小字最後備援）
- **原因**: 實測中大型文字（28px）2x INTER_CUBIC 放大後 `24.338535` 被誤判成 `24.53585355`（3→5）；無放大 100% 正確。小字（14px）則需放大 3x 才正確，故放大降為 fallback
- **實測**: 大字/小字 × 寬框/緊框 四種情境全數正確辨識 `24.338535, 120.455958`

### #30 🔴 緊貼文字的 ROI 辨識失敗 ✅
- **位置**: `utils/gps_ocr.py` `ocr_frame()`
- **變更**: ROI 裁切前先對外擴充邊距（寬 5% / 高 15%，最小 4px）
- **原因**: 緊貼文字的裁切（無邊距）時 Otsu 全域門檻在「大片純色背景 + 小面積文字」下把文字筆畫侵蝕掉，OCR 輸出 `Z4.Q90099N` 垃圾 → 回傳 None → 偵測失敗

### #31 🟡 ROI 失敗無全畫面備援 ✅
- **位置**: `utils/gps_parser.py` `_has_ocr_gps()`
- **變更**: 已知 ROI 在取樣幀上辨識失敗時自動退回全畫面 OCR；全畫面成功時把 `parser._ocr_roi` 設為 `None`，後續全片提取也走全畫面
- **原因**: AGENTS.md 記載的「ROI 失敗時改用全畫面 OCR」雙重策略從未實作——ROI 一失敗就整段放棄（且因已存在 ROI 設定而跳過自動偵測）

### #32 🟡 繪製模式舊框殘留 + addItem 警告 ✅
- **位置**: `ui/dialogs/gps_roi_adjust.py` `_toggle_draw_mode()` / `_on_view_mouse_release()`
- **變更**: 進入繪製模式隱藏舊 `_roi_item`；繪製完成時先 `scene.removeItem()` 舊框再替換；移除帶 parent item 的多餘 `scene.addItem()`（child 已自動加入）
- **原因**: 舊的綠色方框在繪製新框時仍顯示造成干擾；`QGraphicsScene::addItem: item has already been added to this scene` 警告來自對已帶 parent 的 item 重複 addItem

### #33 🟢 GPS 訊號漂移過濾（新功能） ✅
- **位置**: `utils/gps_parser.py`（`_haversine_m()` + `filter_gps_drift()`）、`config/settings.py`（`load_gps_filter()`）、`models/capture_settings.py`（`gps_filter_enabled` / `gps_filter_max_distance`）、`ui/dialogs/gps_settings.py`（過濾群組 UI）
- **變更**: GPS 設定對話框新增「GPS 訊號過濾」勾選 + 最大距離（預設 2000m）。`extract_gps_tracks()` 提取完成時套用過濾：丟棄「與前一點距離超過門檻且前後兩點彼此接近」的飄移點（跳離軌跡又跳回原位）；連續大跳躍（真實快速移動）與端點保留
- **原因**: GPS 訊號（尤其 OCR 辨識）偶發飄移產生遠離實際位置的異常點，需可設定的過濾機制
- **測試**: 新增 7 個單元測試（haversine 距離、突波丟棄、連續移動保留、端點保留、門檻停用、短軌跡）

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
