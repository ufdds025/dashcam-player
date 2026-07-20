# EXE 封裝計畫 — DashcamPlayer

## 目標

將 `dashcam_player` 打包成可獨立執行的 Windows `.exe`，使用者在目標電腦不需安裝 Python、PyQt5、OpenCV 等任何依賴。

## 關鍵限制

- **FFmpeg 是外部程式**，不在 Python 套件管理範圍內，需手動攜帶
- **PyQt5 + OpenCV 合計約 150–250 MB**（uncompressed），是檔案大小的主要來源
- **QSettings 使用 Windows Registry**，打包後正常運作，無需特殊處理
- **日誌使用 `tempfile` 系統目錄**，無寫入限制

---

## 工具選擇：PyInstaller

唯一成熟方案，支援 PyQt5 + OpenCV + NumPy 組合。備選（Nuitka / cx_Freeze）考量：

| 工具 | 優點 | 缺點 |
|------|------|------|
| **PyInstaller** | 社群最大，坑最少，hook 完整 | 打包後略大 |
| Nuitka | 執行速度較快 | 編譯時間長，OpenCV 易出錯 |
| cx_Freeze | 檔案較小 | setup.py 配置複雜，維護少 |

**結論：PyInstaller。**

---

## 檔案結構規劃

```
DashcamPlayer/
├── DashcamPlayer.exe       # 主程式（PyInstaller onefile）
│   └── 內部包含：
│       ├── dashcam_player/  # Python bytecode
│       ├── PyQt5 DLLs
│       ├── opencv DLLs
│       └── numpy DLLs
├── ffmpeg.exe              # 手動放置（~100 MB，含常用 codec）
├── ffprobe.exe             # 手動放置（~50 KB）
└── 執行.bat                 # [可選] 確保工作目錄正確的 launcher
```

或改用 `onedir`（打包後為資料夾，啟動快）：

```
DashcamPlayer/
├── DashcamPlayer/          # 資料夾模式
│   ├── DashcamPlayer.exe
│   ├── *.dll / *.pyd
│   └── ...
├── ffmpeg.exe
├── ffprobe.exe
└── ...
```

---

## 預估產物大小

| 項目 | 壓縮後（約） | 說明 |
|------|-------------|------|
| Python + PyQt5 DLL | 40–70 MB | QtCore / QtGui / QtWidgets |
| OpenCV + numpy DLL | 50–80 MB | `cv2` 含大量 codec module |
| `dashcam_player` code | < 1 MB | 純 Python，極小 |
| **onefile 單檔** | **90–150 MB** | UPX 壓縮可再降 20–30% |
| **onedir 資料夾** | **150–250 MB** | 含完整 DLL，無壓縮 |

---

## 實作步驟

### Step 1 — 建立平坦入口

根目錄新增 `main.py`，讓 PyInstaller 能找到所有 import chain：

```python
"""PyInstaller hook — 委託給 dashcam_player.main"""
from dashcam_player.main import main
main()
```

### Step 2 — 下載 FFmpeg

從 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip) 下載 essentials build。
只取 `ffmpeg.exe` + `ffprobe.exe`，放進專案根目錄。

### Step 3 — 撰寫 PyInstaller 腳本

`build_exe.bat`（onefile 版本，初次先用看看）：

```bat
@echo off
pip install pyinstaller

pyinstaller --onefile --windowed ^
  --name "DashcamPlayer" ^
  --icon icon.ico ^
  --add-data "ffmpeg.exe;." ^
  --add-data "ffprobe.exe;." ^
  --collect-submodules dashcam_player ^
  --hidden-import PyQt5.QtCore ^
  --hidden-import PyQt5.QtGui ^
  --hidden-import PyQt5.QtWidgets ^
  --hidden-import cv2 ^
  --hidden-import numpy ^
  main.py

echo.
echo 完成！產物在 dist/DashcamPlayer.exe
pause
```

### Step 4 — 測試

1. 在**全新虛擬機或無 Python 環境**測試執行
2. 測試功能：開啟各種格式影片、單張截圖、批次截圖、影片匯出（含 FFmpeg stream copy）
3. 測試日誌：確認 `%TEMP%\dashcam_debug.log` 正常寫入

### Step 5 — 可選瘦身手段

| 手段 | 節省 | 副作用 |
|------|------|--------|
| UPX 壓縮 (`--upx-dir`) | 20–30% | 啟動稍慢，防毒誤報風險略增 |
| 移除 OpenCV 非必要 module (`opencv_*.dll`) | 30–50 MB | 複雜，需逐個測試 |
| 改用 `opencv-python-headless` | ~10 MB | 不影響（此專案無 GUI 按鈕元件） |
| 改用 `onedir` + 7z SFX 打包 | 變資料夾 | 啟動快，體積接近 |

---

## 風險與對策

| 風險 | 機率 | 對策 |
|------|------|------|
| OpenCV DLL 缺漏（`MSVCP*.dll`） | 中 | `--collect-all cv2` 或手動複製 VC Redist |
| FFmpeg 路徑找不到 | 高 | `find_ffmpeg()` 已用 `shutil.which` + exe 同目錄可用 |
| PyQt5 platform plugin 缺失（「This application failed to start...」） | 中 | `--collect-all PyQt5` 或加 `--add-data "path\to\platforms;."` |
| 防毒誤報（PyInstaller 特有） | 中 | 改用 `onedir` 降低誤報率 |
| Unicode 路徑（中文資料夾） | 低 | 測試過沒問題，Python 3.11 原生支援 |

---

## 什麼時候做

- [ ] 所有核心功能穩定
- [ ] 確認本機 Python 環境可正常執行 `python -m dashcam_player.main`
- [ ] 下載 FFmpeg essentials ZIP
- [ ] 執行 `build_exe.bat`
- [ ] 在無 Python 環境驗證
- [ ] 決定 onefile / onedir / 其他封裝形式

---

## 附：`.spec` 檔參考

如果用 `pyi-makespec` 產生，核心 spec 可簡化為：

```python
# DashcamPlayer.spec
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('ffmpeg.exe', '.'), ('ffprobe.exe', '.')],
    hiddenimports=['PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets', 'cv2', 'numpy'],
    hookspath=[],
    hooksconfig={},
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name='DashcamPlayer', debug=False, strip=False, upx=True, console=False)
```

若改用 `onedir`：`EXE` → `COLLECT`。
