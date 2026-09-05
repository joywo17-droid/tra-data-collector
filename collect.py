#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TDX 臺鐵資料採集器
------------------
以固定間隔輪詢 TDX 的兩支 API，把「原始回應」逐筆寫成 gzip JSONL。
採集階段不做任何欄位解析，確保 schema 有出入時資料不會遺失。

  TrainLiveBoard  列車即時位置動態（含 DelayTime）  預設 60 秒
  Alert           營運通阻                          預設 300 秒

用法：
    export TDX_CLIENT_ID="你的 client id"
    export TDX_CLIENT_SECRET="你的 client secret"
    python3 collect.py --outdir ./raw

    # 只跑 30 分鐘測試
    python3 collect.py --outdir ./raw --duration 1800

輸出：
    raw/liveboard/YYYY-MM-DD.jsonl.gz
    raw/alert/YYYY-MM-DD.jsonl.gz
    每行一筆：{"fetched_at": ISO8601, "payload": <原始回應>}
"""

import argparse
import gzip
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

TDX_AUTH_URL = (
    "https://tdx.transportdata.tw/auth/realms/TDXConnect/"
    "protocol/openid-connect/token"
)
ENDPOINTS = {
    "liveboard": "https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/TrainLiveBoard",
    "alert": "https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/Alert",
}
TZ = timezone(timedelta(hours=8))  # 臺北時間，檔案切日用

_stop = False


def _handle_sigterm(signum, frame):
    global _stop
    _stop = True
    print("\n[收到停止訊號，正在收尾…]", flush=True)


class TdxClient:
    """負責取 token、自動續期、送出請求。"""

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._expires_at = 0.0
        self.session = requests.Session()

    def _refresh(self):
        resp = self.session.post(
            TDX_AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        # 提早 5 分鐘續期，避免邊界失效
        self._expires_at = time.time() + int(data.get("expires_in", 86400)) - 300
        print(f"[auth] token 取得成功，有效至 {datetime.fromtimestamp(self._expires_at, TZ):%H:%M:%S}", flush=True)

    def get(self, url):
        if self._token is None or time.time() >= self._expires_at:
            self._refresh()
        resp = self.session.get(
            url,
            params={"$format": "JSON"},
            headers={
                "authorization": f"Bearer {self._token}",
                "accept-encoding": "gzip",
            },
            timeout=45,
        )
        if resp.status_code == 401:  # token 被提前作廢，重取一次
            self._refresh()
            resp = self.session.get(
                url,
                params={"$format": "JSON"},
                headers={
                    "authorization": f"Bearer {self._token}",
                    "accept-encoding": "gzip",
                },
                timeout=45,
            )
        resp.raise_for_status()
        return resp.json()


def append_record(outdir, kind, payload, tag=""):
    """把一筆回應追加到當日檔案。gzip 支援 append 模式。

    tag 用於區分同一天內的多個執行實例（例如 GitHub Actions 的 run id），
    避免不同執行緒寫入同一個檔案。分析端以萬用字元讀取，不受影響。
    """
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    d = os.path.join(outdir, kind)
    os.makedirs(d, exist_ok=True)
    suffix = f"_{tag}" if tag else ""
    path = os.path.join(d, f"{day}{suffix}.jsonl.gz")
    record = {
        "fetched_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "payload": payload,
    }
    with gzip.open(path, "at", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="./raw", help="輸出目錄")
    ap.add_argument("--liveboard-interval", type=int, default=60,
                    help="列車動態輪詢秒數（預設 60）")
    ap.add_argument("--alert-interval", type=int, default=300,
                    help="營運通阻輪詢秒數（預設 300）")
    ap.add_argument("--duration", type=int, default=0,
                    help="總執行秒數，0 表示無限（預設 0）")
    ap.add_argument("--tag", default="",
                    help="檔名後綴，用於區分同時執行的多個實例（如 CI run id）")
    args = ap.parse_args()

    cid = os.environ.get("TDX_CLIENT_ID")
    secret = os.environ.get("TDX_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("請先設定環境變數 TDX_CLIENT_ID 與 TDX_CLIENT_SECRET")

    signal.signal(signal.SIGINT, _handle_sigterm)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    client = TdxClient(cid, secret)
    started = time.time()
    next_due = {"liveboard": 0.0, "alert": 0.0}
    counts = {"liveboard": 0, "alert": 0}
    errors = {"liveboard": 0, "alert": 0}
    intervals = {
        "liveboard": args.liveboard_interval,
        "alert": args.alert_interval,
    }

    print(f"[start] 輸出至 {os.path.abspath(args.outdir)}，Ctrl-C 停止", flush=True)

    while not _stop:
        if args.duration and time.time() - started >= args.duration:
            break
        now = time.time()
        for kind, url in ENDPOINTS.items():
            if now < next_due[kind]:
                continue
            next_due[kind] = now + intervals[kind]
            try:
                payload = client.get(url)
                append_record(args.outdir, kind, payload, args.tag)
                counts[kind] += 1
                if kind == "liveboard" and counts[kind] % 10 == 0:
                    print(f"[{datetime.now(TZ):%m-%d %H:%M}] "
                          f"liveboard={counts['liveboard']} "
                          f"alert={counts['alert']} "
                          f"err={errors['liveboard']}/{errors['alert']}", flush=True)
                if kind == "alert":
                    n = len(payload.get("Alerts", [])) if isinstance(payload, dict) else "?"
                    print(f"[{datetime.now(TZ):%m-%d %H:%M}] alert 快照，生效中事件 {n} 筆", flush=True)
            except Exception as exc:  # 單次失敗不能中斷兩週的採集
                errors[kind] += 1
                print(f"[warn] {kind} 失敗：{type(exc).__name__}: {exc}", flush=True)
                # 連續失敗時退避
                if errors[kind] % 5 == 0:
                    next_due[kind] = time.time() + min(intervals[kind] * 4, 900)
        time.sleep(1)

    print(f"[done] liveboard {counts['liveboard']} 次 / alert {counts['alert']} 次，"
          f"失敗 {errors['liveboard']}/{errors['alert']} 次", flush=True)


if __name__ == "__main__":
    main()
