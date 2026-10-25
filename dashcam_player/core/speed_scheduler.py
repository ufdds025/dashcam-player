"""SpeedScheduler - 背景掃描排程與 CPU 預算控制

依播放狀態與 CPU 壓力動態調整掃描資源
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from dashcam_player.utils.log import get_logger

log = get_logger()


class SpeedScheduler(QObject):
    """速度掃描排程器

    負責：
    1. 依播放狀態動態調整 SpeedWorker 優先序與 CPU 預算
    2. 批次截圖時暫緩/降載
    3. CPU 壓力回饋自適應降採樣
    """

    # 信號
    budget_changed = pyqtSignal(float)  # 目前預算比例 (0.0-1.0)

    def __init__(self, parent: Any | None = None) -> None:
        super().__init__(parent)
        self._worker = None
        self._playback_active = False
        self._batch_active = False
        self._cpu_pressure = 0.0

        # CPU 監控
        self._cpu_timer = QTimer(self)
        self._cpu_timer.setInterval(1000)  # 每秒檢查一次
        self._cpu_timer.timeout.connect(self._check_cpu)
        self._cpu_timer.start()

        # 內部狀態
        self._current_budget = 1.0  # 1.0 = 全速

    def set_worker(self, worker: Any) -> None:
        """設定要管理的 SpeedWorker"""
        self._worker = worker

    def set_playback_active(self, active: bool) -> None:
        """設定播放狀態"""
        self._playback_active = active
        self._recalculate_budget()

    def set_batch_active(self, active: bool) -> None:
        """設定批次截圖進行中"""
        self._batch_active = active
        self._recalculate_budget()

    def _check_cpu(self) -> None:
        """檢查 CPU 使用率 (簡易版)"""
        # 這裡簡單實作：實際可用 psutil 或 platform 特定 API
        try:
            import psutil
            self._cpu_pressure = psutil.cpu_percent(interval=None) / 100.0
        except ImportError:
            # 無 psutil 時用簡單啟發式：若有播放+批次同時進行，假設高壓
            if self._playback_active and self._batch_active:
                self._cpu_pressure = 0.85
            elif self._playback_active:
                self._cpu_pressure = 0.4
            else:
                self._cpu_pressure = 0.1

        self._recalculate_budget()

    def _recalculate_budget(self) -> None:
        """依當前狀態重新計算 CPU 預算"""
        # 基礎預算
        budget = 1.0

        # 播放中降載
        if self._playback_active:
            budget *= 0.5

        # 批次進行中大幅降載
        if self._batch_active:
            budget *= 0.2

        # CPU 壓力回饋
        if self._cpu_pressure > 0.9:
            budget *= 0.1
        elif self._cpu_pressure > 0.8:
            budget *= 0.3
        elif self._cpu_pressure > 0.6:
            budget *= 0.6

        # 限制範圍
        self._current_budget = max(0.05, min(1.0, budget))

        # 套用到 worker (若有) — 透過公開方法避免直接改私有屬性
        if self._worker and hasattr(self._worker, 'set_sample_budget'):
            try:
                self._worker.set_sample_budget(self._current_budget)  # type: ignore[attr-defined]
            except Exception:
                pass
        elif self._worker and hasattr(self._worker, '_target_samples_per_cycle'):
            self._worker._target_samples_per_cycle = max(3, int(6 * self._current_budget))

        # 發送變更通知
        self.budget_changed.emit(self._current_budget)

    def get_budget(self) -> float:
        return self._current_budget

    def get_status(self) -> dict:
        return {
            "budget": self._current_budget,
            "playback_active": self._playback_active,
            "batch_active": self._batch_active,
            "cpu_pressure": self._cpu_pressure,
        }
