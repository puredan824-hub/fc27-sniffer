"""futbin.com 网页取价器：批量拉 watchlist 的 PS 现价（SSR 页面，curl/requests 直读）。

背景：futbin.org 内部 API 对本机出口 403，但 futbin.com 球员页服务器渲染带价。
解析点（div.price-box.platform-ps-only 内）：
- 当前价 .price.lowest-price-1；近期序列 [data-recent-prices]；
- 在售阶梯 .lowest-prices-wrapper .lowest-price；趋势 .price-box-trend；
- EA 价格区间/更新时间在 .price-box-full-width 文本里。
每张卡的现价自动写入 prices.db（kind='player', source='futbin.com web PS'）。
输出：stdout 一行 JSON {ts, results[], errors[]}。
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from pricedb import insert_samples  # noqa: E402

HERE = Path(__file__).parent
WATCHLIST = HERE / "watchlist.json"
TZ = ZoneInfo("Asia/Shanghai")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")


def to_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def fetch_player(entry: dict) -> dict:
    url = f"https://www.futbin.com/27/player/{entry['futbin_id']}/{entry['slug']}"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    title = soup.title.get_text() if soup.title else ""
    m = re.search(r"-\s*(\d{2})\s*-\s*Rating", title)
    page_rating = int(m.group(1)) if m else None

    box = soup.select_one("div.price-box.platform-ps-only")
    if box is None:
        raise ValueError("PS price box not found")

    price_el = box.select_one(".price.lowest-price-1")
    price = to_int(price_el.get_text()) if price_el else None

    graph = box.select_one("[data-recent-prices]")
    recent = []
    if graph is not None:
        recent = [int(x) for x in graph["data-recent-prices"].split(",") if x.strip().isdigit()]

    ladder = [to_int(el.get_text()) for el in box.select(".lowest-prices-wrapper .lowest-price")]
    ladder = [x for x in ladder if x is not None]

    trend_el = box.select_one(".price-box-trend")
    trend = trend_el.get_text(" ", strip=True) if trend_el else None

    range_min = range_max = updated = None
    full = box.select_one(".price-box-full-width")
    if full is not None:
        text = full.get_text(" ", strip=True)
        rm = re.search(r"Price Range:\s*([\d,]+)\s*-\s*([\d,]+)", text)
        if rm:
            range_min, range_max = to_int(rm.group(1)), to_int(rm.group(2))
        um = re.search(r"Price Updated:\s*(.+?)(?:\s*Price Range:|$)", text)
        if um:
            updated = um.group(1).strip()

    return {
        "key": f"futbin:{entry['futbin_id']}",
        "name": entry["name"],
        "rating": entry["rating"],
        "page_rating": page_rating,
        "price": price,
        "recent": recent,
        "ladder": ladder,
        "trend": trend,
        "range_min": range_min,
        "range_max": range_max,
        "updated": updated,
        "url": url,
    }


def main() -> int:
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    watchlist = json.loads(WATCHLIST.read_text())
    results, errors = [], []
    for entry in watchlist:
        try:
            results.append(fetch_player(entry))
        except Exception as e:  # noqa: BLE001
            errors.append({"name": entry["name"], "error": f"{type(e).__name__}: {e}"})
        time.sleep(1.5)

    rows = [
        {"ts": ts, "kind": "player", "key": r["key"], "name": r["name"],
         "rating": r["rating"], "price": r["price"], "exp_hours": None,
         "source": "futbin.com web PS"}
        for r in results if r["price"] is not None
    ]
    n = insert_samples(rows)
    print(json.dumps({"ts": ts, "db_rows": n, "results": results, "errors": errors},
                     ensure_ascii=False))
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
