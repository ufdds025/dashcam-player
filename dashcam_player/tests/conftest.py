from __future__ import annotations

import queue

import numpy as np
import pytest
from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import QApplication

# 設定 Qt.AA_ShareOpenGLContexts 必須在 QCoreApplication/QApplication 建立前
# 這是為了支援 QtWebEngine（MapView 使用），否則 import 會失敗
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from dashcam_player.core.video_worker import VideoWorker


@pytest.fixture(autouse=True)
def isolated_qsettings(tmp_path, monkeypatch):
    """將 AppSettings 的 QSettings 導離真實登錄檔，隔離每個測試的設定環境。

    ⚠️ QSettings(org, app) 兩參建構子在 Windows 永遠使用 NativeFormat（登錄檔），
    setDefaultFormat()/setPath() 對該建構子無效。因此以子類強制 IniFormat
    並替換 config.settings 模組內的 QSettings 名稱，INI 檔落在測試暫存目錄。
    """
    from dashcam_player.config import settings as settings_mod

    ini_root = tmp_path / "qsettings"
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(ini_root))

    class _TestQSettings(QSettings):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(
                QSettings.IniFormat, QSettings.UserScope, *args, **kwargs
            )

    monkeypatch.setattr(settings_mod, "QSettings", _TestQSettings)
    yield


@pytest.fixture
def video_worker() -> VideoWorker:
    """Pre-configured VideoWorker with a fresh command queue."""
    w = VideoWorker()
    w._total_frames = 100
    w._fps = 30.0
    w._current_idx = 10
    w._cmd_queue = queue.Queue()
    return w


@pytest.fixture
def frame_1080p() -> np.ndarray:
    return np.zeros((1080, 1920, 3), dtype=np.uint8)


@pytest.fixture
def frame_480p() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


@pytest.fixture
def frame_small() -> np.ndarray:
    return np.zeros((100, 100, 3), dtype=np.uint8)
