"""掃描報告摘要索引：為 lane_scan_*.md  prepend 事件總表＋人工優先順序。

用法：
    python scripts/summarize_scan.py H:/TEMP/opencode/lane_scan_norm.md
    （直接改寫該檔，在標題後插入總表；冪等，重跑會取代舊總表）
"""

from __future__ import annotations

import os
import re
import sys

TITLE = "# 車道事件掃描報告"
BEGIN = "<!-- SUMMARY-BEGIN -->"
END = "<!-- SUMMARY-END -->"

PRIORITY = {"transition": 0, "single_left": 1, "single_right": 1, "solid_left": 2,
            "solid_right": 2, "gap_both": 1}


def parse_report(text: str) -> list[dict]:
    videos: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = re.match(r"## (.+)$", line)
        if m and m.group(1) != "人工處理優先順序總表":
            if cur is not None:
                videos.append(cur)
            cur = {"name": m.group(1), "events": []}
            continue
        m = re.match(r"\| f(\d+)–f(\d+) \| .* \| ([a-z_]+) \|", line)
        if m and cur is not None:
            cur["events"].append({"start": int(m.group(1)), "end": int(m.group(2)),
                                  "type": m.group(3)})
    if cur is not None:
        videos.append(cur)
    return videos


def build_summary(videos: list[dict]) -> str:
    rows = []
    for v in videos:
        evs = v["events"]
        if not evs:
            rows.append((3, v["name"], "—", "雙側正常，免處理", "-"))
        else:
            kinds: dict[str, int] = {}
            for e in evs:
                kinds[e["type"]] = kinds.get(e["type"], 0) + 1
            pri = min(PRIORITY.get(k, 1) for k in kinds)
            desc = "＋".join(f"{k}x{n}" for k, n in sorted(kinds.items()))
            first = min(e["start"] for e in evs if "transition" in e["type"] or "single" in e["type"]) \
                if any("transition" in e["type"] or "single" in e["type"] for e in evs) else evs[0]["start"]
            act = "先看 transition/single 段" if pri <= 1 else "實線註記即可"
            rows.append((pri, v["name"], desc, act, f"f{first}"))
    rows.sort()
    out = [BEGIN, "", "## 人工處理優先順序總表",
           "", "| 優先 | 影片 | 事件 | 動作 | 首個重點幀 |",
           "|---|---|---|---|---|"]
    for pri, name, desc, act, first in rows:
        out.append(f"| P{pri} | {name} | {desc} | {act} | {first} |")
    out += ["",
            "P0=變道過渡/單側中斷（先處理）; P1=缺口待裁定；P2=純實線段（註記起止即可）; "
            "—=全片正常。",
            "", END]
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python scripts/summarize_scan.py <lane_scan_*.md>")
        return 1
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    videos = parse_report(text)
    summary = build_summary(videos)
    if BEGIN in text:
        text = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), summary,
                      text, flags=re.DOTALL)
    else:
        text = text.replace(TITLE, TITLE + "\n\n" + summary, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    n_ev = sum(1 for v in videos if v["events"])
    print(f"{os.path.basename(path)}: {len(videos)} 支影片，{n_ev} 支有事件，總表已插入")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
