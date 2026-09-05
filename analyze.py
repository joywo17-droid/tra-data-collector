#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TDX 臺鐵採集資料分析器
----------------------
讀取 collect.py 產出的原始快照，輸出企劃書要用的 CSV 與預覽圖。

用法：
    python3 analyze.py --rawdir ./raw --outdir ./out

輸出（CSV 是主要成果，PNG 只是預覽，正式圖建議用 Excel 重畫以確保中文字型）：
    out/liveboard_events.csv        去重後的「列車到站事件」明細
    out/delay_distribution.csv      延誤分鐘數分布（直方圖資料）
    out/delay_percentiles.csv       中位數 / P75 / P90 / P95 等
    out/delay_by_hour.csv           各時段延誤中位數
    out/corridor_station_delay.csv  示範走廊各站延誤統計
    out/alert_events.csv            去重後的通阻事件明細
    out/alert_field_completeness.csv 公告欄位完整率
    out/*.png                       預覽圖
"""

import argparse
import glob
import gzip
import json
import os
import re
from collections import defaultdict

import pandas as pd

# 示範走廊：以站名比對，避免站碼版本差異
CORRIDOR = ["臺北", "台北", "板橋", "樹林", "鶯歌", "桃園", "新竹"]
CORRIDOR_ORDER = ["臺北", "板橋", "樹林", "鶯歌", "桃園", "新竹"]

# 五類事件的關鍵詞，對應企劃書的分類架構
EVENT_KEYWORDS = {
    "車站事件": ["車站", "站內", "月台", "電扶梯", "電梯", "火災", "人身事故", "動線"],
    "天災事件": ["颱風", "地震", "豪雨", "大雨", "強風", "邊坡", "落石", "土石", "淹水"],
    "列車事件": ["車輛", "故障", "機車", "冒煙", "火警", "供電", "取消", "加開", "停駛"],
    "站間設備事件": ["轉轍器", "平交道", "號誌", "電車線", "軌道", "橋梁", "隧道", "障礙"],
    "人潮事件": ["進香", "演唱會", "跨年", "活動", "疏運", "人潮", "管制"],
}


# ---------- schema 容錯工具 ----------

def pick(d, *names, default=None):
    """從 dict 取值，忽略大小寫，接受多個候選欄位名。"""
    if not isinstance(d, dict):
        return default
    lowered = {k.lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in lowered:
            return lowered[n.lower()]
    return default


def text_of(v):
    """欄位可能是字串，也可能是 {Zh_tw:..., En:...}。"""
    if isinstance(v, dict):
        return pick(v, "Zh_tw", "Zh_TW", "zh_tw", "En", default="") or ""
    return v if isinstance(v, str) else ""


def find_list(payload, *names):
    """在回應中找出主要陣列，容忍外層被不同鍵名包住。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        v = pick(payload, *names)
        if isinstance(v, list):
            return v
        for val in payload.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
    return []


def iter_raw(rawdir, kind):
    pattern = os.path.join(rawdir, kind, "*.jsonl.gz")
    for path in sorted(glob.glob(pattern)):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


# ---------- 列車動態 ----------

def load_liveboard(rawdir):
    """
    輪詢會讓同一列車在同一站重複出現，這裡以
    (車次, 站, 到站狀態, 來源更新時間) 去重，還原為「到站事件」。
    """
    seen = {}
    snapshots = 0
    for rec in iter_raw(rawdir, "liveboard"):
        snapshots += 1
        rows = find_list(rec.get("payload"), "TrainLiveBoards", "TrainLiveBoard")
        for r in rows:
            train = pick(r, "TrainNo", "TrainNumber")
            station = pick(r, "StationID", "StationId")
            sname = text_of(pick(r, "StationName", default=""))
            delay = pick(r, "DelayTime", "Delay")
            status = pick(r, "TrainStationStatus", "Status")
            src = pick(r, "SrcUpdateTime", "UpdateTime") or rec.get("fetched_at")
            if train is None or delay is None:
                continue
            key = (str(train), str(station), str(status), str(src))
            if key in seen:
                continue
            seen[key] = {
                "fetched_at": rec.get("fetched_at"),
                "src_update_time": src,
                "train_no": str(train),
                "train_type": text_of(pick(r, "TrainTypeName", default="")),
                "station_id": str(station),
                "station_name": sname,
                "station_status": status,
                "delay_min": delay,
            }
    df = pd.DataFrame(seen.values())
    print(f"[liveboard] 快照 {snapshots} 份 -> 去重後 {len(df)} 筆到站事件")
    if df.empty:
        return df
    df["delay_min"] = pd.to_numeric(df["delay_min"], errors="coerce")
    df = df.dropna(subset=["delay_min"])
    df["ts"] = pd.to_datetime(df["fetched_at"], errors="coerce", utc=True)
    df["hour"] = df["ts"].dt.tz_convert("Asia/Taipei").dt.hour
    return df


def analyze_delay(df, outdir):
    if df.empty:
        print("[warn] 無列車動態資料，跳過延誤分析")
        return
    df.to_csv(os.path.join(outdir, "liveboard_events.csv"),
              index=False, encoding="utf-8-sig")

    # 分布
    bins = [-1, 0, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 10 ** 6]
    labels = ["準點(0)", "1-3", "4-5", "6-10", "11-15", "16-20",
              "21-30", "31-45", "46-60", "61-90", "91-120", ">120"]
    cut = pd.cut(df["delay_min"], bins=bins, labels=labels)
    dist = cut.value_counts().reindex(labels).fillna(0).astype(int)
    dist_df = pd.DataFrame({
        "延誤區間(分鐘)": labels,
        "事件數": dist.values,
        "佔比": (dist.values / max(len(df), 1)).round(4),
    })
    dist_df.to_csv(os.path.join(outdir, "delay_distribution.csv"),
                   index=False, encoding="utf-8-sig")

    delayed = df[df["delay_min"] > 0]["delay_min"]
    pct = pd.DataFrame({
        "指標": ["樣本數", "準點率(延誤=0)", "延誤事件數",
                 "延誤中位數", "P75", "P90", "P95", "最大值"],
        "數值": [
            len(df),
            round(float((df["delay_min"] <= 0).mean()), 4),
            len(delayed),
            float(delayed.median()) if len(delayed) else 0.0,
            float(delayed.quantile(0.75)) if len(delayed) else 0.0,
            float(delayed.quantile(0.90)) if len(delayed) else 0.0,
            float(delayed.quantile(0.95)) if len(delayed) else 0.0,
            float(delayed.max()) if len(delayed) else 0.0,
        ],
    })
    pct.to_csv(os.path.join(outdir, "delay_percentiles.csv"),
               index=False, encoding="utf-8-sig")

    # 時段
    by_hour = df.groupby("hour")["delay_min"].agg(
        樣本數="count", 中位數="median", 平均="mean",
        P90=lambda s: s.quantile(0.90)).round(2).reset_index()
    by_hour.rename(columns={"hour": "時"}, inplace=True)
    by_hour.to_csv(os.path.join(outdir, "delay_by_hour.csv"),
                   index=False, encoding="utf-8-sig")

    # 示範走廊
    mask = df["station_name"].astype(str).str.contains("|".join(CORRIDOR), na=False)
    cor = df[mask].copy()
    if not cor.empty:
        cor["站"] = cor["station_name"].str.replace("台北", "臺北", regex=False)
        g = cor.groupby("站")["delay_min"].agg(
            樣本數="count", 中位數="median",
            P90=lambda s: s.quantile(0.90),
            延誤比率=lambda s: (s > 0).mean()).round(3).reset_index()
        g["_o"] = g["站"].apply(
            lambda x: CORRIDOR_ORDER.index(x) if x in CORRIDOR_ORDER else 99)
        g = g.sort_values("_o").drop(columns="_o")
        g.to_csv(os.path.join(outdir, "corridor_station_delay.csv"),
                 index=False, encoding="utf-8-sig")

    print(f"[liveboard] 延誤中位數 {pct.iloc[3, 1]} 分、P90 {pct.iloc[5, 1]} 分")
    return dist_df


# ---------- 營運通阻 ----------

def classify(title, desc):
    text = f"{title} {desc}"
    hits = [cat for cat, kws in EVENT_KEYWORDS.items()
            if any(k in text for k in kws)]
    if not hits:
        return "未分類"
    return hits[0] if len(hits) == 1 else "|".join(hits)


def load_alerts(rawdir, outdir):
    events = {}
    snapshots = 0
    for rec in iter_raw(rawdir, "alert"):
        snapshots += 1
        rows = find_list(rec.get("payload"), "Alerts", "Alert")
        for a in rows:
            aid = pick(a, "AlertID", "AlertId", "Id")
            upd = pick(a, "UpdateTime", "SrcUpdateTime")
            if aid is None:
                continue
            key = str(aid)
            title = text_of(pick(a, "Title", default=""))
            desc = text_of(pick(a, "Description", default=""))
            scope = pick(a, "Scope", default={}) or {}
            stations = pick(scope, "Stations", default=[]) or pick(a, "Stations", default=[]) or []
            sections = (pick(scope, "LineSections", default=[])
                        or pick(a, "LineSections", default=[]) or [])
            lines = pick(scope, "Lines", default=[]) or pick(a, "Lines", default=[]) or []
            trains = pick(scope, "Trains", default=[]) or pick(a, "Trains", default=[]) or []
            row = {
                "alert_id": key,
                "title": title,
                "description": desc,
                "status": pick(a, "Status"),
                "level": pick(a, "Level"),
                "effects": json.dumps(pick(a, "Effects", "Effect", default=""),
                                      ensure_ascii=False),
                "direction": pick(a, "Direction"),
                "start_time": pick(a, "StartTime"),
                "end_time": pick(a, "EndTime"),
                "publish_time": pick(a, "PublishTime"),
                "update_time": upd,
                "n_stations": len(stations) if isinstance(stations, list) else 0,
                "n_sections": len(sections) if isinstance(sections, list) else 0,
                "n_lines": len(lines) if isinstance(lines, list) else 0,
                "n_trains": len(trains) if isinstance(trains, list) else 0,
                "分類": classify(title, desc),
                "first_seen": rec.get("fetched_at"),
                "last_seen": rec.get("fetched_at"),
                "更新次數": 1,
            }
            if key in events:
                prev = events[key]
                row["first_seen"] = prev["first_seen"]
                row["更新次數"] = prev["更新次數"] + (1 if upd != prev["update_time"] else 0)
            events[key] = row

    df = pd.DataFrame(events.values())
    print(f"[alert] 快照 {snapshots} 份 -> 去重後 {len(df)} 件事件")
    if df.empty:
        print("[warn] 樣本期間無通阻事件；請延長採集或改用人工編碼補足")
        return df

    for c in ("first_seen", "last_seen"):
        df[c] = pd.to_datetime(df[c], errors="coerce", utc=True)
    df["觀測持續分鐘"] = ((df["last_seen"] - df["first_seen"])
                          .dt.total_seconds() / 60).round(1)
    df.to_csv(os.path.join(outdir, "alert_events.csv"),
              index=False, encoding="utf-8-sig")

    # 欄位完整率：企劃書表 4 的實證依據
    checks = {
        "有明確站碼": df["n_stations"] > 0,
        "有明確區間": df["n_sections"] > 0,
        "有指定車次": df["n_trains"] > 0,
        "有方向資訊": df["direction"].notna(),
        "有預計結束時間": df["end_time"].notna() & (df["end_time"].astype(str) != ""),
        "可被五類分類命中": df["分類"] != "未分類",
    }
    comp = pd.DataFrame({
        "檢查項目": list(checks.keys()),
        "符合件數": [int(v.sum()) for v in checks.values()],
        "總件數": len(df),
        "完整率": [round(float(v.mean()), 4) for v in checks.values()],
    })
    comp.to_csv(os.path.join(outdir, "alert_field_completeness.csv"),
                index=False, encoding="utf-8-sig")
    print(comp.to_string(index=False))
    return df


# ---------- 預覽圖 ----------

def draw(outdir, dist_df, alert_df):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    cjk = None
    for name in ("Noto Sans CJK TC", "Noto Sans CJK SC", "Microsoft JhengHei",
                 "PingFang TC", "Heiti TC", "WenQuanYi Zen Hei"):
        try:
            font_manager.findfont(name, fallback_to_default=False)
            cjk = name
            break
        except Exception:
            continue
    if cjk:
        matplotlib.rcParams["font.sans-serif"] = [cjk]
        matplotlib.rcParams["axes.unicode_minus"] = False
    else:
        print("[note] 找不到中文字型，預覽圖改用英文標籤；正式圖請用 CSV 於 Excel 重畫")

    if dist_df is not None and not dist_df.empty:
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.bar(dist_df["延誤區間(分鐘)"], dist_df["事件數"], color="#c0504d")
        ax.set_xlabel("延誤區間（分鐘）" if cjk else "Delay bucket (min)")
        ax.set_ylabel("到站事件數" if cjk else "Arrival events")
        ax.set_title("臺鐵列車延誤分布" if cjk else "TRA delay distribution")
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        fig.savefig(os.path.join(outdir, "fig_delay_distribution.png"), dpi=200)
        plt.close(fig)

    if alert_df is not None and not alert_df.empty:
        counts = (alert_df["分類"].value_counts())
        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.barh(counts.index[::-1], counts.values[::-1], color="#4f81bd")
        ax.set_xlabel("事件數" if cjk else "Events")
        ax.set_title("通阻事件五類分布" if cjk else "Alert category distribution")
        plt.tight_layout()
        fig.savefig(os.path.join(outdir, "fig_alert_categories.png"), dpi=200)
        plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rawdir", default="./raw")
    ap.add_argument("--outdir", default="./out")
    ap.add_argument("--no-charts", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    lb = load_liveboard(args.rawdir)
    dist = analyze_delay(lb, args.outdir)
    al = load_alerts(args.rawdir, args.outdir)
    if not args.no_charts:
        draw(args.outdir, dist, al)
    print(f"[done] 輸出於 {os.path.abspath(args.outdir)}")


if __name__ == "__main__":
    main()
