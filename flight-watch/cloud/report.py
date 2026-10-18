#!/usr/bin/env python3
"""
每日查价报告：过去 24 小时每条航线的价格曲线（每轮查价一个点）+ 文字摘要，推送到手机。

    python3 report.py          # 立即生成并推送
    python3 report.py --dry    # 只生成 report.png，不推送

后台 watch.py 每天在 settings.report_hour（默认 21 点）自动调用一次。
数据来自 history.csv：每轮查价、每条航线取它日期区间内符合条件的最低价，作为这一轮的点。
Mac 睡眠的时段没有数据，曲线会断开，摘要里如实写「24 小时查到 N 次」。
"""

import csv
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates          # noqa: E402
import matplotlib.pyplot as plt            # noqa: E402
from matplotlib import font_manager        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import watch       # noqa: E402
import watchlist   # noqa: E402

OUT = os.path.join(os.environ.get("FW_DATA_DIR", HERE), "report.png")
WINDOW = timedelta(hours=24)

for path in ("/System/Library/Fonts/Hiragino Sans GB.ttc", "/Library/Fonts/Arial Unicode.ttf",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):   # 最后一个是云端 Ubuntu
    if os.path.exists(path):
        font_manager.fontManager.addfont(path)
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=path).get_name()
        break
plt.rcParams["axes.unicode_minus"] = False


def load_series(entry, since, all_entries=()):
    """[(查价时间, 当轮最低价, 那一班的描述)]，按时间排序。

    按 entry_id 取本条清单自己的记录。旧记录没有 entry_id：只有当这个航线+日期
    只被这一条清单覆盖时才用，否则分不清是哪条清单的筛选结果，宁可不画。
    """
    route = f"{entry['origin']}-{entry['destination']}"
    days = set(watchlist.entry_dates(entry)) or {entry["from"]}

    def unambiguous(day):
        return sum(1 for e in all_entries if f"{e['origin']}-{e['destination']}" == route
                   and e["from"] <= day <= e["to"]) <= 1

    best = {}
    try:
        with open(watch.HISTORY) as f:
            for r in csv.DictReader(f):
                if r["route"] != route or r["depart_date"] not in days:
                    continue
                eid = r.get("entry_id") or ""
                if eid and eid != entry["id"]:
                    continue
                if not eid and not unambiguous(r["depart_date"]):
                    continue
                t = datetime.fromisoformat(r["checked_at"])
                if t < since:
                    continue
                p = float(r["price"])
                if t not in best or p < best[t][0]:
                    best[t] = (p, f"{r['depart_date'][5:]} {r['airlines']}")
    except FileNotFoundError:
        pass
    return [(t, p, d) for t, (p, d) in sorted(best.items())]


def summarize(entry, series, prev):
    route = f"{entry['origin']}→{entry['destination']}"
    dates = entry["from"][5:] if entry["from"] == entry["to"] else f"{entry['from'][5:]}~{entry['to'][5:]}"
    head = f"{route} {dates}"
    if not series:
        return f"{head}：过去 24 小时没有查到数据"
    prices = [p for _, p, _ in series]
    now_t, now_p, now_d = series[-1]
    lo = min(series, key=lambda x: x[1])
    line = (f"{head}：现在 A${now_p:.0f}（{now_d}）"
            f"｜24h 最低 A${lo[1]:.0f}（{lo[0]:%H:%M}）最高 A${max(prices):.0f}")
    if prev:
        diff = now_p - prev[-1][1]
        line += f"｜比昨天 {'+' if diff > 0 else ''}{diff:.0f}" if abs(diff) >= 1 else "｜和昨天持平"
    ideal = entry.get("max_price")
    if ideal:
        line += f"｜理想价 {ideal:.0f}" + (" ✅ 已低于" if now_p <= ideal else f"，还差 {now_p - ideal:.0f}")
    line += f"｜查到 {len(series)} 次"
    return line


def draw(entries, now):
    since = now - WINDOW
    n = len(entries)
    fig, axes = plt.subplots(n, 1, figsize=(8, 2.6 * n), squeeze=False)
    for ax, (entry, series) in zip(axes[:, 0], entries):
        route = f"{entry['origin']} → {entry['destination']}"
        dates = entry["from"][5:] if entry["from"] == entry["to"] else f"{entry['from'][5:]}~{entry['to'][5:]}"
        ax.set_title(f"{route}  {dates}", loc="left", fontsize=12, fontweight="bold")
        ax.set_xlim(since, now)
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.grid(True, color="#E3E7EC", linewidth=0.8)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ideal = entry.get("max_price")
        if ideal:
            ax.axhline(ideal, color="#1E7A4C", linestyle="--", linewidth=1)
            ax.text(since, ideal, f" 理想价 {ideal:.0f}", color="#1E7A4C", fontsize=9, va="bottom")
        if not series:
            ax.text(0.5, 0.5, "过去 24 小时没有数据（Mac 可能在睡眠）", transform=ax.transAxes,
                    ha="center", va="center", color="#7B8794")
            continue
        # 两次查价间隔超过 2.5 小时（Mac 睡着了）就断开，不画假连线
        segs, cur = [], [series[0]]
        for a, b in zip(series, series[1:]):
            if b[0] - a[0] > timedelta(hours=2.5):
                segs.append(cur)
                cur = []
            cur.append(b)
        segs.append(cur)
        for seg in segs:
            ax.plot([t for t, _, _ in seg], [p for _, p, _ in seg], color="#1F4E79",
                    linewidth=2, marker="o", markersize=3.5)
        lo = min(series, key=lambda x: x[1])
        ax.plot([lo[0]], [lo[1]], "o", color="#B86E00", markersize=8, zorder=5)
        ax.annotate(f"最低 A${lo[1]:.0f}", (lo[0], lo[1]), textcoords="offset points",
                    xytext=(6, -14), color="#B86E00", fontsize=9)
        last = series[-1]
        ax.annotate(f"A${last[1]:.0f}", (last[0], last[1]), textcoords="offset points",
                    xytext=(6, 6), fontsize=10, fontweight="bold", color="#1F4E79")
        vals = [p for _, p, _ in series] + ([ideal] if ideal else [])
        pad = max(10, (max(vals) - min(vals)) * 0.25)
        ax.set_ylim(min(vals) - pad, max(vals) + pad)
        ax.set_ylabel("AUD")
    fig.suptitle(f"机票每日价格  {now:%Y-%m-%d %H:%M}（过去 24 小时，每轮查价一个点）", fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(OUT, dpi=140)
    plt.close(fig)


def push_report(title, message):
    """ntfy：PUT 图片做附件，标题和正文走 URL 参数（UTF-8 安全，header 只能 latin-1）。"""
    url = watch.webhook_url()
    if not url:
        raise RuntimeError("没有配置推送地址")
    q = urllib.parse.urlencode({"title": title, "message": message, "filename": "report.png",
                                "tags": "chart_with_upwards_trend", "priority": "default"})
    with open(OUT, "rb") as f:
        req = urllib.request.Request(f"{url}?{q}", data=f.read(), method="PUT")
    urllib.request.urlopen(req, timeout=30)


def build(now=None):
    now = now or datetime.now()
    entries = [e for e in watchlist.load()["entries"] if e.get("enabled", True)]
    rows, lines = [], []
    for e in entries:
        series = load_series(e, now - WINDOW, entries)
        prev = [x for x in load_series(e, now - 2 * WINDOW, entries) if x[0] < now - WINDOW]
        rows.append((e, series))
        lines.append(summarize(e, series, prev))
    if rows:
        draw(rows, now)
    return lines


def main():
    lines = build()
    if not lines:
        print("没有启用的航线")
        return 1
    print("\n".join(lines))
    print(f"\n图：{OUT}")
    if "--dry" not in sys.argv:
        push_report(f"📈 机票日报 {datetime.now():%m-%d}", "\n".join(lines))
        print("已推送")
    return 0


if __name__ == "__main__":
    sys.exit(main())
