# PR: 增加影片擷取功能 + 截圖效能改善

> **注意**：本文件為重構前原始單檔設計稿。專案已重構為套件結構（`dashcam_player/`），詳見 `AGENTS.md`。以下變更已分散至 `controller.py` / `video_player.py` / `export_worker.py` / `ffmpeg.py` 等模組。

## 變更摘要

| 項目 | 內容 |
|------|------|
| **核心路徑** | FFmpeg stream copy（-ss + -t + -c copy），不重新編碼 |
| **截圖格式** | 下拉選單 JPEG / PNG，預設 JPEG |
| **音軌處理** | 預設保留，ffprobe 偵測無音訊時自動忽略 |
| **架構** | 維持單檔 video_player.py |

## 決策紀錄

1. **影片擷取核心路徑**：FFmpeg stream copy 為主，不經過 OpenCV pipe，速度快且不損畫質
2. **截圖格式**：預設 JPEG（行車紀錄器存證情境夠用），提供 PNG 選項
3. **音軌**：預設保留，無音訊時自動忽略（ffprobe 偵測）
4. **架構**：維持單檔設計，不拆分多個 .py 檔案

---

## 具體變更清單（video_player.py）

### 1. 新增 import

```python
import subprocess
import json
```

### 2. 新增工具函式

- `_probe_media_info(video_path)` — ffprobe 偵測音軌、視訊編碼、時長
- `_find_ffmpeg()` — 找到 ffmpeg/ffprobe 可執行檔路徑

### 3. 新增 UI 控件

右側輸出設定區新增：

| 控件 | 說明 |
|------|------|
| `img_format_combo` (QComboBox) | 截圖格式：JPEG / PNG，預設 JPEG |
| **影片擷取設定** (QGroupBox) | 獨立區塊 |
| `vid_format_combo` (QComboBox) | 輸出格式：MP4 / MKV，預設 MP4 |
| `keep_audio_check` (QCheckBox) | 保留音軌，預設勾選 |
| `copy_codec_check` (QCheckBox) | 使用原始編碼（stream copy），預設勾選，tooltip 說明 keyframe 精度 |
| `vid_start_edit` (QLineEdit) | 開始時間 hh:mm:ss |
| `vid_end_edit` (QLineEdit) | 結束時間 hh:mm:ss |
| `btn_capture_video` (QPushButton) | 「擷取影片」按鈕 |

### 4. 截圖格式切換邏輯

- 選 JPEG → 顯示 quality_spin
- 選 PNG → 隱藏 quality_spin

### 5. 影片擷取流程

```
1. 決定時間範圍：优先用 vid_start_edit/vid_end_edit，否則用標記
2. ffprobe 偵測是否有音軌
3. 組裝 FFmpeg 指令：
   ffmpeg -y -i INPUT -ss START -t DURATION -c:v copy [-c:a copy | -an] OUTPUT
4. 以 QThread 背景執行 subprocess
5. 完成後更新進度條與狀態列
```

### 6. VideoExportWorker (QThread)

```python
class VideoExportWorker(QThread):
    finished = pyqtSignal(bool, str)  # (success, message)
    progress = pyqtSignal(int)        # 百分比 0-100
```

### 7. 批次截圖改良

- batch size 依格式區分：JPEG=8、PNG=3（瓶頸在磁碟 I/O 而非 GPU）
- 開始前偵測目標目錄是否已有同前綴檔案，有則弹出確認對話框避免靜默覆蓋

### 8. 選單列更新

```
截取
├── 截取單張          Ctrl+S
├── 批次截取區間       Ctrl+B
├── 擷取影片          Ctrl+V   ← 新增
```

### 9. 邊界問題修正（Code Review 後）

- `_open_video` 重設時清除 `vid_start_edit` / `vid_end_edit`，避免跨影片殘留
- `_capture_range` 執行前掃描目錄，偵測同前綴檔案時弹出確認對話框
- `-ss` 改放到 `-i` 之後（output seeking），避免 `-c:v copy` 搭配 input seeking 造成黑色畫面與時間偏移
- 移除多餘的 `-avoid_negative_ts make_zero`（output seeking 不會產生負數時間戳記）

### 10. UI 跳動修正（Code Review 後）

- `progress_bar` 改用固定高度 18px + 樣式切換（透明隱藏），不再用 `setVisible` 造成 layout 重排
- `quality_spin` 改用 `setEnabled`（灰色停用）取代 `setVisible`（隱藏），避免 Grid 行塌陷
- `_display_frame` 移除每幀呼叫 `setStyleSheet`，改在 `_on_frame_ready` 第一幀時設定一次背景色

### 11. 除錯日誌系統（Code Review 後新增）

- 存放於 `tempfile.gettempdir() / dashcam_debug.log`（系統 tmp，重開機自動清除）
- 每次啟動覆寫舊檔，同一次 session 持續附加
- 選單 `說明 → 檢視除錯日誌`（Ctrl+L）開啟唯讀對話框，可即時查看
- 記錄點：

| 位置 | 層級 | 記錄內容 |
|------|------|----------|
| `main()` | INFO | 應用程式啟動 |
| `VideoPlayer.__init__` | INFO | 硬體加速偵測結果（CUDA/OpenCL/軟體） |
| `_open_video` | INFO | 開啟的影片路徑 |
| `VideoWorker.run()` 成功 | INFO | codec fourcc（avc1/hevc 等）、幀數、fps |
| `VideoWorker.run()` 失敗 | ERROR | 無法開啟 / 幀數為 0 / 第一幀讀取失敗 |
| `VideoWorker` 播放中 | WARN | seek 後 read 失敗、播放中 decode 失敗 |
| `_capture_video` | INFO | FFmpeg 完整指令 |
| `_on_video_export_finished` | INFO/ERROR | 影片擷取成功／失敗 + stderr |

### 12. Bug 修復（Code Review 後）

- `_init_ui` 中兩次 `setStyleSheet` 呼叫造成 border 樣式被第二個覆蓋消失，合併為一個 stylesheet
- `_display_frame` 中 `QImage(rgb.data, ...)` 不複製 buffer，加 `.copy()` 確保資料所有權，避免隨機畫面花掉
- 批次截圖寫入失敗時進度條卡住（`_capture_idx` 只在成功時遞增），拆分 `_capture_success`（成功數）與 `_capture_idx`（進度），進度條永遠可達 100%
- `_find_ffmpeg` 簡化：16 行 `for...else` 縮減為 4 行（`shutil.which(...) or shutil.which(...)`），行為不變

---

## Milestones

| Milestone | 內容 | 交付件 | 狀態 |
|-----------|------|--------|------|
| **M1** | import + 工具函式 + ffprobe | `_probe_media_info`, `_find_ffmpeg` | ✅ |
| **M2** | UI 控件（影片擷取區塊 + 截圖格式下拉） | 右側面板完整佈局 | ✅ |
| **M3** | VideoExportWorker + FFmpeg stream copy | 影片擷取核心功能 | ✅ |
| **M4** | 截圖格式切換 + PNG 支援 + batch 改良 | 截圖功能升級 | ✅ |
| **M5** | 硬體偵測整合 + batch size 自動調整 | 效能優化 | ✅ |
| **M6** | 整體測試 + 回歸驗證 | 可穩定運行 | ✅ |

---

## 驗證清單

- [ ] 開啟影片後，右側顯示「影片擷取設定」區塊
- [ ] 截圖格式下拉可切換 JPEG/PNG，PNG 時品質設定變灰（非隱藏）
- [ ] 擷取影片：有音軌的 MP4 → 輸出 MP4 保留音軌
- [ ] 擷取影片：無音軌的 MP4 → 輸出 MP4 不報錯
- [ ] 擷取影片：取消保留音軌 → 輸出無音軌
- [ ] 擷取影片：時間範圍輸入有效（hh:mm:ss 格式）
- [ ] 擷取影片：時間範圍為空時，使用標記範圍
- [ ] 擷取影片：stream copy 失敗時回退 re-encode
- [ ] 擷取影片：進度條正確顯示
- [ ] 擷取影片：UI 不卡頓（背景執行）
- [ ] 批次截取 PNG 格式正常運作
- [ ] 批次截取 JPEG 格式正常運作
- [ ] 原有截圖、播放、標記功能不受影響
- [ ] 開第二部影片時，時間範圍欄位已自動清除
- [ ] 對同一區間跑兩次批次截取，第二次弹出覆蓋確認
- [ ] stream copy 起點精度符合 keyframe 說明（tooltip 已更新）
- [ ] 切換截圖格式時下半部面板不跳動
- [ ] 影片擷取過程中進度條出現/消失時面板不跳動
- [ ] 說明 → 檢視除錯日誌（Ctrl+L）可開啟日誌對話框
- [ ] H.264/H.265 影片開啟失敗時，日誌記錄 codec fourcc + OpenCV 版本
- [ ] 批次截圖中間有寫入失敗時，日誌記錄警告，進度條仍可達 100%
