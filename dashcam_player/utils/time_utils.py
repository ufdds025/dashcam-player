from __future__ import annotations


def format_time(seconds: float) -> str:
    if seconds < 0:
        return "00:00:00"
    total_sec = max(0, int(seconds))
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_time_input(text: str) -> float | None:
    parts = text.replace("\uff1a", ":").split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
            return h * 3600 + m * 60 + s
        elif len(parts) == 2:
            m, s = int(parts[0]), float(parts[1])
            return m * 60 + s
        elif len(parts) == 1:
            return float(parts[0])
    except (ValueError, IndexError):
        return None
    return None
