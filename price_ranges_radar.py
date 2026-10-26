"""FC27 Price Ranges 雷达：抓 fut.gg/price-ranges（EA 官方调价事件页）→ diff → 新事件判定 + 落库。

页面每行 = 一次 EA 调价事件：球员、状态（Listed/Extinct before）、Old 区间 → New 区间、事件时间（UTC）。
diff 口径：dedupe_key（item_id|事件时间|新区间）未见过 = 新事件。首轮建基线不告警。
输出：stdout 一行 JSON {ts, baseline, counts, new_events[], warnings[]}
状态：state/price_ranges_snapshot.json
事件落库：prices.db 的 range_events 表（INSERT OR IGNORE 去重）。
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from pricedb import insert_range_events  # noqa: E402

URL = "https://www.fut.gg/price-ranges/"
STATE_DIR = Path(__file__).parent / "state"
SNAPSHOT = STATE_DIR / "price_ranges_snapshot.json"
TZ = ZoneInfo("Asia/Shanghai")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
CARD_HREF = re.compile(r"/players/(\d+)-([a-z0-9-]+)/27-(\d+)/")
RANGE_RE = re.compile(
    r"Old\s+([\d.]+[KM]?)\s*[–—-]\s*([\d.]+[KM]?)\s+New\s+([\d.]+[KM]?)\s*[–—-]\s*([\d.]+[KM]?)")
TIME_RE = re.compile(r"(\d{1,2})\s+([A-Z][a-z]{2}),\s*(\d{2}):(\d{2})\s*UTC")


def parse_val(s: str) -> int:
    s = s.strip()
    if s.endswith("K"):
        return int(float(s[:-1]) * 1_000)
    if s.endswith("M"):
        return int(float(s[:-1]) * 1_000_000)
    return int(float(s))


def fetch_rows(fetch_dt: datetime) -> list[dict]:
    resp = requests.get(URL, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    rows: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = CARD_HREF.search(a["href"])
        if not m:
            continue
        item_id = m.group(3)
        # 行容器：从 anchor 向上找含 Old/New 且只含一张卡的祖先
        row = a
        for _ in range(4):
            row = row.parent
            if row is None:
                break
            txt = row.get_text(" ", strip=True)
            n_cards = len([x for x in row.find_all("a", href=True) if CARD_HREF.search(x["href"])])
            if "Old" in txt and "New" in txt and n_cards == 1:
                break
        else:
            continue
        if row is None:
            continue
        text = re.sub(r"\s+", " ", row.get_text(" ", strip=True))
        rm = RANGE_RE.search(text)
        tm = TIME_RE.search(text)
        if not rm or not tm:
            continue
        face = [p.strip() for p in a.get_text("|", strip=True).split("|")]
        name = face[0] if face else ""
        rating = int(face[1]) if len(face) > 1 and face[1].isdigit() else None
        day, mon, hh, mm = int(tm.group(1)), MONTHS[tm.group(2)], int(tm.group(3)), int(tm.group(4))
        event_utc = datetime(fetch_dt.year, mon, day, hh, mm, tzinfo=timezone.utc)
        if (event_utc - fetch_dt).total_seconds() > 2 * 86400:  # 跨年兜底
            event_utc = event_utc.replace(year=fetch_dt.year - 1)
        old_min, old_max, new_min, new_max = (parse_val(rm.group(i)) for i in range(1, 5))
        status = "Extinct" if "Extinct before" in text else "Listed"
        key = f"{item_id}|{event_utc.isoformat()}|{new_min}-{new_max}"
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "item_id": item_id, "name": name, "rating": rating, "status": status,
            "old_min": old_min, "old_max": old_max, "new_min": new_min, "new_max": new_max,
            "event_ts": event_utc.astimezone(TZ).isoformat(timespec="minutes"),
            "dedupe_key": key,
        })
    return rows


def main() -> int:
    fetch_dt = datetime.now(timezone.utc)
    ts = fetch_dt.astimezone(TZ).isoformat(timespec="seconds")
    warnings: list[str] = []
    try:
        rows = fetch_rows(fetch_dt)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ts": ts, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1
    if not rows:
        print(json.dumps({"ts": ts, "error": "parsed 0 rows", "warnings": warnings}, ensure_ascii=False))
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    prev_keys: set[str] | None = None
    if SNAPSHOT.exists():
        try:
            prev_keys = set(json.loads(SNAPSHOT.read_text()).get("keys", []))
        except Exception:  # noqa: BLE001
            prev_keys = None
    baseline = prev_keys is None
    new_events = [] if baseline else [r for r in rows if r["dedupe_key"] not in prev_keys]

    all_keys = sorted({r["dedupe_key"] for r in rows} | (prev_keys or set()))
    SNAPSHOT.write_text(json.dumps({"ts": ts, "keys": all_keys}, ensure_ascii=False))
    added = insert_range_events([
        {**r, "fetched_ts": ts} for r in (rows if baseline else new_events)
    ])
    print(json.dumps({
        "ts": ts, "baseline": baseline,
        "counts": {"page_rows": len(rows), "db_added": added},
        "new_events": new_events, "warnings": warnings,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
