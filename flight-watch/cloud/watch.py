#!/usr/bin/env python3
"""
机票盯价 —— 后台常驻、按清单查价、跌破理想价推手机。

    python3 watch.py              # 常驻盯价（launchd 用这个）
    python3 watch.py --once       # 立刻查一轮并打印，不常驻
    python3 watch.py --health     # 看是否在正常跑
    python3 watch.py --test-push  # 发一条测试推送

取价走本机 tripcom-mcp（127.0.0.1:18061），不自己开浏览器。
推送复用 doc-booking/watch_config.py 里的 ntfy 地址（同一个手机主题，密钥只存一处）。
"""

import csv
import importlib.util
import json
import os
import signal
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import watchlist  # noqa: E402

TRIP_MCP = "http://127.0.0.1:18061/mcp"
# 云端（GitHub Actions）跑同一份代码：FW_CLOUD=1 时直接调 tripcom 包查价，数据放 FW_DATA_DIR
CLOUD = os.environ.get("FW_CLOUD") == "1"
DATA = os.environ.get("FW_DATA_DIR", HERE)
STATE = os.path.join(DATA, "state.json")
SENT = os.path.join(DATA, "notify_sent.json")
HEARTBEAT = os.path.join(HERE, "heartbeat.json")
HISTORY = os.path.join(DATA, "history.csv")
CLOUD_STALE = timedelta(hours=3)   # 云端超过 3 小时没更新，Mac 就接手推送
CHECK_NOW = os.path.join(HERE, "check_now")
LOG = os.path.join(HERE, "watch.log")
PIDFILE = os.path.join(HERE, "watch.pid")
DOC_CONFIG = ""
MAX_SEARCHES_PER_ENTRY = 3


# ── 基础 ────────────────────────────────────────────────────

def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def webhook_url():
    """ntfy 地址只在 doc-booking 存一份，这里运行时读取，不复制密钥。"""
    url = os.environ.get("FW_WEBHOOK_URL")
    if url:
        return url
    if not os.path.exists(DOC_CONFIG):
        return ""
    spec = importlib.util.spec_from_file_location("doc_watch_config", DOC_CONFIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    url = getattr(mod, "WEBHOOK_URL", "") or ""
    if url and "://" not in url:
        url = "https://ntfy.sh/" + url.lstrip("/")
    return url


def push(title, body, click=None, priority="high"):
    url = webhook_url()
    if not url:
        raise RuntimeError("没有配置推送地址（doc-booking/watch_config.py 的 WEBHOOK_URL）")
    # ntfy 的 header 只能 latin-1，中文标题放正文第一行
    headers = {"Title": "Flight price alert", "Priority": priority, "Tags": "airplane"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(url, data=f"{title}\n{body}".encode(), headers=headers)
    urllib.request.urlopen(req, timeout=15)


# ── 调 tripcom-mcp ──────────────────────────────────────────

def mcp_call(tool, args, timeout=200):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    req = urllib.request.Request(TRIP_MCP, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    if "error" in resp:
        raise RuntimeError(resp["error"].get("message"))
    result = resp["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        raise RuntimeError(text)
    return text


def trip_healthy():
    if CLOUD:
        return True
    try:
        with urllib.request.urlopen("http://127.0.0.1:18061/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


_worker = None


def search(entry, day):
    if CLOUD:
        global _worker
        from tripcom import flights
        from tripcom.browser import BrowserWorker
        if _worker is None:
            _worker = BrowserWorker()
        return flights.search_flights(_worker, origin=entry["origin"], destination=entry["destination"],
                                      depart_date=day, adults=entry["adults"], limit=500)
    text = mcp_call("search_flights", {
        "origin": entry["origin"], "destination": entry["destination"],
        "depart_date": day, "adults": entry["adults"], "limit": 500, "detail": True,
    })
    marker = "--- 完整数据 ---"
    if marker not in text:
        raise RuntimeError(f"tripcom-mcp 返回格式不对：{text[:120]}")
    return json.loads(text.split(marker, 1)[1])


def qualifies(f, entry):
    if entry["max_stops"] is not None and f["stops"] > entry["max_stops"]:
        return False
    if f.get("self_transfer") and not entry["allow_self_transfer"]:
        return False
    if entry["need_bag"] and not f.get("checked_baggage"):
        return False
    if not watchlist.in_slots(f["depart"], entry.get("depart_slots")):
        return False
    if entry.get("transfer_pref") == "fast":
        if entry.get("max_duration_h") and (f.get("duration_min") or 1e9) > entry["max_duration_h"] * 60:
            return False
        if entry.get("max_layover_h") and any(
                (l.get("minutes") or 0) > entry["max_layover_h"] * 60 for l in f["outbound"]["layovers"]):
            return False
    return f.get("price_total") is not None


CATEGORIES = (("direct", "直飞"), ("connecting", "转机"), ("self_transfer", "自行转机"))


def category(f):
    if f["stops"] == 0:
        return "direct"
    return "self_transfer" if f.get("self_transfer") else "connecting"


def by_category(data, entry, day):
    """像 Trip.com 那样把直飞、转机分开给最低价——不受中转筛选影响，只受行李条件影响。"""
    out = {}
    for f in data["flights"]:
        if f.get("price_total") is None or (entry["need_bag"] and not f.get("checked_baggage")):
            continue
        if not watchlist.in_slots(f["depart"], entry.get("depart_slots")):
            continue
        c = category(f)
        if c not in out or f["price_total"] < out[c]["price"]:
            out[c] = _summary(f, data, day)
    return out


def best_of(data, entry, day):
    ok = [f for f in data["flights"] if qualifies(f, entry)]
    if not ok:
        return None
    return _summary(min(ok, key=lambda x: x["price_total"]), data, day)


def _summary(f, data, day):
    return {
        "date": day,
        "price": f["price_total"],
        "currency": data.get("currency", "AUD"),
        "airlines": "/".join(f["airlines"]),
        "flights": " ".join(s["flight_no"] for s in f["outbound"]["segments"]),
        "depart": f["depart"], "arrive": f["arrive"],
        "duration": f["duration"], "stops": f["stops"],
        "via": "/".join(l["airport"] for l in f["outbound"]["layovers"] if l["airport"]),
        "self_transfer": f.get("self_transfer", False),
        "checked_baggage": f.get("checked_baggage", False),
        "seats_left": f.get("seats_left"),
        "complete": data.get("complete", True),
        "url": data["query"]["url"],
    }


# ── 一条清单的一轮检查 ──────────────────────────────────────

def check_entry(entry):
    if entry.get("transfer_pref") == "stopover":
        return check_stopover(entry)
    return check_direct(entry)


def check_direct(entry):
    """单日：查一次。区间：先查中间那天拿价格日历，再挑日历最便宜的日子实搜确认。

    价格日历是缓存值，只用来挑日子；推送只认实搜到的价格。
    """
    days = watchlist.entry_dates(entry)
    if not days:
        return {"status": "expired", "per_day": {}, "calendar": []}

    per_day, calendar, types = {}, [], {}

    def take(day, data):
        per_day[day] = best_of(data, entry, day)
        for c, v in by_category(data, entry, day).items():
            if c not in types or v["price"] < types[c]["price"]:
                types[c] = v

    first = days[len(days) // 2]
    data = search(entry, first)
    take(first, data)
    calendar = [c for c in data.get("price_calendar") or [] if c["date"] in days]

    if len(days) > 1:
        candidates = [c["date"] for c in sorted(calendar, key=lambda c: c["price"])
                      if c["date"] not in per_day]
        for day in candidates[:MAX_SEARCHES_PER_ENTRY - 1]:
            time.sleep(5)
            take(day, search(entry, day))

    found = [b for b in per_day.values() if b]
    best = min(found, key=lambda b: b["price"]) if found else None
    return {"status": "ok", "per_day": per_day, "calendar": calendar, "best": best, "by_type": types}


# ── 中转停留：在中转城市待够时间、玩一玩 ─────────────────────
# 两个来源一起比：
#   一张票：出发地→目的地的搜索结果里，本来就在停留城市中转、且等待时间落在区间内的行程（有联程保障）
#   两张票：出发地→停留城市 + 停留城市→目的地 各买一张单程，按到达/出发时间拼出停留时长合适的组合
# 两张票是各自独立的机票：前一段延误导致误了后一段，航空公司不管。所以停留时间至少 12 小时起步。

MAX_STOPOVER_SEARCHES = 7
STOPOVER_POOL = 40          # 每段只拿最便宜的 40 班来拼


def _fmt_min(m):
    m = int(round(m))
    return f"{m // 60}h{m % 60:02d}m"


def _gap_h(arrive, depart):
    return (datetime.fromisoformat(depart) - datetime.fromisoformat(arrive)).total_seconds() / 3600


def _leg_ok(f, entry, first):
    if entry.get("max_stops") is not None and f["stops"] > entry["max_stops"]:
        return False
    if f.get("self_transfer") and not entry.get("allow_self_transfer"):
        return False
    if entry.get("need_bag") and not f.get("checked_baggage"):
        return False
    if first and not watchlist.in_slots(f["depart"], entry.get("depart_slots")):
        return False
    return f.get("price_total") is not None


def _leg(f, data):
    segs = f["outbound"]["segments"]
    return {"flights": " ".join(s["flight_no"] for s in segs), "airlines": "/".join(f["airlines"]),
            "from": segs[0]["from"], "to": segs[-1]["to"], "depart": f["depart"], "arrive": f["arrive"],
            "duration": f["duration"], "stops": f["stops"], "price": f["price_total"],
            "checked_baggage": f.get("checked_baggage", False), "url": data["query"]["url"]}


def _combo(day, city, a, da, b, db):
    stay = _gap_h(a["arrive"], b["depart"])
    la, lb = _leg(a, da), _leg(b, db)
    seats = [x for x in (a.get("seats_left"), b.get("seats_left")) if x]
    return {
        "date": day, "price": a["price_total"] + b["price_total"], "currency": da.get("currency", "AUD"),
        "airlines": f"{la['airlines']} ＋ {lb['airlines']}", "flights": f"{la['flights']} ＋ {lb['flights']}",
        "depart": a["depart"], "arrive": b["arrive"],
        "duration": _fmt_min((a["duration_min"] or 0) + stay * 60 + (b["duration_min"] or 0)),
        "stops": a["stops"] + b["stops"] + 1, "via": f"{city} 停 {stay:.0f}h",
        "self_transfer": False,
        "checked_baggage": la["checked_baggage"] and lb["checked_baggage"],
        "seats_left": min(seats) if seats else None,
        "complete": da.get("complete", True) and db.get("complete", True), "url": la["url"],
        "stopover": {"city": city, "hours": round(stay, 1), "kind": "two_tickets", "legs": [la, lb]},
    }


def _single_stopover(f, data, day, city, lo, hi):
    """一张票里本来就在停留城市长时间中转的行程。"""
    ports = watchlist.city_airports(city)
    for l in f["outbound"]["layovers"]:
        if l.get("airport") in ports and lo * 60 <= (l.get("minutes") or 0) <= hi * 60:
            out = _summary(f, data, day)
            out["via"] = f"{city} 停 {l['minutes'] / 60:.0f}h"
            out["stopover"] = {"city": city, "hours": round(l["minutes"] / 60, 1), "kind": "one_ticket"}
            return out
    return None


def check_stopover(entry):
    """日期区间指的是第一段（离开出发地）的日期。每轮最多搜 MAX_STOPOVER_SEARCHES 次。"""
    days = watchlist.entry_dates(entry)
    if not days:
        return {"status": "expired", "per_day": {}, "calendar": []}
    o, d, city = entry["origin"], entry["destination"], entry["stopover_city"]
    lo = float(entry.get("stopover_min_h") or 12)
    hi = float(entry.get("stopover_max_h") or 48)
    ms = entry.get("max_stops")
    one_ticket_entry = {**entry, "max_stops": None if ms is None else 2 * ms + 1}

    cache, errors, budget = {}, [], [MAX_STOPOVER_SEARCHES]

    def get(a, b, day):
        k = (a, b, day)
        if k not in cache:
            if budget[0] <= 0:
                return None
            budget[0] -= 1
            if cache:
                time.sleep(5)
            try:
                cache[k] = search({**entry, "origin": a, "destination": b}, day)
            except Exception as e:
                log(f"{a}→{b} {day} 查价失败: {type(e).__name__}: {e}")
                errors.append(f"{a}→{b} {day}: {e}")
                cache[k] = None
        return cache[k]

    first = days[len(days) // 2]
    l1 = get(o, city, first)
    calendar = [c for c in (l1 or {}).get("price_calendar") or [] if c["date"] in days]
    order = [first] + [c["date"] for c in sorted(calendar, key=lambda c: c["price"]) if c["date"] != first]

    per_day, best_two, best_one = {}, None, None
    for day in order:
        if budget[0] <= 0 and (o, city, day) not in cache:
            break
        l1 = get(o, city, day)
        whole = get(o, d, day)
        one = None
        if whole:
            for f in sorted(whole["flights"], key=lambda f: f.get("price_total") or 1e12):
                if qualifies(f, one_ticket_entry):
                    one = _single_stopover(f, whole, day, city, lo, hi)
                    if one:
                        break
        two = None
        if l1:
            A = sorted((f for f in l1["flights"] if _leg_ok(f, entry, True)),
                       key=lambda f: f["price_total"])[:STOPOVER_POOL]
            if A:
                arr = [datetime.fromisoformat(f["arrive"]) for f in A]
                start = (min(arr) + timedelta(hours=lo)).date()
                end = (max(arr) + timedelta(hours=hi)).date()
                leg2_days = []
                while start <= end and len(leg2_days) < 3:
                    leg2_days.append(start.isoformat())
                    start += timedelta(days=1)
                for day2 in leg2_days:
                    l2 = get(city, d, day2)
                    if not l2:
                        continue
                    B = sorted((f for f in l2["flights"] if _leg_ok(f, entry, False)),
                               key=lambda f: f["price_total"])[:STOPOVER_POOL]
                    for a in A:
                        for b in B:
                            if lo <= _gap_h(a["arrive"], b["depart"]) <= hi:
                                if not two or a["price_total"] + b["price_total"] < two["price"]:
                                    two = _combo(day, city, a, l1, b, l2)
        found = [x for x in (one, two) if x]
        per_day[day] = min(found, key=lambda x: x["price"]) if found else None
        if two and (not best_two or two["price"] < best_two["price"]):
            best_two = two
        if one and (not best_one or one["price"] < best_one["price"]):
            best_one = one

    if not any(cache.values()):
        raise RuntimeError("中转停留的几次搜索都失败了：" + "; ".join(errors[:2]))
    found = [b for b in per_day.values() if b]
    best = min(found, key=lambda b: b["price"]) if found else None
    types = {k: v for k, v in (("stopover_one", best_one), ("stopover_two", best_two)) if v}
    return {"status": "ok", "per_day": per_day, "calendar": calendar, "best": best, "by_type": types}


HISTORY_COLS = ["checked_at", "route", "depart_date", "price", "airlines", "flights", "stops",
                "self_transfer", "checked_baggage", "data_type", "entry_id"]


def record_history(entry, per_day):
    """每轮每条清单写一行/日期。entry_id 用来区分同航线同日期、筛选条件不同的两条清单。"""
    if os.path.exists(HISTORY):
        with open(HISTORY) as f:
            header = f.readline().strip().split(",")
        if header != HISTORY_COLS:            # 旧格式：补上表头列，旧行 entry_id 留空
            with open(HISTORY) as f:
                rows = list(csv.reader(f))[1:]
            with open(HISTORY, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(HISTORY_COLS)
                w.writerows(r + [""] * (len(HISTORY_COLS) - len(r)) for r in rows)
    new = not os.path.exists(HISTORY)
    with open(HISTORY, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(HISTORY_COLS)
        now = datetime.now().isoformat(timespec="minutes")
        for day, b in sorted(per_day.items()):
            if b:
                w.writerow([now, f"{entry['origin']}-{entry['destination']}", day, b["price"],
                            b["airlines"], b["flights"], b["stops"], b["self_transfer"],
                            b["checked_baggage"], "actual" if b["complete"] else "upper-bound", entry["id"]])


def cloud_alive():
    """云端在正常干活吗：仓库里的 status.json 是云端写的、且 3 小时内更新过。只在 Mac 上有意义。"""
    if CLOUD:
        return True
    try:
        import sync
        st = load_json(sync.REMOTE_ST, {})
        if st.get("source") != "cloud":
            return False
        if datetime.now() - datetime.fromisoformat(st["generated_at"]) < CLOUD_STALE:
            return True
        # 本地克隆里的状态旧了：只有刚刚成功拉过仓库、确认云端真的没更新，才算云端停了。
        # 醒来时网络还没通、拉不到最新状态，不能据此就接手（10-14 的误报就是这么来的）。
        return not _pulled_recently()
    except Exception:
        return True


_last_pull_ok = [0.0]


def _pulled_recently():
    return time.time() - _last_pull_ok[0] < 15 * 60


def maybe_notify(entry, best, sent, passenger="乘机人", urgent=False, send=True):
    """同一天同一条信息只发一次；只有新日期、或比上次推送更便宜才再报。

    Trip.com 会拦截自动化浏览器的下单流程，所以这里不替你下单，
    只把「怎么最快手动订到这一班」写清楚，你点开照做约 1 分钟。
    """
    if not best or entry["max_price"] is None or best["price"] > entry["max_price"]:
        return False
    # 按「航线+日期」去重：多条清单覆盖同一天时只报一次
    key = f"{entry['origin']}-{entry['destination']}|{best['date']}"
    if best.get("stopover"):
        key = f"{entry['origin']}-{best['stopover']['city']}-{entry['destination']}|{best['date']}"
    last = sent.get(key)
    if last and best["price"] >= last["price"] - 5:
        return False
    if best.get("stopover", {}).get("kind") == "two_tickets":
        title, body, click = _stopover_message(entry, best, passenger)
        if send:
            push(title, body, click=click, priority="urgent" if urgent else "high")
        sent[key] = {"price": best["price"], "at": datetime.now().isoformat(timespec="minutes")}
        log(f"已推送 {title}")
        return True

    route = f"{entry['origin']}→{entry['destination']}"
    title = f"✈️ {route} {best['date'][5:]} A${best['price']:.0f}（理想价 {entry['max_price']:.0f}）"
    tags = []
    tags.append("直飞" if best["stops"] == 0 else f"{best['stops']}转 经{best['via']}")
    tags.append("含托运" if best["checked_baggage"] else "仅手提")
    if best["self_transfer"]:
        tags.append("⚠️自行转机")
    if best.get("stopover"):
        tags.insert(0, f"在 {best['stopover']['city']} 停 {best['stopover']['hours']:.0f} 小时（一张票）")
    if best["seats_left"] and best["seats_left"] <= 5:
        tags.append(f"仅剩{best['seats_left']}座")
    promo = entry.get("promo_code")
    body = (f"{best['airlines']} [{best['flights']}]\n"
            f"{best['depart'][11:]} 起飞 → {best['arrive'][5:]} 到达，{best['duration']}\n"
            f"{' · '.join(tags)}\n"
            + ("" if best["complete"] else "（本次只解析到部分结果，实际可能更便宜）\n")
            + "\n手动下单（约 1 分钟）：\n"
            f"1. 点开本通知 → 点「Cheapest」\n"
            f"2. 找 {best['depart'][11:]} 起飞、{best['flights']} 那班 → View Details → Select\n"
            f"3. 勾 {passenger} → 保险/TripFlex 选不要 → Next → Next step\n"
            + (f"4. 优惠码栏填 {promo}\n" if promo else
               "4. 页面会自动套一张银联优惠码，不用银联付款就点掉「Applied」\n")
            + "5. 到付款页，有倒计时，付款前核对总价")
    if send:
        push(title, body, click=best["url"], priority="urgent" if urgent else "high")
    sent[key] = {"price": best["price"], "at": datetime.now().isoformat(timespec="minutes")}
    log(f"已推送 {title}")
    return True


def _stopover_message(entry, best, passenger):
    so = best["stopover"]
    a, b = so["legs"]
    title = (f"✈️ {entry['origin']}→{so['city']}→{entry['destination']} {best['date'][5:]} "
             f"A${best['price']:.0f}（理想价 {entry['max_price']:.0f}）")
    same_port = "" if a["to"] == b["from"] else f"（到达 {a['to']}，下一段从 {b['from']} 出发，不同机场）"

    def leg_line(n, x):
        return (f"第{n}段 {x['from']}→{x['to']} {x['depart'][5:]} 起飞 → {x['arrive'][5:]} 到达，"
                f"{x['airlines']} [{x['flights']}]，A${x['price']:.0f}，"
                f"{'含托运' if x['checked_baggage'] else '仅手提'}\n{x['url']}")

    body = (f"在 {so['city']} 停留 {so['hours']:.0f} 小时{same_port}，全程 {best['duration']}\n\n"
            f"{leg_line(1, a)}\n\n{leg_line(2, b)}\n\n"
            "这是两张独立的单程票：第一段延误误了第二段，航空公司不负责；"
            "行李要在停留城市取出、重新托运；入境要看护照对该国的免签/落地签规定。\n"
            f"下单：先订第一段（点开本通知），再用上面第二个链接订第二段，乘机人都勾 {passenger}。")
    return title, body, a["url"]


def maybe_daily_report():
    """到了 report_hour 且今天还没发，就发每日价格报告。Mac 那个点在睡眠的话，醒来后补发一次。"""
    if not CLOUD:
        return                      # 日报只由云端发：Mac 常睡眠，本机历史不全，发出来会是空图
    hour = watchlist.load()["settings"].get("report_hour", 21)
    now = datetime.now()
    if hour < 0 or now.hour < hour:
        return
    state = load_json(STATE, {})
    today = now.date().isoformat()
    if state.get("report_sent") == today:
        return
    try:
        import report
        lines = report.build(now)
        if lines:
            report.push_report(f"📈 机票日报 {now:%m-%d}", "\n".join(lines))
            log("已推送每日价格报告")
    except Exception as e:
        log(f"每日报告失败: {type(e).__name__}: {e}")
        return
    state = load_json(STATE, {})
    state["report_sent"] = today
    save_json(STATE, state)


def launch_prepare(entry):
    """价格达标且开了「自动准备下单页」：另起进程在 Mac 上把下单页第 1 页填好、停在 Next 前。

    独立进程，因为它要开着浏览器等你最多 30 分钟，不能卡住盯价主循环。
    prepare.py 从 state.json 读这一班，所以调用前最新结果要已写进 state（见 check_once 的顺序）。
    """
    import subprocess
    log(f"启动自动准备下单页 {entry['origin']}→{entry['destination']}")
    subprocess.Popen([sys.executable, os.path.join(HERE, "prepare.py"), "--auto", entry["id"]],
                     cwd=HERE, stdout=open(os.path.join(HERE, "prepare.log"), "a"),
                     stderr=subprocess.STDOUT, start_new_session=True)


SNIPE_LEAD_MIN = 1        # 提前 1 分钟开始
SNIPE_WINDOW_MIN = 20     # 到点后持续 20 分钟
SNIPE_EVERY_SEC = 45


def due_snipes():
    """到了抢票时间窗口、且这次还没抢过的航线。"""
    data = watchlist.load()
    state = load_json(STATE, {})
    done = state.get("snipes_done", {})
    now = datetime.now()
    out = []
    for e in data["entries"]:
        s = e.get("snipe_at")
        if not s or not e.get("enabled", True) or done.get(e["id"]) == s or e.get("transfer_pref") == "stopover":
            continue
        t = datetime.fromisoformat(s)
        if t - timedelta(minutes=SNIPE_LEAD_MIN) <= now <= t + timedelta(minutes=SNIPE_WINDOW_MIN):
            out.append(e)
    return out


def run_snipe(entry):
    """定时盯价：到点前后每 45 秒实搜一次，一达标立刻加急推送（你手动下单）。"""
    s = entry["snipe_at"]
    end = datetime.fromisoformat(s) + timedelta(minutes=SNIPE_WINDOW_MIN)
    route = f"{entry['origin']}→{entry['destination']}"
    log(f"⏰ 定时盯价开始 {route}（到 {end:%H:%M}）")
    try:
        push(f"⏰ 开始定时盯价 {route}",
             f"{entry['from']}~{entry['to']}，理想价 A${entry['max_price']:.0f}，每 45 秒查一次，持续到 {end:%H:%M}。"
             "达标会立刻再推一条，准备好手机。", priority="default")
    except Exception as e:
        log(f"推送失败: {e}")

    outcome = "窗口内没出现达标价格"
    passenger = watchlist.load()["settings"].get("passenger_name", "乘机人")
    while datetime.now() <= end:
        try:
            best = check_entry(entry).get("best")
            log(f"⏰ {route} 当前最低 " + (f"A${best['price']} {best['date']}" if best else "无"))
            sent = load_json(SENT, {})
            if maybe_notify(entry, best, sent, passenger=passenger, urgent=True):
                save_json(SENT, sent)
                if entry.get("auto_prepare"):
                    st = load_json(STATE, {})
                    st.setdefault("entries", {}).setdefault(entry["id"], {})["best"] = best
                    save_json(STATE, st)
                    launch_prepare(entry)
                outcome = f"已推送 A${best['price']}"
                break
        except Exception as e:
            log(f"⏰ 定时盯价查价失败: {type(e).__name__}: {e}")
        time.sleep(SNIPE_EVERY_SEC)

    state = load_json(STATE, {})
    state.setdefault("snipes_done", {})[entry["id"]] = s
    save_json(STATE, state)
    log(f"⏰ 定时盯价结束 {route}：{outcome}")
    if outcome.startswith("窗口内"):
        try:
            push(f"⏰ 定时盯价结束 {route}", outcome, priority="default")
        except Exception:
            pass


def check_once(verbose=False):
    data = watchlist.load()
    state = load_json(STATE, {})
    sent = load_json(SENT, {})
    results = state.setdefault("entries", {})

    if not trip_healthy():
        msg = "tripcom-mcp 没响应（127.0.0.1:18061），本轮跳过"
        log(msg)
        state["last_error"] = msg
        today = date.today().isoformat()
        if state.get("service_alert_day") != today:
            try:
                push("⚠️ 机票盯价暂停", msg + "\n检查: launchctl list | grep tripcom", priority="default")
                state["service_alert_day"] = today
            except Exception as e:
                log(f"推送失败: {e}")
        save_json(STATE, state)
        return state

    state.pop("last_error", None)
    for entry in data["entries"]:
        if not entry.get("enabled", True):
            continue
        route = f"{entry['origin']}→{entry['destination']} {entry['from']}~{entry['to']}"
        try:
            r = check_entry(entry)
        except Exception as e:
            log(f"{route} 查价失败: {type(e).__name__}: {e}")
            results[entry["id"]] = {**results.get(entry["id"], {}), "error": str(e),
                                    "checked_at": datetime.now().isoformat(timespec="minutes")}
            continue
        record_history(entry, r["per_day"])
        best = r.get("best")
        if verbose or best:
            log(f"{route} 最低 " + (f"A${best['price']} {best['date']} {best['airlines']}"
                                   if best else "无符合条件的航班"))
        results[entry["id"]] = {
            "checked_at": datetime.now().isoformat(timespec="minutes"),
            "status": r["status"], "best": best,
            "per_day": r["per_day"], "calendar": r["calendar"], "by_type": r["by_type"],
        }
        try:
            # 云端在跑时，推送由云端发；Mac 只负责「自动备好下单页」，自己不再推，免得重复
            if maybe_notify(entry, best, sent, passenger=data["settings"].get("passenger_name", "乘机人"),
                            send=CLOUD or not cloud_alive()):
                save_json(SENT, sent)
                if entry.get("auto_prepare") and not CLOUD and not best.get("stopover"):
                    save_json(STATE, state)   # prepare.py 从 state 读这一班，必须先落盘
                    launch_prepare(entry)
        except Exception as e:
            log(f"推送失败: {e}")
        time.sleep(5)

    known = {e["id"] for e in data["entries"]}
    for k in list(results):
        if k not in known:
            del results[k]
    state["last_cycle"] = datetime.now().isoformat(timespec="seconds")
    save_json(STATE, state)
    save_json(SENT, sent)
    return state


# ── 手机端同步（GitHub 私有仓库）────────────────────────────

CLOUD_PULL_EVERY = 300
_cloud_err_day = [None]


def _cloud_fail(what, e):
    log(f"手机同步{what}失败: {e}")
    today = date.today().isoformat()
    if _cloud_err_day[0] != today:      # 同步坏了每天只提醒一次，免得刷屏
        _cloud_err_day[0] = today
        try:
            push("⚠️ 机票盯价：手机同步出错", f"{what}失败：{e}\n本机盯价照常运行，只是手机上改的清单暂时同步不过来。",
                 priority="default")
        except Exception:
            pass


def cloud_pull():
    try:
        import sync
        r = sync.sync_watchlist()
        _last_pull_ok[0] = time.time()
        if r == "pulled":
            log("已采用手机上改的航线清单")
    except Exception as e:
        _cloud_fail("拉取清单", e)


def cloud_push_status():
    if cloud_alive():
        return                      # 云端在写 status.json，Mac 不抢
    try:
        import sync
        sync.push_status(load_json(STATE, {}), {"trip_ok": trip_healthy()})
    except Exception as e:
        _cloud_fail("上传结果", e)


# ── 常驻 ────────────────────────────────────────────────────

def heartbeat(**kv):
    hb = load_json(HEARTBEAT, {})
    hb.update(kv, pid=os.getpid(), alive_at=datetime.now().isoformat(timespec="seconds"))
    save_json(HEARTBEAT, hb)


def running_pid():
    try:
        pid = int(open(PIDFILE).read())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def main_loop():
    other = running_pid()
    if other and other != os.getpid():
        log(f"已有盯价进程在跑 (pid {other})，退出")
        sys.exit(0)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log("机票盯价启动")

    while True:
        cloud_pull()
        sig = watchlist.signature()
        try:
            check_once()
            heartbeat(last_ok=datetime.now().isoformat(timespec="seconds"))
        except Exception as e:
            log(f"本轮异常: {type(e).__name__}: {e}")
            heartbeat(last_error=f"{type(e).__name__}: {e}")
        cloud_push_status()
        interval = watchlist.load()["settings"]["interval_minutes"] * 60
        deadline = time.time() + interval
        next_pull = time.time() + CLOUD_PULL_EVERY
        while time.time() < deadline:
            time.sleep(15)
            heartbeat()
            maybe_daily_report()
            for e in due_snipes():
                run_snipe(e)
                cloud_push_status()
            if time.time() >= next_pull:
                cloud_pull()          # 手机上改了清单，最多 5 分钟就会被发现并立即查价
                next_pull = time.time() + CLOUD_PULL_EVERY
            if os.path.exists(CHECK_NOW) or watchlist.signature() != sig:
                try:
                    os.remove(CHECK_NOW)
                except OSError:
                    pass
                log("清单有变化或手动触发，立即查价")
                break


def health():
    hb = load_json(HEARTBEAT, {})
    st = load_json(STATE, {})
    pid = running_pid()
    print(f"进程: {'✅ 在跑 pid ' + str(pid) if pid else '❌ 没在跑'}")
    print(f"心跳: {hb.get('alive_at', '无')}")
    print(f"上一轮查价: {st.get('last_cycle', '还没跑过')}")
    print(f"tripcom-mcp: {'✅' if trip_healthy() else '❌ 没响应'}")
    if st.get("last_error"):
        print(f"⚠️ {st['last_error']}")
    wl = watchlist.load()
    for e in wl["entries"]:
        r = st.get("entries", {}).get(e["id"], {})
        b = r.get("best")
        mark = "⏸" if not e["enabled"] else ("🔥" if b and e["max_price"] and b["price"] <= e["max_price"] else "·")
        print(f" {mark} {e['origin']}→{e['destination']} {e['from']}~{e['to']} 理想价 {e['max_price']}  "
              + (f"当前最低 A${b['price']} ({b['date']} {b['airlines']})" if b else r.get("error", "暂无结果")))


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--once":
        check_once(verbose=True)
    elif arg == "--health":
        health()
    elif arg == "--test-push":
        push("✈️ 机票盯价测试推送", "收到这条说明手机推送配好了。", priority="default")
        print("已发送")
    else:
        main_loop()
