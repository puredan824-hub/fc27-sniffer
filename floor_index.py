"""fodder 地板指数：futbin.com 列表页分段采样，每段取最便宜 10 张的中位数为地板价。

机制（2026-10-25 实测两轮定案）：
- /27/players 的 ps_price 价格带服务端生效；**不带 sort 参数时默认按评分降序**，
  窄带内该段卡连续排布——按评分过滤取前 10 张即该段最便宜的一批。
- 切勿用 sort=ps_price&order=asc 宽带扫：600 价位平台有海量低分卡，目标段会被淹没。
行元素 tr.player-row（cell0=球员链接、cell1=评分、cell4=PS 价）。
段带贴合已知地板留余量；underfill 时放宽高界重试一次。
结果写入 prices.db（kind='floor', key='floor:<rating>'），并与上轮中位数算涨跌。
输出：stdout 一行 JSON {ts, segments{}, requests_used, errors[]}
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from pricedb import connect, insert_samples  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")}
# 窄带（默认评分降序，带内该段卡在高分侧连续出现）
SEGMENT_BANDS = {
    83: (500, 1_200),
    84: (900, 2_000),
    85: (2_000, 4_500),
    86: (4_000, 9_000),
    87: (6_000, 13_000),
    88: (10_000, 25_000),
}
PER_SEGMENT = 10
MIN_TO_PUBLISH = 3
MAX_PAGES_PER_SEGMENT = 6
LINK = re.compile(r"/27/player/(\d+)/")


def parse_price_token(tok: str) -> int | None:
    tok = tok.strip().replace(",", "")
    try:
        if tok.endswith("K"):
            return int(float(tok[:-1]) * 1_000)
        if tok.endswith("M"):
            return int(float(tok[:-1]) * 1_000_000)
        return int(float(tok))
    except ValueError:
        return None


def fetch_band_page(lo: int, hi: int, page: int) -> list[dict]:
    url = f"https://www.futbin.com/27/players?ps_price={lo}-{hi}&page={page}"
    resp = requests.get(url, headers=UA, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for tr in soup.find_all("tr", class_="player-row"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        try:
            rating = int(tds[1].get_text(strip=True))
        except ValueError:
            continue
        price = parse_price_token(tds[4].get_text(" ", strip=True).split(" ")[0])
        if price is None:
            continue
        a = tds[0].find("a", href=True)
        m = LINK.search(a["href"]) if a else None
        out.append({
            "id": m.group(1) if m else tds[0].get_text(" ", strip=True),
            "name": tds[0].get_text(" ", strip=True),
            "rating": rating, "price": price,
        })
    return out


def collect_segment(seg: int, lo: int, hi: int, counter: list[int]) -> list[dict]:
    got: list[dict] = []
    seen: set[str] = set()
    for page in range(1, MAX_PAGES_PER_SEGMENT + 1):
        rows = fetch_band_page(lo, hi, page)
        counter[0] += 1
        if not rows:
            break
        for r in rows:
            if r["rating"] == seg and r["id"] not in seen:
                seen.add(r["id"])
                got.append(r)
        if len(got) >= PER_SEGMENT:
            break
        # 评分降序：本页最低评分已低于目标段且已攒到一些，说明该段已翻完
        if got and min(r["rating"] for r in rows) < seg:
            break
        time.sleep(0.8)
    got.sort(key=lambda r: r["price"])
    return got[:PER_SEGMENT]


def prev_medians() -> dict[int, int]:
    conn = connect()
    res = {}
    for seg in SEGMENT_BANDS:
        row = conn.execute(
            "SELECT price FROM samples WHERE kind='floor' AND key=? ORDER BY id DESC LIMIT 1",
            (f"floor:{seg}",),
        ).fetchone()
        if row:
            res[seg] = row[0]
    conn.close()
    return res


def main() -> int:
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    errors: list[str] = []
    counter = [0]
    buckets: dict[int, list[dict]] = {}
    for seg, (lo, hi) in SEGMENT_BANDS.items():
        try:
            cards = collect_segment(seg, lo, hi, counter)
            if len(cards) < MIN_TO_PUBLISH:  # 放宽高界重试一次
                cards = collect_segment(seg, lo, hi * 2, counter)
            buckets[seg] = cards
        except Exception as e:  # noqa: BLE001
            errors.append(f"seg {seg}: {type(e).__name__}: {e}")
            buckets[seg] = []
        time.sleep(0.8)

    prev = prev_medians()
    segments: dict[str, dict] = {}
    db_rows = []
    for seg, cards in buckets.items():
        if len(cards) < MIN_TO_PUBLISH:
            segments[str(seg)] = {"n": len(cards), "median": None}
            continue
        prices = sorted(c["price"] for c in cards)
        med = int(median(prices))
        p = prev.get(seg)
        segments[str(seg)] = {
            "n": len(cards),
            "min": prices[0],
            "median": med,
            "prev_median": p,
            "change_pct": round((med - p) / p * 100, 1) if p else None,
            "cheapest": [{"name": c["name"], "price": c["price"]} for c in cards[:3]],
        }
        db_rows.append({"ts": ts, "kind": "floor", "key": f"floor:{seg}",
                        "name": f"{seg} fodder floor", "rating": seg, "price": med,
                        "exp_hours": None, "source": "futbin.com list PS"})
    n = insert_samples(db_rows)
    print(json.dumps({"ts": ts, "requests_used": counter[0], "db_rows": n,
                      "segments": segments, "errors": errors}, ensure_ascii=False))
    return 0 if db_rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
