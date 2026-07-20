from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
from logging.handlers import RotatingFileHandler

_LOG_PATH: str = ""
_log_lock = threading.Lock()


def init_log() -> None:
    global _LOG_PATH

    logger = logging.getLogger("dashcam")
    if logger.handlers:
        return

    _LOG_PATH = os.path.join(tempfile.gettempdir(), "dashcam_debug.log")
    logger.setLevel(logging.DEBUG)

    # File handler (UTF-8 with rotation, 1 MB max)
    fh = RotatingFileHandler(_LOG_PATH, maxBytes=1_048_576, backupCount=2, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    logger.addHandler(fh)

    # Console handler (only WARNING+)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    logger.info(f"=== 行車紀錄器啟動 ===  PID={os.getpid()}")


def get_logger() -> logging.Logger:
    return logging.getLogger("dashcam")


def get_log_path() -> str:
    return _LOG_PATH
