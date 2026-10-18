"""Trip.com 机票搜索。

思路：不去硬拼带签名的接口请求，而是用真实浏览器打开搜索页，
拦截它自己发出的两个内部接口：

  - FlightListSearchSSE   航班主列表（SSE 流，一次推完整个 itineraryList）
  - GetLowPriceInCalender  前后若干天的最低价日历

这样所有 token / 指纹 / AB 实验参数都由页面自己生成，比手工复刻请求稳得多。
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

log = logging.getLogger("tripcom.flights")

DOMAIN = "au.trip.com"

CABIN_CODE = {
    "economy": "y",
    "premium_economy": "s",
    "business": "c",
    "first": "f",
}

SSE_ENDPOINT = "FlightListSearchSSE"
CAL_ENDPOINT = "GetLowPriceInCalender"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SearchError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# URL 构造
# --------------------------------------------------------------------------


def build_search_url(
    origin,
    destination,
    depart_date,
    return_date=None,
    adults=1,
    children=0,
    infants=0,
    cabin="economy",
    currency="AUD",
    locale="en-AU",
    domain=DOMAIN,
):
    for label, value in (("origin", origin), ("destination", destination)):
        if not value or not re.fullmatch(r"[A-Za-z]{3}", value.strip()):
            raise SearchError(f"{label} 必须是 3 位机场/城市代码，收到: {value!r}")
    for label, value in (("depart_date", depart_date), ("return_date", return_date)):
        if value and not _DATE_RE.match(value):
            raise SearchError(f"{label} 必须是 YYYY-MM-DD 格式，收到: {value!r}")
    if cabin not in CABIN_CODE:
        raise SearchError(f"cabin 只能是 {list(CABIN_CODE)} 之一，收到: {cabin!r}")

    params = {
        "dcity": origin.strip().lower(),
        "acity": destination.strip().lower(),
        "ddate": depart_date,
        "triptype": "rt" if return_date else "ow",
        "class": CABIN_CODE[cabin],
        "quantity": max(1, int(adults)),
        "locale": locale,
        "curr": currency,
    }
    if return_date:
        params["rdate"] = return_date
    if children:
        params["childqty"] = int(children)
    if infants:
        params["infantqty"] = int(infants)

    return f"https://{domain}/flights/showfarefirst?{urlencode(params)}"


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def _parse_sse(raw):
    """SSE 报文 -> 事件列表。格式是若干个 `data:{json}` 块。"""
    events = []
    for chunk in raw.split("data:"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            events.append(json.loads(chunk))
        except json.JSONDecodeError:
            continue
    return events


def _fmt_duration(minutes):
    if minutes is None:
        return None
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _dt(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _short(value):
    d = _dt(value)
    return d.strftime("%Y-%m-%d %H:%M") if d else value


def _journey_record(journey, airline_names):
    sections = journey.get("transSectionList") or []
    if not sections:
        return None

    segments = []
    for sec in sections:
        info = sec.get("flightInfo") or {}
        dep = sec.get("departPoint") or {}
        arr = sec.get("arrivePoint") or {}
        craft = info.get("craftInfo") or {}
        code = info.get("airlineCode")
        segments.append(
            {
                "flight_no": info.get("flightNo"),
                "airline": airline_names.get(code, code),
                "airline_code": code,
                "from": dep.get("airportCode"),
                "from_city": dep.get("cityName"),
                "from_terminal": dep.get("terminal") or None,
                "to": arr.get("airportCode"),
                "to_city": arr.get("cityName"),
                "to_terminal": arr.get("terminal") or None,
                "depart": _short(sec.get("departDateTime")),
                "arrive": _short(sec.get("arriveDateTime")),
                "duration_min": sec.get("duration"),
                "aircraft": craft.get("name") or craft.get("shortName"),
            }
        )

    layovers = []
    for prev, nxt in zip(sections, sections[1:]):
        a, d = _dt(prev.get("arriveDateTime")), _dt(nxt.get("departDateTime"))
        if a and d:
            mins = int((d - a).total_seconds() // 60)
            layovers.append(
                {
                    "airport": (prev.get("arrivePoint") or {}).get("airportCode"),
                    "city": (prev.get("arrivePoint") or {}).get("cityName"),
                    "minutes": mins,
                    "duration": _fmt_duration(mins),
                }
            )

    flags = set(journey.get("journeyFlags") or [])
    return {
        "depart": segments[0]["depart"],
        "arrive": segments[-1]["arrive"],
        "day_count": journey.get("dayCount") or 0,
        "duration_min": journey.get("duration"),
        "duration": _fmt_duration(journey.get("duration")),
        "stops": max(0, len(sections) - 1),
        "segments": segments,
        "layovers": layovers,
        "airlines": sorted({s["airline"] for s in segments if s["airline"]}),
        "luggage_through": "LUGGAGE_DIRECT" in flags,
    }


def _itinerary_record(itin, airline_names):
    journeys = [_journey_record(j, airline_names) for j in (itin.get("journeyList") or [])]
    journeys = [j for j in journeys if j]
    policies = itin.get("policies") or []
    if not journeys or not policies:
        return None

    # 同一行程可能挂多个价格方案，取最便宜的那个作为代表
    best = min(policies, key=lambda p: (p.get("price") or {}).get("totalPrice") or 1e12)
    price = best.get("price") or {}
    pflags = set(best.get("policyFlags") or [])
    tags = {t.get("key") for t in (best.get("tagList") or [])}
    grades = {
        g.get("gradeMultilingual")
        for g in (best.get("gradeInfoList") or [])
        if g.get("gradeMultilingual")
    }

    # 分开出票：各段是独立机票，退改签和行李各算各的
    separate_tickets = bool(
        {"TRANSFER_SPLICING_MULTIPLE_TICKETS", "NON_INTERLINE_FLIGHT", "MULTI_PU"} & pflags
        or "TRANSFER_MULTIPLE_TICKET" in tags
    )
    # 只有真的存在中转时，「分开出票」才等于「要自己提行李重新值机」
    has_connection = any(j["stops"] > 0 for j in journeys)
    self_transfer = separate_tickets and has_connection

    return {
        "price_total": price.get("totalPrice"),
        "price_per_adult": price.get("averagePrice"),
        "tax": price.get("totalTax"),
        "cabin": ", ".join(sorted(grades)) or None,
        "seats_left": best.get("seatCount"),
        "low_seats": "LOW_SEAT_COUNT" in tags,
        "checked_baggage": "FREE_CHECKED_BAGGAGE" in pflags or "FREE_CHECKED_BAGGAGE" in tags,
        "carry_on_only": "FREE_CHECKED_BAGGAGE" not in pflags and "FREE_CARRY_ON_BAGGAGE" in pflags,
        "self_transfer": self_transfer,
        "separate_tickets": separate_tickets,
        "budget_airline": "LCC" in pflags,
        "fare_options": len(policies),
        "outbound": journeys[0],
        "inbound": journeys[1] if len(journeys) > 1 else None,
        # 顶层放一份，方便直接排序/展示
        "stops": journeys[0]["stops"],
        "duration_min": journeys[0]["duration_min"],
        "duration": journeys[0]["duration"],
        "depart": journeys[0]["depart"],
        "arrive": journeys[0]["arrive"],
        "airlines": journeys[0]["airlines"],
    }


def _itinerary_key(itin):
    """行程指纹：航班号 + 起降时刻。

    SSE 会分批重复推送全量列表（同一趟航班可能带不同价格方案再来一次），
    所以不能用 uniqueId 之类每次都变的字段来去重。
    """
    parts = []
    for journey in itin.get("journeyList") or []:
        for sec in journey.get("transSectionList") or []:
            parts.append(
                (
                    (sec.get("flightInfo") or {}).get("flightNo"),
                    sec.get("departDateTime"),
                    sec.get("arriveDateTime"),
                )
            )
    return tuple(parts)


def parse_flight_list(events):
    """把 SSE 事件合并成航班列表（去重，同一行程保留最低价）。"""
    by_key, airline_names, meta = {}, {}, {}

    for ev in events:
        for al in ev.get("airlineList") or []:
            if al.get("code"):
                airline_names[al["code"]] = al.get("name") or al["code"]

        basic = ev.get("basicInfo") or {}
        if basic.get("recordCount"):
            meta["record_count"] = max(meta.get("record_count", 0), basic["recordCount"])
        if basic.get("currency"):
            meta["currency"] = basic["currency"]
        lowest = basic.get("lowestPrice") or {}
        if lowest.get("totalPrice") is not None:
            meta["lowest_price"] = min(
                meta.get("lowest_price", float("inf")), lowest["totalPrice"]
            )

        for itin in ev.get("itineraryList") or []:
            key = _itinerary_key(itin)
            if not key:
                continue
            rec = _itinerary_record(itin, airline_names)
            if not rec:
                continue
            prev = by_key.get(key)
            if prev is None or (rec["price_total"] or 1e12) < (prev["price_total"] or 1e12):
                by_key[key] = rec

    itineraries = list(by_key.values())

    # airlineList 可能在后面的事件才到，补一次名字
    for rec in itineraries:
        for journey in (rec["outbound"], rec["inbound"]):
            if not journey:
                continue
            for seg in journey["segments"]:
                seg["airline"] = airline_names.get(seg["airline_code"], seg["airline"])
            journey["airlines"] = sorted({s["airline"] for s in journey["segments"] if s["airline"]})
        rec["airlines"] = rec["outbound"]["airlines"]

    return itineraries, meta


def parse_price_calendar(payload, around=None, window_days=21):
    """GetLowPriceInCalender -> [{date, price}]。

    接口一次会给半年（~180 天）的数据，这里只保留出发日前后 window_days 天，
    免得把整个上下文塞满。
    """
    items = payload.get("lowPriceInCalenderDtoInfoList") or []
    # 往返查询时去程/回程各有一份日历，只取去程那条腿
    if any(i.get("segmentNo") not in (None, 1) for i in items):
        items = [i for i in items if i.get("segmentNo") in (None, 1)]

    by_date = {}
    for item in items:
        ts, price = item.get("dDate"), item.get("currencyPrice")
        # -1 是「当天无价」的哨兵值
        if ts is None or not isinstance(price, (int, float)) or price <= 0:
            continue
        date = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        if date not in by_date or price < by_date[date]:
            by_date[date] = price

    out = [{"date": d, "price": p} for d, p in sorted(by_date.items())]
    if around and _DATE_RE.match(around):
        anchor = datetime.strptime(around, "%Y-%m-%d")
        out = [
            c
            for c in out
            if abs((datetime.strptime(c["date"], "%Y-%m-%d") - anchor).days) <= window_days
        ]
    return out


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------


def _count_itineraries(raw_bodies):
    """当前已经收到多少条行程（用来判断流式推送是否结束）。"""
    total = 0
    for raw in raw_bodies:
        for ev in _parse_sse(raw):
            total += len(ev.get("itineraryList") or [])
    return total


def fetch_search(ctx, url, wait_seconds=45, want_calendar=True):
    """打开搜索页，拦截 SSE 与价格日历，返回 (events, calendar_payload)。"""
    captured = {"sse": [], "cal": None}
    page = ctx.new_page()

    def on_response(resp):
        u = resp.url
        try:
            if SSE_ENDPOINT in u:
                captured["sse"].append(resp.body().decode("utf-8", "ignore"))
            elif want_calendar and CAL_ENDPOINT in u and captured["cal"] is None:
                captured["cal"] = resp.json()
        except Exception as exc:  # noqa: BLE001 - 单个接口读失败不该让整次搜索崩掉
            log.debug("读取响应失败 %s: %s", u[:80], exc)

    page.on("response", on_response)
    try:
        log.info("打开搜索页: %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)

        # 结果是流式推的，而且第一个 SSE 事件常常是空的「搜索中」。
        # 所以要按「已经解析出多少条行程」来判断，而不是按「收到没收到字节」。
        deadline = time.time() + wait_seconds
        stable_since = None
        last_count = 0
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            count = _count_itineraries(captured["sse"])
            if count == 0:
                continue
            if count != last_count:
                last_count, stable_since = count, time.time()
                log.info("已收到 %d 条行程，等待后续批次…", count)
            elif time.time() - stable_since > 4:
                break  # 连续 4 秒没有新数据，认为推送结束

        if last_count == 0:
            title = ""
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                pass
            raise SearchError(
                f"{wait_seconds}s 内没等到航班数据"
                f"（收到 {len(captured['sse'])} 个 SSE 响应，页面标题: {title!r}）。"
                "可能是被风控拦了、该日期无航班，或页面结构变了。"
            )
        log.info("推送结束，共 %d 条行程", last_count)
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

    events = []
    for raw in captured["sse"]:
        events.extend(_parse_sse(raw))
    return events, captured["cal"]


def search_flights(worker, *, sort="price", limit=20, max_stops=None,
                   wait_seconds=45, **criteria):
    """对外主入口：返回结构化搜索结果。"""
    url = build_search_url(**criteria)

    events, cal_payload = worker.submit(
        lambda ctx: fetch_search(ctx, url, wait_seconds=wait_seconds),
        timeout=wait_seconds + 90,
    )
    flights, meta = parse_flight_list(events)
    if not flights:
        raise SearchError("接口有响应但没解析出航班，可能该日期无航班，或 Trip.com 改了数据结构。")

    # 完整性自证：服务端说有多少条，我们真解析出多少条。
    # 漏抓时必须能自己说出来，否则「没报错」会被误当成「拿到了全场最低价」。
    parsed = len(flights)
    declared = meta.get("record_count")
    complete = declared is None or parsed >= declared

    if max_stops is not None:
        flights = [f for f in flights if f["stops"] <= int(max_stops)]

    keys = {
        "price": lambda f: (f["price_total"] if f["price_total"] is not None else 1e12),
        "duration": lambda f: (f["duration_min"] or 1e12, f["price_total"] or 1e12),
        "depart": lambda f: (f["depart"] or "", f["price_total"] or 1e12),
    }
    if sort not in keys:
        raise SearchError(f"sort 只能是 {list(keys)} 之一，收到: {sort!r}")
    flights.sort(key=keys[sort])

    total = len(flights)
    flights = flights[: max(1, int(limit))]

    result = {
        "query": {
            **criteria,
            "sort": sort,
            "max_stops": max_stops,
            "searched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "url": url,
        },
        "currency": meta.get("currency") or criteria.get("currency", "AUD"),
        "total_found": declared if declared is not None else parsed,
        "parsed": parsed,
        "matched": total,
        "returned": len(flights),
        # complete=True 才可当「全场最低价」采信；False 说明只解析到一部分，只能算上限
        "complete": complete,
        "data_type": "actual" if complete else "upper-bound",
        "lowest_price": meta.get("lowest_price"),
        "flights": flights,
    }
    # 往返查询时 Trip.com 的列表页只列去程，价格已经是往返总价，回程要在下一步才选。
    # 这点必须讲清楚，否则很容易把「往返总价」误读成「单程价」。
    if criteria.get("return_date") and not any(f.get("inbound") for f in flights):
        result["round_trip_note"] = (
            "价格为往返总价；Trip.com 列表页只展示去程航班，具体回程班次需在其页面上第二步选择。"
        )
    if cal_payload:
        cal = parse_price_calendar(cal_payload, around=criteria.get("depart_date"))
        if cal:
            result["price_calendar"] = cal
    return result


# --------------------------------------------------------------------------
# 文本摘要（给人看的那一版）
# --------------------------------------------------------------------------


def _leg_line(leg):
    """一条航段腿的紧凑描述：时长 起飞→降落 航司 [航班号] 经停。"""
    arrive = leg["arrive"][-5:]
    if leg["day_count"]:
        arrive += f"(+{leg['day_count']})"
    if leg["stops"] == 0:
        route = "直飞"
    else:
        vias = "/".join(l["airport"] for l in leg["layovers"] if l["airport"])
        waits = "/".join(l["duration"] for l in leg["layovers"] if l.get("duration"))
        route = f"{leg['stops']}转" + (f" 经{vias}" if vias else "") + (f" 候机{waits}" if waits else "")
    flight_nos = " ".join(s["flight_no"] for s in leg["segments"])
    return (
        f"{leg['duration']:<8} {leg['depart'][-5:]}→{arrive:<10} "
        f"{'/'.join(leg['airlines'])} [{flight_nos}]  {route}"
    )


def format_summary(result, max_rows=12):
    q = result["query"]
    cur = result["currency"]
    lines = [
        f"{q['origin'].upper()} → {q['destination'].upper()}  {q['depart_date']}"
        + (f"  返程 {q['return_date']}" if q.get("return_date") else "")
        + f"  |  {result['total_found']} 个结果，按{'价格' if q['sort']=='price' else q['sort']}排序",
    ]
    if result.get("lowest_price") is not None:
        lines.append(f"最低价 {cur} {result['lowest_price']}")
    if not result.get("complete", True):
        lines.append(
            f"⚠️ 数据不完整：服务端声明 {result['total_found']} 条，只解析到 {result['parsed']} 条。"
            "本次结果只能当价格上限（upper-bound），不可当全场最低价采信。"
        )
    if result.get("round_trip_note"):
        lines.append(f"注意：{result['round_trip_note']}")
    lines.append("")

    for i, f in enumerate(result["flights"][:max_rows], 1):
        marks = []
        if f["checked_baggage"]:
            marks.append("含托运")
        elif f["carry_on_only"]:
            marks.append("仅手提")
        if f["self_transfer"]:
            marks.append("⚠️自行转机")
        elif f.get("separate_tickets"):
            marks.append("分开出票")
        if f["budget_airline"]:
            marks.append("廉航")
        if f["low_seats"] and f["seats_left"]:
            marks.append(f"仅剩{f['seats_left']}座")

        lines.append(
            f"{i:>2}. {cur} {f['price_total']:<8} {_leg_line(f['outbound'])}"
            + (f"  {' · '.join(marks)}" if marks else "")
        )
        if f.get("inbound"):
            lines.append(f"    {'返程':<{len(cur) + 9}}{_leg_line(f['inbound'])}")

    if result.get("price_calendar"):
        cheapest = sorted(result["price_calendar"], key=lambda x: x["price"])[:5]
        lines.append("")
        lines.append("附近日期最低价: " + ", ".join(f"{c['date']} {cur}{c['price']}" for c in cheapest))

    return "\n".join(lines)
