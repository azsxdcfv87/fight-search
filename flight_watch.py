#!/usr/bin/env python3
"""監控 台北(TPE)↔釜山(PUS) 來回機票，最低來回合計 <= 門檻時 Slack 標記頻道。

資料來源：Google Flights（透過 fast-flights 取 HTML，內建 parser 已與 Google 改版脫節，
故此處自帶容錯解析器）。

做法：去程、回程各搜一次「單程」，再依航空公司把「同一家的去程 + 回程」配成一筆，
顯示各自起飛→抵達時間與「去價 + 回價 = 來回合計」（價格為全體乘客總價）。
對廉航（一次買單程）這即實付金額；傳統航空的來回套票可能更便宜，以訂票頁為準。

環境變數：
  SLACK_WEBHOOK_URL   Slack Incoming Webhook（未設定時走 dry-run，只印在 stdout）
  PRICE_THRESHOLD     最低來回合計 <= 此值才推播
  ALWAYS_POST         1 → 每次都推；0（預設）→ 只有達門檻才推
  FROM_AIRPORT / TO_AIRPORT / DEPART_DATE / RETURN_DATE / PASSENGERS  可覆蓋搜尋設定
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import dataclass

from fast_flights import create_query, fetch_flights_html, FlightQuery, Passengers
from selectolax.lexbor import LexborHTMLParser

# ═══════════════ 搜尋設定（要改路線 / 日期 / 人數，改這裡即可）═══════════════
# 機場代碼範例：台北桃園 TPE、台北松山 TSA、釜山 PUS、首爾仁川 ICN、
#              東京成田 NRT、大阪關西 KIX、香港 HKG、曼谷 BKK
FROM_AIRPORT = os.environ.get("FROM_AIRPORT", "TPE")        # ① 出發地點（去程從這裡起飛）
TO_AIRPORT   = os.environ.get("TO_AIRPORT",   "PUS")        # ② 回程地點（目的地；回程從這裡飛回）
DEPART_DATE  = os.environ.get("DEPART_DATE",  "2027-03-03")  # ③ 出發日期（去程），格式 YYYY-MM-DD
RETURN_DATE  = os.environ.get("RETURN_DATE",  "2027-03-07")  # ④ 回程日期，        格式 YYYY-MM-DD
PASSENGERS   = int(os.environ.get("PASSENGERS", "2"))       # ⑤ 乘客人數（成人）
# ══════════════════════════════════════════════════════════════════════════════

CURRENCY = "TWD"
THRESHOLD = int(os.environ.get("PRICE_THRESHOLD", "13000"))  # 最低來回合計(全體乘客) <= 此值才推播
# ALWAYS_POST=1 → 每次都推（未達門檻走正常推播）；預設 0 → 只有達門檻才推，未達安靜略過
ALWAYS_POST = os.environ.get("ALWAYS_POST", "0").strip() in ("1", "true", "True")
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


def carrier_tag(name: str) -> str:
    if any(k in name for k in LCC_KEYWORDS):
        return "廉航"
    if any(k in name for k in FSC_KEYWORDS):
        return "傳統"
    return ""


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

    @property
    def time_label(self) -> str:
        if self.depart_time and self.arrive_time:
            return f"{self.depart_time}→{self.arrive_time}"
        return self.depart_time or self.arrive_time

    @property
    def detail(self) -> str:
        return " · ".join(x for x in [self.time_label, self.stops_label, self.duration_label] if x)


@dataclass
class RoundTrip:
    """同一家航空的去程 + 回程配成一筆來回。"""
    outbound: Itinerary
    inbound: Itinerary

    @property
    def airline(self) -> str:
        return self.outbound.airline_label

    @property
    def tag(self) -> str:
        return carrier_tag(self.airline)

    @property
    def total(self) -> int:
        return self.outbound.price + self.inbound.price


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
    # 依 (航空, 價格, 出發時間) 去重，並依價格由低到高排序
    seen, deduped = set(), []
    for it in sorted(results, key=lambda x: x.price):
        key = (it.airline_label, it.price, it.depart_time)
        if key not in seen:
            seen.add(key)
            deduped.append(it)
    return deduped


def fetch_oneway(date: str, frm: str, to: str) -> list[Itinerary]:
    """單程搜尋 → 該方向班次（價格為全體乘客總價，已依價格排序）。"""
    query = create_query(
        flights=[FlightQuery(date=date, from_airport=frm, to_airport=to)],
        trip="one-way", seat="economy", passengers=Passengers(adults=PASSENGERS),
        language="zh-TW", currency=CURRENCY,
    )
    return parse_itineraries(fetch_flights_html(query))


def _cheapest_by_airline(items: list[Itinerary]) -> dict[str, Itinerary]:
    """items 已依價格排序 → 每家航空取第一筆（最便宜）。"""
    best: dict[str, Itinerary] = {}
    for it in items:
        best.setdefault(it.airline_label, it)
    return best


def pair_roundtrips(outbound: list[Itinerary], inbound: list[Itinerary]) -> list[RoundTrip]:
    """把同一家航空的最便宜去程與最便宜回程配成來回，依來回合計排序。"""
    best_out = _cheapest_by_airline(outbound)
    best_in = _cheapest_by_airline(inbound)
    pairs = [RoundTrip(best_out[a], best_in[a]) for a in best_out if a in best_in]
    pairs.sort(key=lambda p: p.total)
    return pairs


def _per_person(total: int) -> int:
    """全體乘客總價 → 每人單價。"""
    return round(total / PASSENGERS) if PASSENGERS else total


def _price_str(total: int) -> str:
    """價格字串；多人時附註每人單價。"""
    if PASSENGERS > 1:
        return f"NT${total:,}，每人 NT${_per_person(total):,}"
    return f"NT${total:,}"


def build_slack_payload(pairs: list[RoundTrip]) -> dict:
    route = f"{FROM_AIRPORT}↔{TO_AIRPORT}"
    dates = f"{DEPART_DATE} 去 / {RETURN_DATE} 回"
    pax = f"{PASSENGERS} 位成人"
    if not pairs:
        return {"text": f":warning: [{route}] {dates} 這次沒配對到任何來回（可能來源改版或暫時無結果）。"}

    cheapest = pairs[0]
    hit = cheapest.total <= THRESHOLD
    ctag = f"〈{cheapest.tag}〉" if cheapest.tag else ""
    cpp = f"（每人 NT${_per_person(cheapest.total):,}）" if PASSENGERS > 1 else ""

    if hit:
        head = (
            f":airplane: <!channel> *[{route}] 出現 NT${THRESHOLD:,} 以下的來回票！*\n"
            f"最低來回合計 *NT${cheapest.total:,}* — {cheapest.airline}{ctag}{cpp}"
        )
    else:
        head = (
            f":airplane: [{route}] 來回機票監控\n"
            f"目前最低來回合計 *NT${cheapest.total:,}* — {cheapest.airline}{ctag}{cpp}"
        )

    lines = [head, f"_{dates}・經濟艙・{pax}・金額為全體乘客來回合計_", ""]
    for i, rt in enumerate(pairs[:5], 1):
        tag = f"〈{rt.tag}〉" if rt.tag else ""
        pp = f"（每人 NT${_per_person(rt.total):,}）" if PASSENGERS > 1 else ""
        lines.append(f"{i}. *NT${rt.total:,}* — {rt.airline}{tag}{pp}")
        lines.append(f"      去 {rt.outbound.detail}（{_price_str(rt.outbound.price)}）")
        lines.append(f"      回 {rt.inbound.detail}（{_price_str(rt.inbound.price)}）")

    lines.append("")
    lines.append("_※ 合計＝去程單程＋回程單程（廉航一次買單程即實付；傳統航空來回套票可能更便宜）。行李以訂票頁為準。_")

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
        outbound = fetch_oneway(DEPART_DATE, FROM_AIRPORT, TO_AIRPORT)
        inbound = fetch_oneway(RETURN_DATE, TO_AIRPORT, FROM_AIRPORT)
    except Exception as e:  # 抓取失敗也推一則，避免默默失效
        post_to_slack({"text": f":x: 機票監控執行失敗：{type(e).__name__}: {e}"})
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    pairs = pair_roundtrips(outbound, inbound)
    if not pairs:  # 沒配對到 → 推警告（可能來源改版）
        post_to_slack(build_slack_payload(pairs))
        return 0

    cheapest = pairs[0]
    if cheapest.total <= THRESHOLD or ALWAYS_POST:
        post_to_slack(build_slack_payload(pairs))
    else:  # 未達門檻且非 ALWAYS_POST → 安靜略過，只留 log
        print(f"[skip] 最低來回合計 NT${cheapest.total:,} > 門檻 NT${THRESHOLD:,}，不推播")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
