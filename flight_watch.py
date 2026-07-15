#!/usr/bin/env python3
"""監控 台北(TPE)→釜山(PUS) 來回機票，最低來回票價 < 門檻時 Slack 標記頻道。

資料來源：Google Flights（透過 fast-flights 取 HTML，內建 parser 已與 Google 改版脫節，
故此處自帶容錯解析器）。

顯示：
  - 去程 TPE→PUS：以「來回總價」排序（Google 來回搜尋只回傳去程班次 + 來回總價）。
  - 回程 PUS→TPE：另打一次單程搜尋，補上回程班次的起飛/抵達時間（參考時段）。
  - 每家航空標註 廉航 / 傳統，底部附行李提示（清單頁無逐筆行李資料，只能依航司類型提示）。

環境變數：
  SLACK_WEBHOOK_URL   Slack Incoming Webhook（未設定時走 dry-run，只印在 stdout）
  PRICE_THRESHOLD     觸發標記頻道的門檻，預設 8000
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import dataclass

from fast_flights import create_query, fetch_flights_html, FlightQuery, Passengers
from selectolax.lexbor import LexborHTMLParser

# ---- 搜尋條件 -------------------------------------------------------------
DEPART_DATE = "2027-03-03"
RETURN_DATE = "2027-03-07"
FROM_AIRPORT = "TPE"   # 桃園
TO_AIRPORT = "PUS"     # 釜山金海
CURRENCY = "TWD"
THRESHOLD = int(os.environ.get("PRICE_THRESHOLD", "8000"))
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

# 廉航 / 傳統航空 關鍵字（用於判斷行李慣例，非即時資料）
LCC_KEYWORDS = [
    "虎航", "Tigerair", "樂桃", "Peach", "濟州", "Jeju", "真航空", "Jin Air",
    "釜山航空", "Air Busan", "德威", "T'way", "Tway", "易斯達", "Eastar",
    "酷航", "Scoot", "亞洲航空", "AirAsia", "越捷", "VietJet", "春秋", "宿霧",
    "Cebu", "泰國獅", "首爾航空",
]
FSC_KEYWORDS = [
    "中華航空", "China Airlines", "長榮", "EVA", "大韓", "Korean Air",
    "韓亞", "Asiana", "國泰", "Cathay", "日本航空", "JAL", "全日空", "ANA",
    "星宇", "STARLUX", "泰國航空", "新加坡航空", "Singapore", "菲律賓航空",
]


@dataclass
class Itinerary:
    airlines: list[str]
    price: int
    stops: int
    depart_time: str = ""
    arrive_time: str = ""
    duration_min: int | None = None

    @property
    def airline_label(self) -> str:
        return " / ".join(self.airlines) if self.airlines else "未知航空"

    @property
    def carrier_tag(self) -> str:
        name = self.airline_label
        if any(k in name for k in LCC_KEYWORDS):
            return "廉航"
        if any(k in name for k in FSC_KEYWORDS):
            return "傳統"
        return ""

    @property
    def stops_label(self) -> str:
        return "直飛" if self.stops == 0 else f"轉{self.stops}次"

    @property
    def duration_label(self) -> str:
        if not self.duration_min:
            return ""
        return f"{self.duration_min // 60}小時{self.duration_min % 60}分"

    @property
    def time_label(self) -> str:
        if self.depart_time and self.arrive_time:
            return f"{self.depart_time}→{self.arrive_time}"
        return self.depart_time or self.arrive_time


def _safe(seq, *idx):
    """逐層取 index，任一層失敗回 None，用來扛 Google 陣列結構變動。"""
    cur = seq
    for i in idx:
        try:
            cur = cur[i]
        except (IndexError, TypeError, KeyError):
            return None
    return cur


def _fmt_time(t) -> str:
    """Google 時間為 [時, 分] 陣列；整點時可能只給 [時] → 補 :00。"""
    if isinstance(t, list) and t and all(isinstance(x, int) for x in t[:2]):
        hour = t[0]
        minute = t[1] if len(t) >= 2 else 0
        return f"{hour:02d}:{minute:02d}"
    return ""


def _parse_entry(k) -> Itinerary | None:
    price = _safe(k, 1, 0, 1)
    if not isinstance(price, (int, float)):
        return None  # 無價格（如某些 codeshare / 售罄）直接跳過
    flight = _safe(k, 0)
    airlines = _safe(flight, 1) or []
    if not isinstance(airlines, list):
        airlines = [str(airlines)]
    legs = _safe(flight, 2) or []
    stops = max(len(legs) - 1, 0)
    first, last = (legs[0] if legs else None), (legs[-1] if legs else None)
    durations = [_safe(leg, 11) for leg in legs]
    total = sum(d for d in durations if isinstance(d, (int, float)))
    return Itinerary(
        airlines=[a for a in airlines if isinstance(a, str)],
        price=int(price),
        stops=stops,
        depart_time=_fmt_time(_safe(first, 8)),
        arrive_time=_fmt_time(_safe(last, 10)),
        duration_min=int(total) or None,
    )


def parse_itineraries(html: str) -> list[Itinerary]:
    parser = LexborHTMLParser(html)
    script = parser.css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("找不到 Google Flights 資料節點（可能被擋或改版）")
    text = script.text()
    payload = json.loads(text.split("data:", 1)[1].rsplit(",", 1)[0])

    results: list[Itinerary] = []
    # best 清單 payload[3][0] 與 其他 清單 payload[2][0] 都要看
    for path in ((3, 0), (2, 0)):
        bucket = _safe(payload, *path) or []
        for k in bucket:
            it = _parse_entry(k)
            if it is not None:
                results.append(it)
    # 依 (航空, 價格, 出發時間) 去重
    seen, deduped = set(), []
    for it in sorted(results, key=lambda x: x.price):
        key = (it.airline_label, it.price, it.depart_time)
        if key not in seen:
            seen.add(key)
            deduped.append(it)
    return deduped


def fetch_roundtrip() -> list[Itinerary]:
    """來回搜尋 → 去程班次（價格為來回總價）。"""
    query = create_query(
        flights=[
            FlightQuery(date=DEPART_DATE, from_airport=FROM_AIRPORT, to_airport=TO_AIRPORT),
            FlightQuery(date=RETURN_DATE, from_airport=TO_AIRPORT, to_airport=FROM_AIRPORT),
        ],
        trip="round-trip", seat="economy", passengers=Passengers(adults=1),
        language="zh-TW", currency=CURRENCY,
    )
    return parse_itineraries(fetch_flights_html(query))


def fetch_oneway(date: str, frm: str, to: str) -> list[Itinerary]:
    """單程搜尋 → 該方向班次（價格為單程價，主要拿來補時段）。"""
    query = create_query(
        flights=[FlightQuery(date=date, from_airport=frm, to_airport=to)],
        trip="one-way", seat="economy", passengers=Passengers(adults=1),
        language="zh-TW", currency=CURRENCY,
    )
    return parse_itineraries(fetch_flights_html(query))


def _fmt_line(it: Itinerary, *, show_price: bool) -> str:
    tag = f"〈{it.carrier_tag}〉" if it.carrier_tag else ""
    extras = " · ".join(x for x in [it.stops_label, it.duration_label, it.time_label] if x)
    head = f"*NT${it.price:,}* — " if show_price else ""
    return f"{head}{it.airline_label}{tag}" + (f"（{extras}）" if extras else "")


def build_slack_payload(outbound: list[Itinerary], inbound: list[Itinerary]) -> dict:
    route = f"{FROM_AIRPORT}↔{TO_AIRPORT}"
    dates = f"{DEPART_DATE} 去 / {RETURN_DATE} 回"
    if not outbound:
        return {"text": f":warning: [{route}] {dates} 這次沒抓到任何票價（可能來源改版或暫時無結果）。"}

    cheapest = outbound[0]
    hit = cheapest.price < THRESHOLD
    ctag = f"〈{cheapest.carrier_tag}〉" if cheapest.carrier_tag else ""

    if hit:
        header = (
            f":airplane: <!channel> *[{route}] 出現低於 NT${THRESHOLD:,} 的來回票！*\n"
            f"最低來回總價 *NT${cheapest.price:,}* — {cheapest.airline_label}{ctag}"
        )
    else:
        header = (
            f":airplane: [{route}] 來回機票監控\n"
            f"目前最低來回總價 *NT${cheapest.price:,}* — {cheapest.airline_label}{ctag}"
        )

    lines = [header, f"_{dates}・經濟艙・1 位成人_", ""]

    lines.append(f":small_orange_diamond: *去程 {FROM_AIRPORT}→{TO_AIRPORT}｜{DEPART_DATE}*（金額為來回總價）")
    for i, it in enumerate(outbound[:5], 1):
        lines.append(f"{i}. {_fmt_line(it, show_price=True)}")

    if inbound:
        lines.append("")
        lines.append(f":small_blue_diamond: *回程 {TO_AIRPORT}→{FROM_AIRPORT}｜{RETURN_DATE}*（參考時段）")
        for it in inbound[:4]:
            lines.append(f"• {_fmt_line(it, show_price=False)}")

    lines.append("")
    lines.append("_※ 行李：廉航票價多為僅手提、託運需另購；傳統航空通常含 1 件託運。實際以訂票頁為準。_")

    gf_url = (
        "https://www.google.com/travel/flights?q="
        f"Flights%20to%20{TO_AIRPORT}%20from%20{FROM_AIRPORT}%20on%20{DEPART_DATE}%20"
        f"through%20{RETURN_DATE}"
    )
    lines.append(f"<{gf_url}|在 Google Flights 開啟>")
    return {"text": "\n".join(lines)}


def post_to_slack(payload: dict) -> None:
    if not SLACK_WEBHOOK_URL:
        print("[dry-run] 未設定 SLACK_WEBHOOK_URL，訊息內容如下：\n")
        print(payload["text"])
        return
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Slack 回應非 200：{resp.status} {resp.read()!r}")
    print("[slack] 已推播")


def main() -> int:
    try:
        outbound = fetch_roundtrip()
        try:
            inbound = fetch_oneway(RETURN_DATE, TO_AIRPORT, FROM_AIRPORT)
        except Exception as e:  # 回程只是補充資訊，失敗不影響主流程
            print(f"WARN: 回程查詢失敗（略過）：{e}", file=sys.stderr)
            inbound = []
    except Exception as e:  # 主抓取失敗也推一則，避免默默失效
        post_to_slack({"text": f":x: 機票監控執行失敗：{type(e).__name__}: {e}"})
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    post_to_slack(build_slack_payload(outbound, inbound))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
