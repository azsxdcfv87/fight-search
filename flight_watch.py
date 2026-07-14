#!/usr/bin/env python3
"""每半小時監控 台北(TPE)→釜山(PUS) 來回機票，最低來回票價 < 門檻時 Slack 標記頻道。

資料來源：Google Flights（透過 fast-flights 取 HTML，內建 parser 已與 Google 改版脫節，
故此處自帶容錯解析器）。價格為該行程的「來回總價」，幣別 TWD。

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
    def stops_label(self) -> str:
        return "直飛" if self.stops == 0 else f"轉{self.stops}次"

    @property
    def duration_label(self) -> str:
        if not self.duration_min:
            return ""
        return f"{self.duration_min // 60}小時{self.duration_min % 60}分"


def _safe(seq, *idx):
    """逐層取 index，任一層失敗回 None，用來扛 Google 陣列結構變動。"""
    cur = seq
    for i in idx:
        try:
            cur = cur[i]
        except (IndexError, TypeError, KeyError):
            return None
    return cur


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


def _fmt_time(t) -> str:
    """Google 時間為 [時, 分] 陣列，轉成 HH:MM。"""
    if isinstance(t, list) and len(t) >= 2 and all(isinstance(x, int) for x in t[:2]):
        return f"{t[0]:02d}:{t[1]:02d}"
    return ""  # 結構不符就留空，不輸出雜訊


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


def fetch() -> list[Itinerary]:
    query = create_query(
        flights=[
            FlightQuery(date=DEPART_DATE, from_airport=FROM_AIRPORT, to_airport=TO_AIRPORT),
            FlightQuery(date=RETURN_DATE, from_airport=TO_AIRPORT, to_airport=FROM_AIRPORT),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=1),
        language="zh-TW",
        currency=CURRENCY,
    )
    html = fetch_flights_html(query)
    return parse_itineraries(html)


def build_slack_payload(itineraries: list[Itinerary]) -> dict:
    route = f"{FROM_AIRPORT}↔{TO_AIRPORT}"
    dates = f"{DEPART_DATE} 去 / {RETURN_DATE} 回"
    if not itineraries:
        return {"text": f":warning: [{route}] {dates} 這次沒抓到任何票價（可能來源改版或暫時無結果）。"}

    cheapest = itineraries[0]
    hit = cheapest.price < THRESHOLD

    top = itineraries[:5]
    lines = []
    for i, it in enumerate(top, 1):
        if it.depart_time and it.arrive_time:
            seg = f"{it.depart_time} → {it.arrive_time}"
        else:
            seg = it.depart_time or it.arrive_time
        extras = " · ".join(x for x in [it.stops_label, it.duration_label, seg] if x)
        lines.append(f"{i}. *NT${it.price:,}* — {it.airline_label}" + (f"（{extras}）" if extras else ""))

    header = (
        f":airplane: <!channel> *[{route}] 出現低於 NT${THRESHOLD:,} 的來回票！*\n"
        f"最低 *NT${cheapest.price:,}* — {cheapest.airline_label}"
        if hit
        else f":airplane: [{route}] 來回機票監控\n目前最低 *NT${cheapest.price:,}* — {cheapest.airline_label}"
    )

    gf_url = (
        "https://www.google.com/travel/flights?q="
        f"Flights%20to%20{TO_AIRPORT}%20from%20{FROM_AIRPORT}%20on%20{DEPART_DATE}%20"
        f"through%20{RETURN_DATE}"
    )
    body = "\n".join(
        [header, f"_{dates}・經濟艙・來回總價（TWD）_", "", *lines, "", f"<{gf_url}|在 Google Flights 開啟>"]
    )
    payload = {"text": body}
    return payload


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
        itineraries = fetch()
    except Exception as e:  # 抓取/解析失敗也推一則，避免默默失效
        post_to_slack({"text": f":x: 機票監控執行失敗：{type(e).__name__}: {e}"})
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    payload = build_slack_payload(itineraries)
    post_to_slack(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
