"""subprocess 工具函數"""

from __future__ import annotations

import subprocess
import sys


def run_no_window(
    cmd: list[str],
    *,
    timeout: int = 15,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """在 Windows 上隱藏子程序控制台視窗（``CREATE_NO_WINDOW``）。

    五個現有呼叫全部使用 ``capture_output=True``，因此預設值為 True。
    若未來需要 ``capture_output=False``（例如 FFmpeg 匯出需要 streaming
    output），此 helper 不適用，請直接使用 ``subprocess.run``。
    """
    kwargs: dict = {
        "capture_output": capture_output,
        "timeout": timeout,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, **kwargs)  # noqa: S603
