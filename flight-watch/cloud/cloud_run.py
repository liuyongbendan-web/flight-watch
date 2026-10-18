#!/usr/bin/env python3
"""云端（GitHub Actions）每小时跑一次：查价 → 推送 → 到点发日报 → 写 status.json 给手机页面。

数据目录 FW_DATA_DIR = 仓库的 flight-watch/cloud-data/，清单读 flight-watch/watchlist.json。
「自动备好下单页」和「定时盯价」只在 Mac 上做（要开浏览器等你点 / 要 45 秒一查）。
"""
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_FW = os.path.dirname(HERE)                       # 仓库里的 flight-watch/
os.environ["FW_CLOUD"] = "1"
os.environ.setdefault("FW_DATA_DIR", os.path.join(REPO_FW, "cloud-data"))
os.environ.setdefault("FW_WATCHLIST", os.path.join(REPO_FW, "watchlist.json"))
os.environ.setdefault("TRIPCOM_STATE_PATH", "/tmp/tripcom-state.json")
os.makedirs(os.environ["FW_DATA_DIR"], exist_ok=True)
sys.path.insert(0, HERE)

import sync    # noqa: E402
import watch   # noqa: E402


def main():
    state = watch.check_once(verbose=True)
    watch.maybe_daily_report()
    import watchlist
    hour = watchlist.load()["settings"].get("report_hour", 21)
    report_failed = hour >= 0 and datetime.now().hour >= hour and \
        watch.load_json(watch.STATE, {}).get("report_sent") != datetime.now().date().isoformat()
    if watch._worker:
        watch._worker.stop()
    state = watch.load_json(watch.STATE, state)
    body = sync.status_body(state, {"trip_ok": True})
    with open(os.path.join(REPO_FW, "status.json"), "w") as f:
        json.dump({"generated_at": datetime.now().isoformat(timespec="seconds"), "source": "cloud", **body},
                  f, ensure_ascii=False, indent=2)
        f.write("\n")
    # 每条都查价失败，就让这次运行标红，GitHub 会发邮件提醒
    errs = [r for r in (state.get("entries") or {}).values() if r.get("error") and not r.get("best")]
    if report_failed:
        print("::error::每日报告没发出去，见上面的「每日报告失败」")
        return 1
    return 1 if state.get("entries") and len(errs) == len(state["entries"]) else 0


if __name__ == "__main__":
    sys.exit(main())
