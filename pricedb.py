"""FC27 价格序列 SQLite 存储（盯盘/SBC 雷达共用）。
表 samples: ts(ISO, Asia/Shanghai), kind('sbc'|'evo'|'player'), key, name, rating, price, exp_hours, source
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("prices.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  name TEXT,
  rating INTEGER,
  price INTEGER,
  exp_hours REAL,
  source TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_kind_key_ts ON samples(kind, key, ts);
CREATE TABLE IF NOT EXISTS range_events(
  id INTEGER PRIMARY KEY,
  fetched_ts TEXT NOT NULL,
  event_ts TEXT,
  item_id TEXT NOT NULL,
  name TEXT,
  rating INTEGER,
  status TEXT,
  old_min INTEGER, old_max INTEGER,
  new_min INTEGER, new_max INTEGER,
  dedupe_key TEXT UNIQUE
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def insert_samples(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = connect()
    with conn:
        conn.executemany(
            "INSERT INTO samples(ts,kind,key,name,rating,price,exp_hours,source)"
            " VALUES(:ts,:kind,:key,:name,:rating,:price,:exp_hours,:source)",
            [
                {
                    "ts": r["ts"],
                    "kind": r["kind"],
                    "key": r["key"],
                    "name": r.get("name"),
                    "rating": r.get("rating"),
                    "price": r.get("price"),
                    "exp_hours": r.get("exp_hours"),
                    "source": r.get("source"),
                }
                for r in rows
            ],
        )
    conn.close()
    return len(rows)


def insert_range_events(rows: list[dict]) -> int:
    """写入 fut.gg Price Ranges 事件（dedupe_key 唯一，重复自动忽略）。返回实际新增数。"""
    if not rows:
        return 0
    conn = connect()
    before = conn.total_changes
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO range_events"
            "(fetched_ts,event_ts,item_id,name,rating,status,old_min,old_max,new_min,new_max,dedupe_key)"
            " VALUES(:fetched_ts,:event_ts,:item_id,:name,:rating,:status,:old_min,:old_max,:new_min,:new_max,:dedupe_key)",
            rows,
        )
    added = conn.total_changes - before
    conn.close()
    return added
