"""幀級標定驗收 CLI（Task C 指標，純量測不改檔）。核心邏輯見 utils/lane_events。

用法（專案根目錄）:
    python scripts/eval_cross.py
    python scripts/eval_cross.py PIC20260826-092148-F_annotations.json
    python scripts/eval_cross.py --tol 2.0 --window 650 800 PIC20260826-091548-F_annotations.json
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

from dashcam_player.utils.lane_events import TASK_C_RECALL, TASK_C_TOL, evaluate_file  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Task C 幀級驗收：auto-bw vs 人工 cross")
    ap.add_argument("files", nargs="*", default=["*_annotations.json"])
    ap.add_argument("--tol", type=float, default=TASK_C_TOL)
    ap.add_argument("--window", type=float, nargs=2, default=None, metavar=("W0", "W1"))
    args = ap.parse_args()

    paths: list[str] = []
    for pat in args.files:
        paths.extend(glob.glob(pat))
    paths = sorted(set(paths))
    if not paths:
        print("找不到標定檔")
        return 1

    w0, w1 = (args.window[0], args.window[1]) if args.window else (0.0, float("inf"))
    all_ok = True
    for p in paths:
        r = evaluate_file(p, tol=args.tol, w0=w0, w1=w1)
        flag = "PASS" if r["pass_task_c"] else "FAIL"
        all_ok = all_ok and r["pass_task_c"]
        print(f"=== {r['file']} tol={r['tol']} R={r['recall']:.2f} P={r['precision']:.2f} [{flag}]")
        for side in ("left", "right"):
            s = r["per_side"][side]
            b = s["bias"]
            print(
                f"  {side}: truth={s['truth']} auto={s['auto']} hit={s['hits']} "
                f"R={s['recall']:.2f} P={s['precision']:.2f} "
                f"bias={b['median']:+.2f}f p10={b['p10']:+.2f} p90={b['p90']:+.2f}"
            )
    print(f"Task C (tol={args.tol}, recall>={TASK_C_RECALL}): {'ALL PASS' if all_ok else 'NOT YET'}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
