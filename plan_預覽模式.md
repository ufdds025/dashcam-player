# 規劃：雙擊影片放大預覽模式 + 暫停縮放

## 動機
播放時畫面太小，無法確認細節，設定擷取起始/結束可能出錯。需要一個「預覽模式」讓影片佔據絕大多數畫面，底部只保留必要控制項。同時在暫停時可用滾輪放大畫面檢視細節。

## 最終實作摘要

### 預覽模式（雙擊切換）
- `video_label.installEventFilter` → `eventFilter` 攔截 `MouseButtonDblClick`
- 底部使用 show/hide 切換（非 QStackedWidget）：
  - `_full_bottom.setVisible(False)` → 完整面板脫離佈局，`video_label` 自然擴張
  - `_preview_bar.setVisible(True)` → 精簡工具列取代
- 工具列無區塊分隔，連續排列：
  ```
  [⏮][⏪][▶][⏩][⏭] [1.0x] [標記開始][標記結束][清除標記]
  開始:--|結束:--  time/frame  ...[stretch]... [輸出設定][擷取設定][截取單張][批次截取][擷取影片]
  ```

### 暫停縮放（滾輪）
- `eventFilter` 攔截 `QEvent.Wheel` + `not self.ctrl.is_playing`
- `_get_image_relative_pos()`：label 座標 → 影像相對座標 [0,1]，含黑邊偏移補償
- `_get_zoom_crop_rect()`：根據縮放中心計算 crop 範圍，自動 clamp 至影像邊界
- 範圍 1.0x ~ 5.0x，步進 0.2
- numpy slice 後 `.copy()` 確保連續記憶體（避免 QImage TypeError）
- 開始播放 / 開啟新影片時自動重置為 1.0x
- 視窗縮放時自動更新畫面

### 已實作的 .md
- `plan_編碼選項.md`：影片重新編碼選項
- `plan_預覽模式.md`：本檔案
- `AGENTS.md`：專案架構與注意事項（已包含縮放描述）
