from __future__ import annotations

import locale
import subprocess

from PyQt5.QtCore import QThread, pyqtSignal

from dashcam_player.utils.log import get_logger

log = get_logger()


class ExportWorker(QThread):
    """背景執行 FFmpeg 指令的執行緒"""

    # ⚠️ 不可命名為 finished——會遮蔽 QThread 內建信號，執行緒崩潰時完全靜默
    result_ready = pyqtSignal(bool, str)  # (success, message)

    TIMEOUT_SEC: float = 3600.0  # 1 小時上限，避免 FFmpeg 卡死時執行緒永久阻塞

    def __init__(self, cmd: list[str], parent: object | None = None) -> None:
        super().__init__(parent)
        self._cmd = cmd
        self._proc: subprocess.Popen | None = None

    def run(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                _, stderr = self._proc.communicate(timeout=self.TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                self._kill_proc()
                # 排空子行程的 stdout/stderr pipe，避免 pipe 滿載導致 zombie
                try:
                    self._proc.communicate()
                except Exception:
                    pass
                self.result_ready.emit(False, "FFmpeg 執行逾時（超過 1 小時）")
                return
            if self._proc.returncode == 0:
                self.result_ready.emit(True, "擷取完成")
            else:
                try:
                    enc = locale.getpreferredencoding(do_setlocale=False)
                    msg = stderr.decode(enc, errors="replace").strip()
                except (LookupError, UnicodeDecodeError):
                    msg = stderr.decode("utf-8", errors="replace").strip()
                self.result_ready.emit(False, f"FFmpeg 錯誤:\n{msg[-500:]}")
        except OSError as e:
            self.result_ready.emit(False, f"無法執行 FFmpeg:\n{e}")
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
                self._kill_proc()
