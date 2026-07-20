# 規劃：影片重新編碼選項

## 動機
取消「使用原始編碼（stream copy）」時，目前僅硬編碼 `-c:v libx264 -crf 18 -preset medium`，無法壓縮檔案大小或調整解析度以符合上傳限制。

## 修改範圍

| 檔案 | 變更 |
|------|------|
| `models/capture_settings.py` | +4 欄位 |
| `utils/ffmpeg.py` | `build_export_cmd` 接收新參數 |
| `core/controller.py` | `start_video_export` 傳遞新參數 |
| `core/video_player.py` | 正常面板 + 對話框新增 UI |

## Step 1 — CaptureSettings 新增欄位

```python
video_codec: str = "libx264"          # libx264 / libx265
video_crf: int = 18                   # 0-51
video_preset: str = "medium"          # ultrafast~veryslow
video_scale_width: str = "原始"       # 原始 / 1920 / 1280 / 854 / 640
```

`to_dict()` 與 `from_dict()` 更新（from_dict 使用 `**{k:v for k,v in ... if k in cls.__dataclass_fields__}` 已自動相容新欄位）。

## Step 2 — ffmpeg.py / build_export_cmd

**新參數**：`codec="libx264"`, `crf=18`, `preset="medium"`, `scale_width="原始"`

**use_copy=True**（不變）：
```
-c:v copy
```

**use_copy=False**（新）：
```
-c:v {codec} -crf {crf} -preset {preset}
```
若 `scale_width != "原始"`，追加：
```
-vf "scale={scale_width}:-2"
```
`-2` 自動計算高度並強制偶數，維持比例不變形。

## Step 3 — controller.py / start_video_export

`_export_params` dict 新增：
```python
codec=settings.video_codec,
crf=settings.video_crf,
preset=settings.video_preset,
scale_width=settings.video_scale_width,
```

stream copy 失敗自動重試時（`_on_export_finished`），這些參數已存在 `_export_params`，不需額外處理。

## Step 4 — video_player.py 正常面板

在「影片擷取設定」QGroupBox 中，`copy_codec_check` 下方插入：

```
[編碼格式: libx264 ▼]   [CRF 品質: 18 ▼]   [編碼預設: medium ▼]
[縮放寬度: 原始 ▼]
```

**show/hide 連動**：
- `copy_codec_check` 勾選 → hide 四項
- `copy_codec_check` 取消 → show 四項
- 欄位值存入 `self.settings.video_codec/ crf/ preset/ scale_width`

## Step 5 — video_player.py / _CaptureSettingsDialog

對話框內新增同樣四項 + show/hide 連動。
`_accept()` 同步回 `self.vp.settings` 及正常面板對應 widget。

## 審查確認（已完成）

- Signal 順序不受影響（不改 video_worker）
- 執行緒安全不受影響（不改 video_worker）
- stream copy 重試時新參數自然帶入
- `scale=W:-2` 保證等比 + 偶數高度
- `to_dict/from_dict` 相容既有測試
- 預覽模式工具列不受影響（編碼設定在對話框內）
