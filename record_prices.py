"""把一轮实读的球员 PS 价格落进 prices.db（kind='player'）。
用法：stdin 传入 JSON 数组，每项 {key, name, rating, price, source}。
  echo '[{"key":"kanjinga-23437","name":"Kanjinga","rating":85,"price":41500,"source":"FUTBIN PS"}]' | record_prices.py
查看：record_prices.py --show 20
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from pricedb import connect, insert_samples  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--show":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        conn = connect()
        rows = conn.execute(
            "SELECT ts,kind,key,name,price,exp_hours,source FROM samples ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        conn.close()
        for r in rows:
            print(r)
        return 0
    data = json.load(sys.stdin)
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    rows = [
        {"ts": ts, "kind": "player", "key": d["key"], "name": d.get("name"),
         "rating": d.get("rating"), "price": d.get("price"),
         "exp_hours": None, "source": d.get("source")}
        for d in data
    ]
    print(f"inserted {insert_samples(rows)} rows @ {ts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
