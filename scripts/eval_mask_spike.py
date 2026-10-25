"""UFLDv2 Spike 對比腳本（classical vs ai-conf vs ai-geo）。

用途：Phase 1 無真值訊號品質對比。同一幀段同時跑三路訊號，輸出脈衝統計＋
一致性＋耗時＋疊加 gallery，供人工審核；Task C 精度比對待 Phase 1b bronze
真值完成後另行計算。

三路訊號（下游 _side_crossings／交替／閃爍濾波共用，見 manual_annotator）：
  classical — detect_dash_bands 左右半區佔比（基線）
  ai-conf   — UFLDv2 row-softmax 最大機率在框 y 範圍內的均值（lane1→左, lane2→右
              依框內平均 x 大小自動歸屬，不寫死索引語意）
  ai-geo    — AI 車道在 ref_y 的 x 位置定窗（半寬 AI_GEO_HALF_W），窗內傳統
              中位數+35 亮部比例時序

用法：
    python scripts/eval_mask_spike.py --segment S1
    python scripts/eval_mask_spike.py --all --batch 8
    python scripts/eval_mask_spike.py --segment S6 --no-ai  # 只跑基線+gallery

torch／權重為 lazy import；缺 torch 或缺權重時 AI 路自動跳過（exit code 仍 0，
JSON 註記 skipped），不影響基線與測試。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

if __package__ in (None, ""):
    _proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _proj_root not in sys.path:
        sys.path.insert(0, _proj_root)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from dashcam_player.utils.lane_events import GENERIC_BAND  # noqa: E402
from dashcam_player.utils.lane_mask import lane_windows_at_ref  # noqa: E402
from dashcam_player.utils.log import get_logger  # noqa: E402
from dashcam_player.utils.manual_annotator import (  # noqa: E402
    _enforce_alternating_pattern,
    _remove_flicker_pairs,
    _side_crossings,
)
from dashcam_player.utils.marking_detector import (  # noqa: E402
    assess_lighting,
    detect_marking_pulses,
)
from dashcam_player.utils.marking_tracker import detect_dash_bands  # noqa: E402

log = get_logger()

NORM_DIR = r"D:\norm"
SAMPLES_JSON = r"H:\TEMP\opencode\spike_samples.json"
DEFAULT_CKPT = r"H:\TEMP\opencode\culane_res18.pth"
DEFAULT_OUT = r"H:\TEMP\opencode\spike"

# UFLDv2 CULane res18 推論常數（對應 configs/culane_res18.py＋demo.py 前處理）
NUM_ROW, NUM_COL = 72, 81
NUM_CELL_ROW, NUM_CELL_COL = 200, 100
NUM_LANES = 4
TRAIN_W, TRAIN_H, CROP_RATIO = 1600, 320, 0.6
IN_H = int(TRAIN_H / CROP_RATIO)  # 533
ROW_LANE_IDX = (1, 2)  # row-based 的兩條（通常為自車道左右）
AI_GEO_HALF_W = 60  # ai-geo 窗半寬（px, 2560 寬下約虛線寬＋抖動餘裕，spike 起點）
CONF_EXIST_MIN_ROWS = 10  # 該 lane 在框內有效列少於此視為無偵測（NaN）


def _stub_dali() -> None:
    """ufldv2 utils.common 頂層 import nvidia.dali（訓練用）——推論前 stub。"""
    import types

    def _pkg(name: str) -> types.ModuleType:
        if name in sys.modules:
            return sys.modules[name]
        m = types.ModuleType(name)
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = m
        return m

    _pkg("nvidia")
    _pkg("nvidia.dali")
    _pkg("nvidia.dali.pipeline").Pipeline = object  # type: ignore[attr-defined]
    _pkg("nvidia.dali.types")
    _pkg("nvidia.dali.fn")
    _pkg("nvidia.dali.plugin")
    _pkg("nvidia.dali.plugin.pytorch").DALIGenericIterator = object  # type: ignore[attr-defined]
    _pkg("nvidia.dali.plugin.pytorch").LastBatchPolicy = object  # type: ignore[attr-defined]


def load_ufldv2(ckpt_path: str):
    """載入 UFLDv2 CULane res18（lazy torch）。回傳 (net, row_anchor)。"""
    import torch

    _stub_dali()
    sys.path.insert(0, r"H:\TEMP\opencode\ufldv2")
    from model.model_culane import parsingNet

    net = parsingNet(
        pretrained=False, backbone="18",
        num_grid_row=NUM_CELL_ROW, num_cls_row=NUM_ROW,
        num_grid_col=NUM_CELL_COL, num_cls_col=NUM_COL,
        num_lane_on_row=NUM_LANES, num_lane_on_col=NUM_LANES,
        use_aux=False, input_height=TRAIN_H, input_width=TRAIN_W,
        fc_norm=True,
    )
    sd = torch.load(ckpt_path, map_location="cpu")
    sd = sd.get("model", sd)
    sd = {k[7:] if k.startswith("module.") else k: v for k, v in sd.items()}
    net.load_state_dict(sd, strict=True)
    net.cuda().eval()
    row_anchor = np.linspace(0.42, 1, NUM_ROW)
    return net, row_anchor


def preprocess_batch(frames_bgr: list[np.ndarray]):
    """BGR list → UFLDv2 輸入 tensor（Resize 1600x533 取底部 320＋正規化）。"""
    import torch

    arr = np.stack([
        cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2RGB), (TRAIN_W, IN_H))[-TRAIN_H:, :, :]
        for f in frames_bgr
    ])
    x = torch.from_numpy(arr).permute(0, 3, 1, 2).contiguous().float().div(255.0)
    mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
    return ((x - mean) / std).cuda()


@np.errstate(all="ignore")
def _summarize_series(sig: list[float], fps: float) -> dict:
    """單路時序訊號摘要（無真值）：脈衝數／中位週期／一致性／SNR。"""
    a = np.array(sig, dtype=np.float64)
    valid = a[~np.isnan(a)]
    if len(valid) < 20:
        return {"n_valid": int(len(valid)), "pulses": 0, "period": None,
                "cons": 0.0, "snr": 0.0, "mean": None}
    filled = np.where(np.isnan(a), np.nanmean(a), a)
    ons, med, cons = detect_marking_pulses(filled.astype(np.float64), fps)
    mu, sd = float(valid.mean()), float(valid.std())
    return {"n_valid": int(len(valid)), "pulses": int(len(ons)),
            "period": round(float(med), 3) if med else None,
            "cons": round(float(cons), 3),
            "snr": round(float(mu / sd), 3) if sd > 1e-9 else 0.0,
            "mean": round(mu, 4)}


def _crossings_from_occ(occ: list[float], start: int, side: str) -> list[dict]:
    """佔比序列→bw/wb 候選（與 _auto_detect_crossings 同一下游）。"""
    out = []
    for fr0, typ in _side_crossings(occ, start):
        out.append({"frame": float(fr0), "type": typ, "auto": True, "side": side})
    out = _enforce_alternating_pattern(out)
    return _remove_flicker_pairs(out)


def run_segment(seg: dict, ckpt: str, batch: int, outdir: str, no_ai: bool,
                dump_series: bool = False) -> dict:
    vid = os.path.join(NORM_DIR, seg["file"])
    cap = cv2.VideoCapture(vid)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    fx0, fx1, fy0, fy1 = GENERIC_BAND
    x0, x1 = int(W * fx0), int(W * fx1)
    y0, y1 = int(H * fy0), int(H * fy1)
    div_x = (x0 + x1) // 2
    ref_y = (y0 + y1) // 2
    start, end = seg["start"], seg["end"]

    occ_l_c, occ_r_c = [], []
    geo_l, geo_r = [], []          # ai-geo 窗內亮部比例
    conf_l, conf_r = [], []        # ai-conf 框內 row-softmax 均值
    frames_idx: list[int] = []
    sky_sum, sky_px, sat_px, band_px = 0.0, 0, 0, 0
    gray_cache: list[np.ndarray] = []

    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    fi = start
    while fi < end:
        ret, fr = cap.read()
        if not ret or fr is None:
            break
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        gray_cache.append(g)
        dl, _ = detect_dash_bands(g, x0, y0, div_x, y1)
        dr, _ = detect_dash_bands(g, div_x, y0, x1, y1)
        wl = max(div_x - x0, 1)
        wr = max(x1 - div_x, 1)
        occ_l_c.append(min(sum(b - a for a, b in dl) / wl, 1.0))
        occ_r_c.append(min(sum(b - a for a, b in dr) / wr, 1.0))
        band = g[y0:y1, :]
        sky = g[int(H * 0.05):int(H * 0.20), :]
        sky_sum += float(sky.sum())
        sky_px += sky.size
        sat_px += int(np.count_nonzero(band > 200))
        band_px += band.size
        frames_idx.append(fi)
        fi += 1
    cap.release()
    n = len(frames_idx)

    lighting, light_reason = assess_lighting(
        sky_sum / max(sky_px, 1), sat_px / max(band_px, 1))

    res: dict = {"id": seg["id"], "file": seg["file"], "category": seg["category"],
                 "start": start, "n_frames": n, "fps": fps,
                 "box": [x0, y0, x1, y1],
                 "lighting": lighting, "light_reason": light_reason,
                 "classical": {"left": _summarize_series(occ_l_c, fps),
                               "right": _summarize_series(occ_r_c, fps)}}
    res["classical"]["crossings"] = {
        "left": _crossings_from_occ(occ_l_c, start, "left"),
        "right": _crossings_from_occ(occ_r_c, start, "right")}

    ai_info: dict = {"skipped": True, "reason": "--no-ai" if no_ai else ""}
    ai_ms_per_frame: list[float] = []
    ai_lanes_cache: list = []  # 每幀 [(lane, conf_rows)]，供 gallery
    if not no_ai:
        try:
            import torch

            net, row_anchor = load_ufldv2(ckpt)
            box_rows = [k for k in range(NUM_ROW)
                        if y0 <= row_anchor[k] * H <= y1]
            # warmup
            cap2 = cv2.VideoCapture(vid)
            cap2.set(cv2.CAP_PROP_POS_FRAMES, start)
            ret, fr0 = cap2.read()
            cap2.release()
            if ret and fr0 is not None:
                with torch.no_grad():
                    _ = net(preprocess_batch([fr0]))
            torch.cuda.synchronize()

            for b in range(0, n, batch):
                # 需 BGR 原圖做 AI；重讀該 batch（spike 簡單起見，不常駐全段）
                cap3 = cv2.VideoCapture(vid)
                bgrs = []
                for fidx in frames_idx[b:b + batch]:
                    cap3.set(cv2.CAP_PROP_POS_FRAMES, fidx)
                    r2, f2 = cap3.read()
                    bgrs.append(f2 if (r2 and f2 is not None) else np.zeros((H, W, 3), np.uint8))
                cap3.release()
                t0 = time.perf_counter()
                with torch.no_grad():
                    pred = net(preprocess_batch(bgrs))
                torch.cuda.synchronize()
                t1 = time.perf_counter()
                ai_ms_per_frame.extend([(t1 - t0) / len(bgrs) * 1000.0] * len(bgrs))

                prob = pred["loc_row"].softmax(1)  # B x 200 x 72 x 4
                pmax, _ = prob.max(1)              # B x 72 x 4
                ex = pred["exist_row"].argmax(1).cpu().numpy()  # B x 72 x 4
                pm = pmax.cpu().numpy()
                loc = pred["loc_row"].argmax(1).cpu().numpy()
                for bi in range(len(bgrs)):
                    lane_info = []
                    for li in ROW_LANE_IDX:
                        pts = []
                        cfs = []
                        for k in box_rows:
                            if ex[bi, k, li]:
                                gx = int(loc[bi, k, li])
                                px = (gx + 0.5) / NUM_CELL_ROW * W
                                py = float(row_anchor[k] * H)
                                pts.append((px, py))
                                cfs.append(float(pm[bi, k, li]))
                        lane_info.append((pts, cfs))
                    # 左右歸屬：框內平均 x 小者為左
                    means = [(np.mean([p[0] for p in pts]) if pts else np.nan)
                             for pts, _ in lane_info]
                    order = np.argsort(np.array(means, dtype=np.float64))
                    (pts_a, cfs_a), (pts_b, cfs_b) = lane_info[order[0]], lane_info[order[1]]
                    cm_a = float(np.mean(cfs_a)) if len(cfs_a) >= CONF_EXIST_MIN_ROWS else np.nan
                    cm_b = float(np.mean(cfs_b)) if len(cfs_b) >= CONF_EXIST_MIN_ROWS else np.nan
                    conf_l.append(cm_a)
                    conf_r.append(cm_b)
                    ai_lanes_cache.append((pts_a, pts_b))
                    # ai-geo：窗內傳統亮部比例
                    for pts, store in ((pts_a, geo_l), (pts_b, geo_r)):
                        wins = lane_windows_at_ref(
                            [pts], ref_y, AI_GEO_HALF_W, W) if pts else []
                        if not wins:
                            store.append(np.nan)
                            continue
                        xa, xb = wins[0]
                        sub = gray_cache[b + bi][y0:y1, xa:xb]
                        if sub.size == 0:
                            store.append(np.nan)
                            continue
                        base = float(np.median(sub))
                        thr = max(120.0, base + 35.0)
                        store.append(float(np.count_nonzero(sub > thr)) / sub.size)
            ai_info = {"skipped": False,
                       "ms_per_frame_median": round(float(np.median(ai_ms_per_frame)), 1),
                       "ms_per_frame_p90": round(float(np.percentile(ai_ms_per_frame, 90)), 1)}
            res["ai_conf"] = {"left": _summarize_series(conf_l, fps),
                              "right": _summarize_series(conf_r, fps)}
            res["ai_conf"]["crossings"] = {
                "left": _crossings_from_occ(
                    [0.0 if np.isnan(v) else v for v in conf_l], start, "left"),
                "right": _crossings_from_occ(
                    [0.0 if np.isnan(v) else v for v in conf_r], start, "right")}
            res["ai_geo"] = {"left": _summarize_series(geo_l, fps),
                             "right": _summarize_series(geo_r, fps)}
            res["ai_geo"]["crossings"] = {
                "left": _crossings_from_occ(
                    [0.0 if np.isnan(v) else v for v in geo_l], start, "left"),
                "right": _crossings_from_occ(
                    [0.0 if np.isnan(v) else v for v in geo_r], start, "right")}
        except ImportError as e:
            ai_info = {"skipped": True, "reason": f"no torch: {e}"}
        except Exception as e:
            ai_info = {"skipped": True, "reason": f"{type(e).__name__}: {e}"}
    res["ai"] = ai_info
    if dump_series:
        # conf_/geo_ 在 no_ai 時為空 list（函式頂部已初始化），直接存
        res["series"] = {
            "frames": frames_idx,
            "classical_left": occ_l_c, "classical_right": occ_r_c,
            "ai_conf_left": conf_l, "ai_conf_right": conf_r,
            "ai_geo_left": geo_l, "ai_geo_right": geo_r,
        }

    # gallery：每 ~60 幀一張（框＋傳統區間＋AI 車道線）
    gal_dir = os.path.join(outdir, "gallery", seg["id"])
    os.makedirs(gal_dir, exist_ok=True)
    cap4 = cv2.VideoCapture(vid)
    for oi in range(0, n, max(1, n // 10)):
        cap4.set(cv2.CAP_PROP_POS_FRAMES, frames_idx[oi])
        r4, f4 = cap4.read()
        if not (r4 and f4 is not None):
            continue
        g4 = cv2.cvtColor(f4, cv2.COLOR_BGR2GRAY)
        dl4, _ = detect_dash_bands(g4, x0, y0, div_x, y1)
        dr4, _ = detect_dash_bands(g4, div_x, y0, x1, y1)
        for a, b in dl4 + dr4:
            cv2.rectangle(f4, (a, y0), (b, y1), (255, 0, 255), 2)
        cv2.rectangle(f4, (x0, y0), (x1, y1), (0, 255, 255), 2)
        cv2.line(f4, (div_x, y0), (div_x, y1), (255, 0, 255), 2)
        cv2.line(f4, (0, ref_y), (W, ref_y), (0, 165, 255), 2)
        if ai_lanes_cache:
            for pts in ai_lanes_cache[oi]:
                if len(pts) >= 2:
                    cv2.polylines(f4, [np.array(
                        [(int(x), int(y)) for x, y in pts], np.int32)],
                        False, (0, 255, 0), 3)
        small = cv2.resize(f4, (1280, 720), interpolation=cv2.INTER_AREA)
        cv2.putText(small, f"{seg['id']} f{frames_idx[oi]}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
        cv2.imwrite(os.path.join(gal_dir, f"f{frames_idx[oi]}.jpg"), small,
                    [cv2.IMWRITE_JPEG_QUALITY, 82])
    cap4.release()

    with open(os.path.join(outdir, f"{seg['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="UFLDv2 Spike 三路訊號對比")
    ap.add_argument("--segment", default="", help="S1..S9（空=全部）")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ckpt", default=DEFAULT_CKPT)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--outdir", default=DEFAULT_OUT)
    ap.add_argument("--no-ai", action="store_true", help="只跑基線")
    ap.add_argument("--dump-series", action="store_true",
                    help="把三路原始時序存入 JSON（供 bronze 脈衝評分）")
    args = ap.parse_args()

    with open(SAMPLES_JSON, encoding="utf-8") as f:
        segs = json.load(f)["segments"]
    if args.segment:
        segs = [s for s in segs if s["id"] == args.segment]
    elif not args.all:
        ap.error("需 --segment SID 或 --all")
    os.makedirs(args.outdir, exist_ok=True)

    for s in segs:
        log.info(f"[{s['id']}] {s['category']} {s['file']} f{s['start']}-{s['end']}")
        r = run_segment(s, args.ckpt, args.batch, args.outdir, args.no_ai,
                        args.dump_series)
        for path_name in ("classical", "ai_conf", "ai_geo"):
            if path_name not in r:
                continue
            for side in ("left", "right"):
                d = r[path_name][side]
                log.info(f"  {path_name:9s} {side:5s} pulses={d['pulses']:3d} "
                         f"period={d['period']} cons={d['cons']:.2f} snr={d['snr']:.2f} "
                         f"cross_bw={sum(1 for c in r[path_name]['crossings'][side] if c['type']=='bw')}")
        if not r["ai"].get("skipped", True):
            log.info(f"  ai infer median={r['ai']['ms_per_frame_median']}ms p90={r['ai']['ms_per_frame_p90']}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
