#!/usr/bin/env python3
"""主題重試狀態管理（給「當天造不出可觀測數據就跳過」用）。

用法：
    tools/retry_state.py --show                     # 列出全部
    tools/retry_state.py --bump <slug> --reason "…"  # 記一次失敗嘗試，回傳嘗試次數
    tools/retry_state.py --reset <slug>              # 成功後清除

--bump 的結束碼：0 = 還可以再試；3 = 已達上限（3 次），應停手並要人決定。
狀態檔：_scheduler/retry_state.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "_scheduler/retry_state.json"
MAX_ATTEMPTS = 3


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def save(d: dict) -> None:
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--bump")
    ap.add_argument("--reason", default="")
    ap.add_argument("--reset")
    args = ap.parse_args()

    data = load()

    if args.show or (not args.bump and not args.reset):
        if not data:
            print("（無重試紀錄）")
            return 0
        for slug, rec in sorted(data.items()):
            print(f"{slug}: {rec['attempts']}/{MAX_ATTEMPTS} 次 · 最後 {rec['last_attempt']} · {rec.get('reason','')}")
        return 0

    if args.reset:
        data.pop(args.reset, None)
        save(data)
        print(f"已清除：{args.reset}")
        return 0

    slug = args.bump
    rec = data.get(slug, {"attempts": 0, "history": []})
    rec["attempts"] += 1
    rec["last_attempt"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rec["reason"] = args.reason
    rec.setdefault("history", []).append(
        {"at": rec["last_attempt"], "reason": args.reason}
    )
    data[slug] = rec
    save(data)

    print(f"{slug}: 第 {rec['attempts']} 次無法取得可觀測數據（上限 {MAX_ATTEMPTS}）")
    if rec["attempts"] >= MAX_ATTEMPTS:
        print("REACHED_LIMIT")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
