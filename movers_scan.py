"""全市场异动扫描（风口发现）：futbin.com/market 的 Top Gainers / Top Losers 名单取候选，
再逐张用球员页核实 PS 现价（名单页的价格列口径不明，绝不直接引用）。

名单条目 = a.market-player-card，href 带 futbin id/slug，文本尾 token 为涨跌幅 %。
过滤：|涨跌幅| >= 25%，每侧取前 12；核实价落在 800–200,000 才保留并落库
（key='futbin:<id>'，与取价器同键，后续转持仓序列不断）。
输出：stdout 一行 JSON {ts, gainers[], losers[], errors[]}
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

from futbin_prices import UA, fetch_player  # noqa: E402
from pricedb import insert_samples  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
LINK = re.compile(r"/27/player/(\d+)/([a-z0-9-]+)")
PCT = re.compile(r"(\d+(?:\.\d+)?)%\s*$")
POS = {"GK", "LB", "RB", "CB", "LWB", "RWB", "CDM", "CM", "CAM", "LM", "RM",
       "LW", "RW", "ST", "CF", "LF", "RF", "++"}
MIN_PCT = 25.0
TOP_N = 12
BAND = (800, 200_000)


def parse_section(soup: BeautifulSoup, cls: str, sign: int) -> list[dict]:
    div = soup.find("div", class_=cls)
    if not div:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for a in div.find_all("a", class_="market-player-card", href=True):
        m = LINK.search(a["href"])
        if not m or m.group(1) in seen:
            continue
        txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        pm = PCT.search(txt)
        if not pm:
            continue
        pct = float(pm.group(1)) * sign
        if abs(pct) < MIN_PCT:
            continue
        seen.add(m.group(1))
        toks = txt.split(" ")
        rating = None
        name = m.group(2)
        # 结构：<价A> <价B> <评分> <位置...> <名> <惯用脚R/L> ...
        for i, t in enumerate(toks):
            if t.isdigit() and 50 <= int(t) <= 99 and rating is None and i >= 2:
                rating = int(t)
                rest = [x for x in toks[i + 1:] if x not in POS]
                nm = []
                for x in rest:
                    if x in ("R", "L"):
                        break
                    nm.append(x)
                if nm:
                    name = " ".join(nm)
                break
        out.append({"futbin_id": int(m.group(1)), "slug": m.group(2),
                    "name": name, "rating": rating, "mover_pct": pct})
    out.sort(key=lambda d: -abs(d["mover_pct"]))
    return out[:TOP_N]


def main() -> int:
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    errors: list[str] = []
    try:
        resp = requests.get("https://www.futbin.com/market",
                            headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cand = {"gainers": parse_section(soup, "market-movers", +1),
                "losers": parse_section(soup, "market-losers", -1)}
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ts": ts, "gainers": [], "losers": [],
                          "errors": [f"market page: {type(e).__name__}: {e}"]},
                         ensure_ascii=False))
        return 1

    result: dict[str, list[dict]] = {"gainers": [], "losers": []}
    db_rows = []
    for side in ("gainers", "losers"):
        for c in cand[side]:
            try:
                v = fetch_player({"futbin_id": c["futbin_id"], "slug": c["slug"],
                                  "name": c["name"], "rating": c["rating"] or 0})
            except Exception as e:  # noqa: BLE001
                errors.append(f"{c['name']}: {type(e).__name__}: {e}")
                continue
            price = v.get("price") or 0
            if not (BAND[0] <= price <= BAND[1]):
                continue
            item = {"futbin_id": c["futbin_id"], "name": v.get("name") or c["name"],
                    "rating": v.get("page_rating") or c["rating"],
                    "mover_pct": c["mover_pct"], "price": price,
                    "recent": v.get("recent"), "ladder": v.get("ladder"),
                    "trend": v.get("trend"), "url": v.get("url")}
            result[side].append(item)
            db_rows.append({"ts": ts, "kind": "player", "key": f"futbin:{c['futbin_id']}",
                            "name": item["name"], "rating": item["rating"],
                            "price": price, "exp_hours": None,
                            "source": "futbin movers PS"})
            time.sleep(0.6)
    n = insert_samples(db_rows)
    print(json.dumps({"ts": ts, "db_rows": n, **result, "errors": errors},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
