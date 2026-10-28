"""关注清单读写。watchlist.json 是唯一数据源，界面和盯梢进程都只读写它。"""

import json
import os
import re
import secrets
import tempfile
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.environ.get("FW_WATCHLIST", os.path.join(HERE, "watchlist.json"))

DEFAULTS = {
    "origin": "",
    "destination": "",
    "from": "",
    "to": "",
    "max_price": None,          # 理想价 AUD，低于等于它才推送
    "max_stops": 1,             # None = 不限
    "allow_self_transfer": False,
    "need_bag": False,          # True = 只算含托运行李的票
    "adults": 1,
    "enabled": True,
    "auto_prepare": False,      # 达标时在 Mac 上自动把下单页第 1 页填好、停在 Next 前等你点
    "exclude": [],              # 不考虑的出发日
    "depart_slots": [],         # 出发时段 morning/afternoon/evening，空 = 不限
    "promo_code": "",           # 占位时在订票页填的优惠码（当天有活动时用）
    "snipe_at": "",             # 定时盯价：YYYY-MM-DDTHH:MM（悉尼时间），到点前后密集查价、达标立刻加急推送
    # 中转偏好：cheap = 只看最便宜；fast = 越快越好（限总时长/单次等待）；stopover = 在中转城市停留玩一玩
    "transfer_pref": "cheap",
    "max_duration_h": None,     # fast：全程最多几小时（None = 不限）
    "max_layover_h": None,      # fast：每次中转最多等几小时（None = 不限）
    "low_margin": 2,            # 7 天新低推送：要比前低至少便宜多少澳元（0 = 只要更低就推）
    "stopover_city": "",        # stopover：停留城市代码，如 BKK
    "stopover_min_h": 12,       # stopover：至少停几小时
    "stopover_max_h": 48,       # stopover：最多停几小时
    "note": "",
}
SETTINGS_DEFAULTS = {
    "interval_minutes": 60,
    "passenger_name": "乘机人",   # 推送里提醒你下单时勾选的常用旅客
    "report_hour": 21,              # 每日价格报告的推送时间（悉尼时间，0–23；-1 = 不发）
}

_CODE = re.compile(r"^[A-Z]{3}$")


def _iso(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def normalize(entry):
    e = {**DEFAULTS, **{k: v for k, v in entry.items() if k in DEFAULTS or k == "id"}}
    e["id"] = e.get("id") or secrets.token_hex(3)
    e["origin"] = str(e["origin"]).strip().upper()
    e["destination"] = str(e["destination"]).strip().upper()
    e["to"] = e["to"] or e["from"]
    for k in ("origin", "destination"):
        if not _CODE.match(e[k]):
            raise ValueError(f"{k} 必须是 3 位机场代码，收到 {e[k]!r}")
    d1, d2 = _iso(e["from"]), _iso(e["to"])
    if d2 < d1:
        raise ValueError(f"结束日期 {e['to']} 早于开始日期 {e['from']}")
    if (d2 - d1).days > 40:
        raise ValueError("日期区间最多 41 天（价格日历只覆盖前后 3 周）")
    if e["max_price"] in ("", None):
        e["max_price"] = None
    else:
        e["max_price"] = float(e["max_price"])
    if e["max_stops"] in ("", None, "any"):
        e["max_stops"] = None
    else:
        e["max_stops"] = int(e["max_stops"])
    e["adults"] = max(1, int(e["adults"]))
    for k in ("allow_self_transfer", "need_bag", "enabled", "auto_prepare"):
        e[k] = bool(e[k])
    e["exclude"] = sorted({str(x) for x in e.get("exclude") or []})
    e["depart_slots"] = [s for s in SLOTS if s in (e.get("depart_slots") or [])]
    e["promo_code"] = re.sub(r"\s+", "", str(e.get("promo_code") or ""))[:40]
    snipe = str(e.get("snipe_at") or "").strip()
    if snipe:
        try:
            snipe = datetime.fromisoformat(snipe).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            raise ValueError(f"定时抢票时间格式不对：{snipe!r}")
    e["snipe_at"] = snipe

    if e["transfer_pref"] not in TRANSFER_PREFS:
        e["transfer_pref"] = "cheap"
    for k in ("max_duration_h", "max_layover_h"):
        e[k] = None if e[k] in ("", None) else float(e[k])
        if e[k] is not None and e[k] <= 0:
            raise ValueError(f"{k} 必须大于 0")
    try:
        e["low_margin"] = max(0.0, float(e["low_margin"] if e["low_margin"] not in ("", None) else 2))
    except (TypeError, ValueError):
        raise ValueError("新低降幅要填数字（澳元）")
    e["stopover_city"] = str(e["stopover_city"] or "").strip().upper()
    e["stopover_min_h"] = float(e["stopover_min_h"] if e["stopover_min_h"] not in ("", None) else 12)
    e["stopover_max_h"] = float(e["stopover_max_h"] if e["stopover_max_h"] not in ("", None) else 48)
    if e["transfer_pref"] == "stopover":
        if not _CODE.match(e["stopover_city"]):
            raise ValueError(f"中转停留要填停留城市的 3 位代码，收到 {e['stopover_city']!r}")
        if e["stopover_city"] in (e["origin"], e["destination"]):
            raise ValueError("停留城市不能和出发地或目的地相同")
        if not 1 <= e["stopover_min_h"] < e["stopover_max_h"] <= 72:
            raise ValueError("停留时长要在 1–72 小时之间，且最少小于最多")
        if e["snipe_at"]:
            raise ValueError("中转停留每轮要查好几次，做不到每 45 秒一次的定时盯价，请清空定时盯价")
        e["auto_prepare"] = False      # 两张票的组合没法自动备下单页
    return e


TRANSFER_PREFS = ("cheap", "fast", "stopover")

# 同一城市的多个机场：停留城市填城市码，中转记录里是机场码
CITY_AIRPORTS = {
    "BKK": {"BKK", "DMK"}, "KUL": {"KUL", "SZB"}, "TYO": {"NRT", "HND"}, "OSA": {"KIX", "ITM"},
    "SEL": {"ICN", "GMP"}, "SHA": {"PVG", "SHA"}, "BJS": {"PEK", "PKX"}, "TPE": {"TPE", "TSA"},
    "JKT": {"CGK", "HLP"}, "MEL": {"MEL", "AVV"}, "LON": {"LHR", "LGW", "STN", "LTN"},
}


def city_airports(code):
    return CITY_AIRPORTS.get(code, {code})


# 出发时段：起飞时刻（当地时间）落在 [start, end) 小时内
SLOTS = {"morning": (0, 12), "afternoon": (12, 18), "evening": (18, 24)}


def in_slots(depart, slots):
    """depart 形如 '2026-12-06 16:30'。slots 为空表示不限。"""
    if not slots:
        return True
    hour = int(depart[11:13])
    return any(SLOTS[s][0] <= hour < SLOTS[s][1] for s in slots)


def entry_dates(e):
    d, end = _iso(e["from"]), _iso(e["to"])
    out = []
    while d <= end:
        s = d.isoformat()
        if s not in e["exclude"] and d >= date.today():
            out.append(s)
        d += timedelta(days=1)
    return out


def load():
    try:
        with open(PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    data.setdefault("entries", [])
    data["settings"] = {**SETTINGS_DEFAULTS, **(data.get("settings") or {})}
    return data


def save(data):
    data["entries"] = [normalize(e) for e in data.get("entries", [])]
    data["settings"] = {**SETTINGS_DEFAULTS, **(data.get("settings") or {})}
    data["settings"]["interval_minutes"] = max(30, int(data["settings"]["interval_minutes"]))
    for k in ("auto_hold_enabled", "hold_mode", "check_requested_at"):   # 旧版本遗留的设置
        data["settings"].pop(k, None)
    data["settings"]["passenger_name"] = str(data["settings"]["passenger_name"]).strip() or "乘机人"
    data["settings"]["report_hour"] = max(-1, min(23, int(data["settings"]["report_hour"])))
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    fd, tmp = tempfile.mkstemp(dir=HERE, suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)
    return data


def signature():
    try:
        return os.stat(PATH).st_mtime
    except OSError:
        return 0
