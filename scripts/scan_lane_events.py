"""車道事件唯讀掃描 CLI：變道過渡 / 單側實線 / 標記中斷候選。

只讀影片＋標定檔，不寫入任何來源檔；報告輸出至 stdout 或 --output。
先在已標定 5 支上驗證規則（--validate），precision/recall ≥ 0.80 才掃 D 槽。

用法（專案根目錄）:
    python scripts/scan_lane_events.py --validate
    python scripts/scan_lane_events.py "D:\\norm\\*.MP4" --output lane_scan_norm.md
    python scripts/scan_lane_events.py PIC20260825-180655-F.AVI --stride 3
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

from dashcam_player.utils.cv_utils import open_video_capture  # noqa: E402
from dashcam_player.utils.lane_events import (  # noqa: E402
    compute_occupancy_series,
    detect_lane_events,
    evaluate_file,
    find_project_annotations,
    load_events,
    render_markdown_report,
    resolve_box,
    truth_events_from_cross,
    validate_events,
)
from dashcam_player.utils.pts_loader import get_reliable_fps_count  # noqa: E402

VALIDATE_PRECISION_MIN = 0.80
VALIDATE_RECALL_MIN = 0.80


def scan_video(video_path: str, ann_map: dict[str, str], stride: int) -> str:
    """掃單支影片，回傳 markdown 段落。"""
    stem = os.path.splitext(os.path.basename(video_path))[0].lower()
    ann_path = ann_map.get(stem)
    box, source = resolve_box(video_path, ann_path)
    frames, occ_l, occ_r = compute_occupancy_series(video_path, box, stride=stride)
    events = detect_lane_events(frames, occ_l, occ_r, stride=stride)

    cap = open_video_capture(video_path)
    try:
        total = int(cap.get(4)) if cap.isOpened() else (frames[-1] + 1 if frames else 0)
    finally:
        cap.release()
    reliable = get_reliable_fps_count(video_path)
    fps = float(reliable[0]) if reliable else 30.0
    if reliable:
        total = int(reliable[1])

    task_c = evaluate_file(ann_path) if ann_path else None
    return render_markdown_report(video_path, fps, total, source, box, events, task_c)


def run_validate(stride: int) -> int:
    """在專案 5 支已標定片上驗證掃描規則。"""
    ann_map = find_project_annotations(os.getcwd())
    videos = sorted(glob.glob("PIC*.AVI") + glob.glob("CH13*.avi"))
    if not videos:
        print("專案根目錄找不到測試影片")
        return 1
    ok = True
    for v in videos:
        stem = os.path.splitext(os.path.basename(v))[0].lower()
        ann_path = ann_map.get(stem)
        if not ann_path:
            print(f"--- {v}：無標定檔，跳過")
            continue
        box, _ = resolve_box(v, ann_path)
        frames, occ_l, occ_r = compute_occupancy_series(v, box, stride=stride)
        detected = detect_lane_events(frames, occ_l, occ_r, stride=stride)
        cross, _ = load_events(ann_path)
        total = frames[-1] + 1 if frames else 0
        truth = truth_events_from_cross(cross, total)
        m = validate_events(detected, truth)
        passed = m["precision"] >= VALIDATE_PRECISION_MIN and m["recall"] >= VALIDATE_RECALL_MIN
        ok = ok and passed
        print(
            f"{os.path.basename(v)}: truth={m['truth']} det={m['detected']} "
            f"hit={m['hits']} P={m['precision']:.2f} R={m['recall']:.2f} "
            f"[{'PASS' if passed else 'FAIL'}]"
        )
        for e in detected:
            print(f"    det f{e['start']}-{e['end']} {e['type']}: {e['detail']}")
        for t in truth:
            print(f"    tru f{t['start']}-{t['end']} {t['type']}")
    print("SCAN-RULES: " + ("ALL PASS" if ok else "NOT YET"))
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="車道事件唯讀掃描（變道/實線/中斷）")
    ap.add_argument("patterns", nargs="*", default=["PIC*.AVI"])
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--output", default="")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    if args.validate:
        return run_validate(args.stride)

    videos: list[str] = []
    for pat in args.patterns:
        videos.extend(glob.glob(pat))
    videos = sorted(set(videos))
    if not videos:
        print("找不到影片")
        return 1

    ann_map = find_project_annotations(os.getcwd())
    parts = ["# 車道事件掃描報告（唯讀產出，人工讀後再用標記軟體處理）", ""]
    for v in videos:
        print(f"掃描 {v} ...", file=sys.stderr)
        try:
            parts.append(scan_video(v, ann_map, args.stride))
        except Exception as e:
            parts.append(f"## {os.path.basename(v)}\n\n掃描失敗：{e}\n")
    report = "\n".join(parts)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"報告已輸出 -> {args.output}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
