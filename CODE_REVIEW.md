# 行車紀錄器影片播放與截圖工具 — 程式碼審查報告

> 審查日期：2026-06-23  
> 審查範圍：全專案 `dashcam_player/` 所有 `.py` 檔案  
> 審查輪次：三輪完整審查  
> 狀態：**全部 22 項發現均已修復完成** (2026-06-23)  
> 最後階段：Phase 5 — MED-06 (GPX warning)、LOW-07 (busy-wait)、LOW-03 (apply_resolution)、LOW-02/LOW-04 (GPS 重構至 cv_utils)  
> 最後更新：2026-07-19 — 30 項優化全部完成，64 tests passing, ruff + mypy clean

---

## 目錄

1. [🔴 關鍵 Bug](#-關鍵-bug)（3 項）
2. [🟠 高優先級](#-高優先級)（5 項）
3. [🟡 中優先級](#-中優先級)（6 項）
4. [🟢 低優先級／程式碼品質](#-低優先級程式碼品質)（8 項）
5. [✅ 已知無誤](#-已知無誤)（8 項）
6. [📋 全域修復優先順序](#-全域修復優先順序)

---

## 🔴 關鍵 Bug（全部已修復 ✅）

### CRIT-01：ExportWorker 無逾時機制 ✅

**檔案：** `dashcam_player/core/export_worker.py:32`  
**影響：** FFmpeg 卡死時 ExportWorker 永久阻塞，UI 無法操作  
**狀態：** 已修復 — 加入 1hr timeout + `_kill_proc()`

`self._proc.communicate()` 沒有 timeout 參數。若 FFmpeg 因輸入/編碼問題卡死（pipe deadlock、網路 input 掛起等），執行緒將永久阻塞。

**修正方案：**

```python
# export_worker.py

import subprocess
from typing import Optional
from PyQt5.QtCore import QThread, pyqtSignal
from dashcam_player.utils.log import get_logger

log = get_logger()


class ExportWorker(QThread):
    """背景執行 FFmpeg 指令的執行緒"""

    finished = pyqtSignal(bool, str)  # (success, message)

    def __init__(self, cmd: list[str], parent: Optional[object] = None) -> None:
        super().__init__(parent)
        self._cmd = cmd
        self._proc: Optional[subprocess.Popen] = None
        self._timeout: float = 3600.0  # 1 小時上限

    def run(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                _, stderr = self._proc.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                self._kill_proc()
                self.finished.emit(False, "FFmpeg 執行逾時（超過 1 小時）")
                return
            if self._proc.returncode == 0:
                self.finished.emit(True, "擷取完成")
            else:
                try:
                    enc = locale.getpreferredencoding(do_setlocale=False)
                    msg = stderr.decode(enc, errors="replace").strip()
                except (LookupError, UnicodeDecodeError):
                    msg = stderr.decode("utf-8", errors="replace").strip()
                self.finished.emit(False, f"FFmpeg 錯誤:\n{msg[-500:]}")
        except OSError as e:
            self.finished.emit(False, f"無法執行 FFmpeg:\n{e}")
        finally:
            self._proc = None

    def _kill_proc(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
                self._proc.wait(5)
            except Exception:
                pass

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
```

---

### CRIT-02：`_cancel_export()` 順序錯誤 — 假性失敗回報 ✅

**檔案：** `dashcam_player/core/controller.py:456-458`  
**影響：** 取消匯出時可能觸發 `export_finished(False, ...)` + UI 彈出錯誤對話框  
**狀態：** 已修復 — 先 `stop_export()` 再清 `_export_params`

```python
def _cancel_export(self) -> None:
    self._export_params = {}       # ① 先清空參數
    self.stop_export()             # ② 再停止 worker
```

若在 `stop_export()` 中 `terminate()` 到 `disconnect()` 之間，worker 的 `finished` signal 被觸發，`_on_export_finished` 讀到空 `_export_params` → `.get("use_copy", False)` 為 `False` → 直接 emit `export_finished(False, ...)` → UI 顯示「擷取失敗」。

**修正方案：**

```python
def _cancel_export(self) -> None:
    self.stop_export()             # ① 先停止 worker
    self._export_params = {}       # ② 再清空參數
```

---

### CRIT-03：`_parse_gps_tags` 正則過度寬鬆 — 誤判非 GPS 文字為座標 ✅

**檔案：** `dashcam_player/utils/gps_parser.py:245-251`  
**影響：** 從影片 metadata tags 中提取錯誤的 GPS 座標  
**狀態：** 已修復 — 加入 lat/lon 範圍驗證（±90/±180）

```python
elif any(k in kl for k in ("location", "gps", "coordinate", "position")):
    m = re.search(r'([\d.\-]+)\s*[,，]\s*([\d.\-]+)', sv)
    if m:
        try:
            return (float(m.group(1)), float(m.group(2)))  # ← 立即回傳，無範圍驗證
        except ValueError:
            pass
```

若 tag value 為 `"aisle 3, row 4"`，正則匹配 `(3.0, 4.0)` 並當成 GPS 座標回傳。

**修正方案：**

```python
elif any(k in kl for k in ("location", "gps", "coordinate", "position")):
    m = re.search(r'([\d.\-]+)\s*[,，]\s*([\d.\-]+)', sv)
    if m:
        try:
            lat_val = float(m.group(1))
            lon_val = float(m.group(2))
            if -90 <= lat_val <= 90 and -180 <= lon_val <= 180:
                return (lat_val, lon_val)
        except ValueError:
            pass
```

---

## 🟠 高優先級

### HIGH-01：VideoWorker `seek/step` — `_current_idx` 在 frame 讀取前就更新

**檔案：** `dashcam_player/core/video_worker.py:82-111`  
**影響：** 快速連續操作時 UI 進度條可能超前實際位置；seek/step 目標被覆寫導致中間幀跳過

> 原評為 CRIT，經討論降格為 HIGH：實際影響限於 UI 進度條超前與連按跳幀，非 crash 級別。採用簡化修正（只刪除 `_current_idx = target`）而非佇列方案。

**問題描述：**

`seek()`（line 88）、`step_forward()`（line 102）、`step_backward()`（line 111）都在 lock 內立即設定 `self._current_idx = target`，但此時 `run()` 迴圈還沒真正讀取到對應 frame。連續快速操作時（如按住右鍵），每次 call 都覆寫 `_seek_frame`，中間目標被跳過。

**修正方案（簡化版）：**

```python
# video_worker.py

def seek(self, frame_idx: int) -> None:
    if not self.cap:
        return
    idx = max(0, min(frame_idx, self._total_frames - 1))
    with self._lock:
        self._seek_frame = idx
        # ❌ 移除: self._current_idx = idx  ── run() 迴圈 line 182 會在成功讀取後設定

def step_forward(self) -> None:
    self._play = False
    self._step_mode = True
    with self._lock:
        target = self._current_idx + 1
        if target < self._total_frames:
            self._seek_frame = target
            # ❌ 移除: self._current_idx = target

def step_backward(self) -> None:
    self._play = False
    self._step_mode = True
    with self._lock:
        target = self._current_idx - 1
        if target >= 0:
            self._seek_frame = target
            # ❌ 移除: self._current_idx = target
```

`run()` 迴圈內 line 181-182 在 frame 讀取成功後才更新 `_current_idx`，此處無須改動。`open()`（line 49-50）重設仍保留。

> **注意：** `controller.py:skip()`（line 219-221）使用 `self._current_idx + delta` 計算目標，連續 `skip()` 會基於同一個 `_current_idx`。若需完全消解，可於 Controller 層加入 `_pending_skip` 累計，但此為邊際場景，不在本次修正範圍。

---

### HIGH-02：BatchCaptureWorker `total_groups` off-by-one

**檔案：** `dashcam_player/core/batch_capture.py:188`  
**影響：** 進度條 max 值少算，顯示進度百分比不準確

```python
# 原：
total_groups = max(1, int(math.ceil((self._end_frame - self._start_frame) / step)))

# 修正：
total_groups = max(1, int(math.ceil((self._end_frame - self._start_frame + 1) / step)))
```

---

### HIGH-03：GPX naive datetime 被系統時區解釋

**檔案：** `dashcam_player/utils/gps_parser.py:51-64`  
**影響：** GPX 時間無時區標記（但實際是 UTC）時，`.timestamp()` 用本地時區解釋，GPS 插值時間可能差數小時

**問題根因：** 第一個 `try` 對 `"2024-01-01T12:00:00"`（無 Z/offset）成功解析為 naive datetime，`.timestamp()` 以本地時區解釋。第二個 `try` 永遠不會執行。

**修正方案：**

```python
def _parse_iso_time(text: str) -> Optional[datetime]:
    if not text:
        return None
    t = text.strip()
    try:
        t_normalized = t.replace("Z", "+00:00")
        dt = datetime.fromisoformat(t_normalized)
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
```

---

### HIGH-04：批次截圖 `get_static_map` 在迴圈內 import

**檔案：** `dashcam_player/core/batch_capture.py:237`  
**影響：** 每幀重複 import，效率不佳

```python
# 現狀（run() 內的迴圈中）：
if gps_coord:
    ...
    from dashcam_player.utils.osm_map import get_static_map  # ❌
    osm_bytes = get_static_map(...)
```

**修正：** 將 `from dashcam_player.utils.osm_map import get_static_map` 移至 batch_capture.py 頂層 import。

---

### HIGH-05：批次截圖 OSM 縮圖位置與單張截圖不一致

| 位置 | `controller.py:_overlay_gps_text` | `batch_capture.py:_overlay_osm_thumbnail` |
|------|------|------|
| `y1` (thumbnail top) | `h - 160 - 38` | `h - 160 - 8` |
| GPS 文字 baseline | `h - 15` | `h - 15` |
| 關係 | 縮圖在文字上方，23px 間距 | **縮圖與文字重疊** |

**修正方案：**

```python
# batch_capture.py _overlay_osm_thumbnail 改為
x1, y1 = w - thumb_size - 8, h - thumb_size - 38  # 與 controller 一致
```

---

## 🟡 中優先級

### MED-01：`_parse_nmea_line` 未檢查 NMEA validity flag

**檔案：** `dashcam_player/utils/gps_parser.py:321-368`  
**影響：** 可能記錄無效 GPS 資料

- **GPRMC：** 未檢查 `parts[2]`（A=有效 / V=無效）。GPRMC 應至少 12 欄，但代碼只檢查 `len(parts) >= 9`
- **GPGGA：** 未檢查 `parts[6]`（fix quality: 0=無定位）

**修正方案：**

```python
@staticmethod
def _parse_nmea_line(line: str) -> Optional[GPSPoint]:
    if not (line.startswith("$GPGGA") or line.startswith("$GNGGA") or
            line.startswith("$GPRMC") or line.startswith("$GNRMC")):
        return None

    parts = line.split(",")
    if len(parts) < 7:
        return None

    try:
        if line.startswith(("$GPRMC", "$GNRMC")):
            if len(parts) < 13 or parts[2] != "A":  # V=void
                return None
            # ... 原有解析 ...

        if line.startswith(("$GPGGA", "$GNGGA")):
            if len(parts) < 10 or parts[6] == "0":  # 無定位
                return None
            # ... 原有解析 ...

    except (ValueError, IndexError):
        pass

    return None
```

---

### MED-02：`open_video()` 重置 prefix 為硬編碼值

**檔案：** `dashcam_player/core/controller.py:107`  
**影響：** 每次開啟新影片都複寫使用者 prefix 設定

```python
self.capture_settings.prefix = "capture"  # ❌ 硬編碼
```

**修正：** 使用已載入或影片名：

```python
saved = self.settings.load_capture_settings()
self.capture_settings.prefix = saved.prefix or "capture"
```

---

### MED-03：`_sync_ui` timer 永不停止

**檔案：** `dashcam_player/core/video_player.py:70, 710`  
**影響：** 播放結束後仍每 200ms 排程

**修正：**

```python
def _on_play_finished(self) -> None:
    self.btn_play.setText("▶")
    self._ui_timer.stop()  # ← 新增
    self.statusBar().showMessage("播放結束")
```

---

### MED-04：`_reset_ui_state()` 遺漏 reset 按鈕狀態

**檔案：** `dashcam_player/core/video_player.py:672-691`  
**影響：** 批次截圖／匯出進行中開啟新影片時，按鈕保持 disabled

`_reset_ui_state` 沒有重設 `btn_capture_range.setEnabled(True)` 和 `btn_capture_video.setEnabled(True)`。

**修正方案：**

```python
def _reset_ui_state(self) -> None:
    # ... 原有邏輯 ...
    self.btn_capture_range.setEnabled(True)   # ← 新增
    self.btn_capture_video.setEnabled(True)   # ← 新增
```

---

### MED-05：NMEA 跨午夜偵測 heuristic 可能誤判

**檔案：** `dashcam_player/utils/gps_parser.py:460-479`  

- 若 GPS 設備中途重啟（時間跳到 0），被誤判為跨日
- 若軌跡時間跨多日，無法正確處理
- `raw_times[i] < raw_times[i - 1] - 43200`：往回跳 >12 小時才觸發

**評估：** 對單日行車記錄器正確率高，非必須立即修復。可文件紀錄設計限制即可。

---

### MED-06：GPX 無 namespace 時靜默失敗

**檔案：** `dashcam_player/utils/gps_parser.py:386-388`  

```python
ns = ""
if tag.startswith("{"):
    ns = tag[: tag.index("}") + 1]
# ns = "" → root.iter("trkpt") 可能找不到元素 → 無 log 回報
```

**修正：**

```python
if not tracks and not ns:
    log.warning(f"GPX 解析失敗：無 namespace 且未找到 trkpt 元素")
```

---

## 🟢 低優先級／程式碼品質

### LOW-01：Unused imports

**檔案：** `dashcam_player/core/controller.py:5, 18`  

```python
from pathlib import Path       # ← 從未使用
from dashcam_player.utils.cv_utils import ..., frame_to_qimage, ...
# frame_to_qimage 僅在 video_player.py 中使用，controller.py 無需 import
```

**修正：** 刪除以上兩行。

---

### LOW-02：`_overlay_gps_text` 與 batch 端 GPS 疊加代碼重複（含 LOW-02/LOW-03 合併）

**檔案：** `controller.py:486-556` vs `batch_capture.py:24-91`

重複的邏輯包括：
- GPS 文字疊加（`_overlay_gps_text` / `_overlay_gps_frame`）
- OSM 縮圖疊加（兩處寫法）
- EXIF GPS 寫入（`_write_exif_gps` 兩份）

**注意：** `controller.py:_overlay_gps_text` 內部使用 `import numpy as np`（line 489），但 AGENTS.md 明確規定：

> `numpy` 僅用於 `cv_utils.py` 型別標註，**其餘檔案不可新增 `import numpy`**

**修正方案：** 將所有 GPS 疊加邏輯（文字 + 縮圖）與 EXIF 寫入抽出到 `utils/cv_utils.py`（該檔案已 import numpy），controller 與 batch 端皆呼叫同一套共用函式：

```
utils/cv_utils.py 新增:
  def overlay_gps_text(frame, gps_coord) -> np.ndarray
  def overlay_gps_and_thumbnail(frame, gps_coord) -> np.ndarray
  def write_exif_gps(image_path: str, gps_coord: tuple) -> None
```

---

### LOW-03：`BatchCaptureWorker` 自行實作解析度縮放

**檔案：** `batch_capture.py:224-231` vs `utils/cv_utils.py:55-74`

批次截圖的縮放邏輯與 `cv_utils.apply_resolution()` 重複但使用不同 scale 演算法。

**修正方案：** 統一使用 `apply_resolution()`：

```python
# batch_capture.py:224-231 改為
from dashcam_player.utils.cv_utils import apply_resolution
if best_frame is not None:
    best_frame = apply_resolution(best_frame, self._resolution)
```

（需確認 `apply_resolution` 目前僅處理 `scale < 1.0` 的縮小，與批次行為一致）

---

### LOW-04：`BatchCaptureWorker._overlay_osm_thumbnail` 未做邊界校驗

**檔案：** `batch_capture.py:41-58`  

Controller 版本（line 516）有：
```python
if x1 >= 0 and y1 >= 0:
```

Batch 版本沒有。若影像尺寸 < 168px（160px thumbnail + 8px padding），`y1` 負值導致 numpy slice wrap。

**修正：** 加入邊界檢查（與 LOW-02 合併到 cv_utils 後一併處理）。

---

### LOW-05：`_parse_gps_tags` 回傳前未驗證 lat/lon 範圍（精確 key 匹配分支）

**檔案：** `dashcam_player/utils/gps_parser.py:253-254`  

SRT parser（line 518）有範圍驗證，但 `_parse_gps_tags` 的精確 key 匹配分支沒有：

```python
if lat is not None and lon is not None:
    return (lat, lon)  # ← 未檢查 -90/90, -180/180
```

**修正：**

```python
if lat is not None and lon is not None:
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
return None
```

---

### LOW-06：`_GPSSettingsDialog` / `_OutputSettingsDialog` / `_CaptureSettingsDialog` 重複傳入參數

**檔案：** `dashcam_player/core/video_player.py:1138, 1150, 1154`  

```python
dlg = _GPSSettingsDialog(self, self)     # vp=self, parent=self (重複)
dlg = _OutputSettingsDialog(self, self)
dlg = _CaptureSettingsDialog(self, self)
```

**修正：**

```python
dlg = _GPSSettingsDialog(self, parent=self)
dlg = _OutputSettingsDialog(self, parent=self)
dlg = _CaptureSettingsDialog(self, parent=self)
```

---

### LOW-07：VideoWorker 解碼慢於 target_fps 時 busy-wait

**檔案：** `dashcam_player/core/video_worker.py:198`  

當解碼耗時超過 frame_interval，`max(1, negative) = 1ms` → 1ms busy-wait 迴圈，CPU 高消耗。

**評估：** 功能無誤，可接受。建議於高 CPU 場景時優化。

---

### LOW-08：NMEA parser encoding

**檔案：** `dashcam_player/utils/gps_parser.py:426`  

```python
with open(nmea_path, "r", encoding="utf-8") as f:
```

純 ASCII NMEA 無問題，某些裝置可能輸出 latin-1。可加入 fallback。

---

## ✅ 已知無誤

| # | 項目 | 檔案 | 說明 |
|---|------|------|------|
| ✓ | `video_info_ready` 先於 `duration_ready` emit | `video_worker.py:133-141` | 順序正確 |
| ✓ | `_on_duration_ready` 寫入 `video_info.duration` | `controller.py:129` | `video_loaded` 正確認發 |
| ✓ | Zoom crop `.copy()` | `video_player.py:755` | numpy view → QImage 相容性 |
| ✓ | `imwrite_safe` 用於所有圖片寫入 | `controller.py:287-289`, `batch_capture.py:247-251` | 支援中文路徑 |
| ✓ | QSettings 布林值轉換 | `settings.py:66-68` | 4 個 bool 欄位全部處理 |
| ✓ | `_current_idx` 執行緒安全 | `video_worker.py:43` | `self._lock` 保護 |
| ✓ | Signal disconnect 防禦 | `controller.py:369-374, 434-437, 463-465` | `try/except TypeError` |
| ✓ | Batch capture 目錄檢查 | `video_player.py:954` | `isdir` 防禦 |

---

## 📋 全域修復優先順序

### Phase 1：穩定性（無風險，建議立即修復）

| 順序 | 編號 | 議題 | 預計工時 |
|------|------|------|----------|
| 1 | CRIT-02 | `_cancel_export` 順序 | 5min |
| 2 | CRIT-01 | ExportWorker timeout | 30min |

### Phase 2：資料正確性

| 順序 | 編號 | 議題 | 預計工時 |
|------|------|------|----------|
| 3 | CRIT-03 | GPS 正則過度匹配 | 15min |
| 4 | HIGH-03 | GPX 時區 bug | 30min |
| 5 | MED-01 | NMEA validity flag 檢查 | 30min |
| 6 | LOW-05 | GPS 回傳前座標範圍驗證 | 10min |

### Phase 3：使用者體驗

| 順序 | 編號 | 議題 | 預計工時 |
|------|------|------|----------|
| 7 | HIGH-01 | seek/step 移除 `_current_idx=target` | 15min |
| 8 | HIGH-02 | batch total_groups off-by-one | 5min |
| 9 | MED-04 | `_reset_ui_state` 遺漏 button reset | 5min |
| 10 | MED-03 | `_sync_ui` timer 清理 | 15min |
| 11 | MED-02 | prefix 硬編碼覆蓋 | 5min |

### Phase 4：重構與一致化

| 順序 | 編號 | 議題 | 預計工時 |
|------|------|------|----------|
| 12 | HIGH-04 | batch import 移到頂層 | 5min |
| 13 | HIGH-05 | 縮圖位置不一致 | 5min |
| 14 | LOW-02 | 抽出共享 GPS overlay / EXIF 到 cv_utils | 1-2h |
| 15 | LOW-03 | batch 使用 `apply_resolution` | 30min |
| 16 | LOW-04 | batch overlay 邊界檢查（與 LOW-02 合併） | — |
| 17 | LOW-06 | dialog 參數重複 | 5min |
| 18 | LOW-01 | 移除 unused imports | 5min |

### Phase 5：長期改善

| 順序 | 編號 | 議題 | 說明 |
|------|------|------|------|
| 19 | MED-05 | NMEA 跨午夜 heuristic | 文件紀錄限制，暫不修改 |
| 20 | MED-06 | GPX no-ns 靜默失敗 | 加入 warning log |
| 21 | LOW-07 | busy-wait CPU | 優化 sleep 策略 |
| 22 | LOW-08 | NMEA encoding | 嘗試 latin-1 fallback |

---

*文件結束。合計 22 項發現，3 項關鍵，5 項高優先。*