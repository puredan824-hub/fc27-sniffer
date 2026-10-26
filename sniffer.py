"""探嗅引擎 sniffer：5 分钟级色卡趋势监测 + 自主判定（状态机）。

背景（2026-10-27 用户令升级）：原盯盘每小时一轮，靠 worker 肉眼看 recent 数组判企稳，
色卡机会熟得慢。本引擎把 flip 纪律编码成状态机，常驻高频轮询 futbin PS 价：
  - 每轮拉 12 张色卡（Scream/Hero/SP/TOTW/FS 观察面），现价写 prices.db（source='sniffer 5m'）；
  - 逐卡算特征：6h 内最近一次创新低距今分钟数、1h 斜率、45 分钟振幅、历史高点回撤；
  - 状态机：FALLING / BOTTOMING / NEUTRAL / READY（企稳+回撤达标）/ RALLY（自低点反弹）；
  - 进入 READY 边沿触发信号落 signals 表：有触发价且现价达标 = FLIP_READY（可执行），
    否则 CANDIDATE_READY（候选情报）；RALLY 单独成信号（持仓卖出窗口/未持仓不追）。
信号由 hook 唤醒处理员复核后通知用户，并由每小时盯盘轮消费执行（信号不替代执行轮的新鲜实读复核）。

用法：
  sniffer.py cycle    跑一轮：取价+落库+判定+发信号，stdout 一行 JSON
  sniffer.py pending  输出未处理信号 JSON {"count":n,"signals":[...]}
  sniffer.py ack 1 2  标记信号已处理
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from futbin_prices import fetch_player  # noqa: E402
from pricedb import connect, insert_samples  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
PARAMS_PATH = Path(__file__).parent / "sniffer_params.json"
PARAM_DEFAULTS = {
    "falling_low_age_min": 30, "falling_slope": -0.03,
    "ready_low_age_min": 45, "ready_range45_max": 0.04, "ready_slope_min": -0.01,
    "running_slope": 0.04, "dd_min": 0.30, "rally_dd_min": 0.20,
}


def load_params() -> dict:
    try:
        p = json.loads(PARAMS_PATH.read_text())
        return {**PARAM_DEFAULTS, **{k: v for k, v in p.items() if k in PARAM_DEFAULTS}}
    except Exception:  # noqa: BLE001
        return dict(PARAM_DEFAULTS)

# 色卡观察面（futbin_id, slug, name）。以下为示例名单，按需替换为自己的观察卡。
SPECIALS = [
    {"futbin_id": 23437, "slug": "merveille-kanjinga", "name": "Kanjinga (FS)", "rating": 85},
    {"futbin_id": 23621, "slug": "junior-alonso", "name": "Alonso (Scream)", "rating": 85},
    {"futbin_id": 23622, "slug": "tahith-chong", "name": "Chong (Scream)", "rating": 85},
    {"futbin_id": 23614, "slug": "selina-vobian", "name": "Vobian (Scream)", "rating": 86},
    {"futbin_id": 23612, "slug": "geoffrey-kondogbia", "name": "Kondogbia (Scream)", "rating": 86},
    {"futbin_id": 23606, "slug": "fabio-henrique-tavares", "name": "Fabinho (Scream Holo)", "rating": 87},
    {"futbin_id": 21725, "slug": "tim-howard", "name": "Howard (Hero Holo)", "rating": 85},
    {"futbin_id": 21731, "slug": "scott", "name": "Scott (Hero Holo)", "rating": 85},
    {"futbin_id": 21727, "slug": "crouch", "name": "Crouch (Hero Holo)", "rating": 85},
    {"futbin_id": 23688, "slug": "joaquin-seys", "name": "Seys (SP Holo)", "rating": 85},
    {"futbin_id": 23401, "slug": "frank-acheampong", "name": "Acheampong (TOTW Holo)", "rating": 80},
    {"futbin_id": 23006, "slug": "orkun-kokcu", "name": "Kökçü (Special)", "rating": 86},
]

# Flip 触发价（示例占位：按你自己的交易计划维护；现价 ≤ 触发价视为回撤达标）
# TRIGGERS = {<futbin_id>: <触发价>, ...}
TRIGGERS = {}
# 已知历史高点种子（示例占位：从 futbin recent 实读；与样本/recent 取大者为 hist_high）
# SEED_HIGHS = {<futbin_id>: <历史高点>, ...}
SEED_HIGHS = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sniffer_state(
  key TEXT PRIMARY KEY, name TEXT, state TEXT, state_since TEXT,
  hist_high INTEGER, last_emit TEXT
);
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, key TEXT NOT NULL, name TEXT,
  type TEXT NOT NULL, price INTEGER, detail TEXT, acked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sniffer_states_log(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, key TEXT NOT NULL, name TEXT,
  state TEXT, price INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def load_series(conn: sqlite3.Connection, key: str, hours: float) -> list[tuple[datetime, int]]:
    since = (datetime.now(TZ) - timedelta(hours=hours)).isoformat(timespec="seconds")
    # 只用 futbin 系来源：fut.gg R2 对冷门色卡与 futbin 价差可达 30%+，混源会造假新低/假斜率
    rows = conn.execute(
        "SELECT ts, price FROM samples WHERE kind='player' AND key=? AND ts>=?"
        " AND source<>'fut.gg R2' ORDER BY ts",
        (key, since),
    ).fetchall()
    out = []
    for ts, price in rows:
        try:
            out.append((datetime.fromisoformat(ts), int(price)))
        except (ValueError, TypeError):
            continue
    return out


def features(conn, key, price, recent):
    """返回 (new_low_age_min, slope_1h, range_45m, tail_at_low, hist_high)。"""
    now = datetime.now(TZ)
    s6 = load_series(conn, key, 6)
    # 6h 内最近一次创新低：样本价低于其前 6h 窗口内全部样本（且前面至少有 2 个样本）
    last_low_ts = None
    for i, (ts, p) in enumerate(s6):
        if i < 2:
            continue
        prior = [pp for (tt, pp) in s6[:i] if tt >= ts - timedelta(hours=6)]
        if prior and p < min(prior):
            last_low_ts = ts
    new_low_age = (now - last_low_ts).total_seconds() / 60 if last_low_ts else 999.0

    # 1h 斜率：取 55-80 分钟前最近样本
    slope = None
    s2 = load_series(conn, key, 2)
    ref = None
    for ts, p in s2:
        age = (now - ts).total_seconds() / 60
        if 50 <= age <= 90:
            ref = p
    if ref:
        slope = (price - ref) / ref

    # 45 分钟振幅：样本不足时用 recent 数组兜底
    s45 = [(ts, p) for ts, p in s6 if (now - ts).total_seconds() <= 45 * 60]
    vals = [p for _, p in s45]
    if len(vals) >= 2:
        rng = (max(vals) - min(vals)) / min(vals)
    elif recent and len(recent) >= 2:
        rng = (max(recent) - min(recent)) / min(recent)
    else:
        rng = None

    # recent 尾部仍在创新低：末值=最小值且仅出现一次且低于前值（平底横盘末值会重复出现）
    tail_at_low = False
    if recent and len(recent) >= 3:
        tail_at_low = (
            recent[-1] == min(recent)
            and recent.count(min(recent)) == 1
            and recent[-1] < recent[-2]
        )

    row = conn.execute("SELECT hist_high FROM sniffer_state WHERE key=?", (key,)).fetchone()
    hist = row[0] if row and row[0] else 0
    allmax = conn.execute(
        "SELECT MAX(price) FROM samples WHERE kind='player' AND key=? AND source<>'fut.gg R2'",
        (key,),
    ).fetchone()[0] or 0
    hist = max(hist, allmax, max(recent) if recent else 0, price)
    return new_low_age, slope, rng, tail_at_low, hist


def judge(fid, price, new_low_age, slope, rng, tail_at_low, hist, P):
    dd = (hist - price) / hist if hist else 0.0
    falling = (new_low_age < P["falling_low_age_min"]
               or (slope is not None and slope <= P["falling_slope"]) or tail_at_low)
    running = slope is not None and slope >= P["running_slope"]
    stabilized = (
        new_low_age >= P["ready_low_age_min"]
        and not tail_at_low
        and (rng is None or rng <= P["ready_range45_max"])
        and (slope is None or slope >= P["ready_slope_min"])
    )
    trig = TRIGGERS.get(fid)
    dd_ok = (trig is not None and price <= trig) or dd >= P["dd_min"]
    if stabilized and not falling and not running and dd_ok:
        state = "READY"
    elif running and dd >= P["rally_dd_min"]:
        state = "RALLY"
    elif falling:
        state = "FALLING"
    elif new_low_age >= 30:
        state = "BOTTOMING"
    else:
        state = "NEUTRAL"
    return state, dd


def cycle() -> dict:
    ts = now_iso()
    params = load_params()
    conn = connect()
    conn.executescript(SCHEMA)
    states, new_signals, errors = [], [], []
    sample_rows = []

    fetched = {}
    for entry in SPECIALS:
        try:
            r = fetch_player(entry)
            fetched[entry["futbin_id"]] = (entry, r)
            if r["price"] is not None:
                sample_rows.append({
                    "ts": ts, "kind": "player", "key": r["key"], "name": r["name"],
                    "rating": r["rating"], "price": r["price"], "exp_hours": None,
                    "source": "sniffer 5m",
                })
        except Exception as e:  # noqa: BLE001
            errors.append({"name": entry["name"], "error": f"{type(e).__name__}: {e}"})
        time.sleep(1.2)
    if sample_rows:
        insert_samples(sample_rows)

    for fid, (entry, r) in fetched.items():
        price = r["price"]
        if price is None:
            continue
        key = r["key"]
        recent = r.get("recent") or []
        nla, slope, rng, tail_low, hist = features(conn, key, price, recent)
        if fid in SEED_HIGHS:
            hist = max(hist, SEED_HIGHS[fid])
        state, dd = judge(fid, price, nla, slope, rng, tail_low, hist, params)

        prev = conn.execute(
            "SELECT state, state_since, last_emit FROM sniffer_state WHERE key=?", (key,)
        ).fetchone()
        prev_state = prev[0] if prev else None
        state_since = prev[1] if prev and prev_state == state else ts
        last_emit = json.loads(prev[2]) if prev and prev[2] else {}

        sig_type = None
        if state == "READY" and prev_state != "READY":
            sig_type = "FLIP_READY" if (fid in TRIGGERS and price <= TRIGGERS[fid]) else "CANDIDATE_READY"
        elif state == "RALLY" and prev_state != "RALLY":
            sig_type = "RALLY"
        if sig_type:
            last = last_emit.get(sig_type)
            cooled = True
            if last:
                cooled = (datetime.now(TZ) - datetime.fromisoformat(last)).total_seconds() >= 6 * 3600
            if cooled:
                detail = {
                    "price": price, "dd_hist": round(dd, 3),
                    "new_low_age_min": round(nla), "slope_1h": None if slope is None else round(slope, 4),
                    "range_45m": None if rng is None else round(rng, 4),
                    "trigger": TRIGGERS.get(fid), "hist_high": hist,
                    "recent": recent[-5:],
                }
                cur = conn.execute(
                    "INSERT INTO signals(ts,key,name,type,price,detail) VALUES(?,?,?,?,?,?)",
                    (ts, key, entry["name"], sig_type, price, json.dumps(detail, ensure_ascii=False)),
                )
                new_signals.append({"id": cur.lastrowid, "name": entry["name"],
                                    "type": sig_type, "price": price, "detail": detail})
                last_emit[sig_type] = ts
        with conn:
            conn.execute(
                "INSERT INTO sniffer_state(key,name,state,state_since,hist_high,last_emit)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name,"
                " state=excluded.state, state_since=excluded.state_since,"
                " hist_high=excluded.hist_high, last_emit=excluded.last_emit",
                (key, entry["name"], state, state_since, hist, json.dumps(last_emit)),
            )
        states.append({
            "key": key, "name": entry["name"], "price": price, "state": state,
            "new_low_age_min": round(nla), "slope_1h": None if slope is None else round(slope, 4),
            "dd_hist": round(dd, 3), "range_45m": None if rng is None else round(rng, 4),
        })
    if states:
        with conn:
            conn.executemany(
                "INSERT INTO sniffer_states_log(ts,key,name,state,price) VALUES(?,?,?,?,?)",
                [(ts, s["key"], s["name"], s["state"], s["price"]) for s in states],
            )
    conn.close()
    return {"ts": ts, "states": states, "new_signals": new_signals, "errors": errors}


def pending() -> dict:
    conn = connect()
    conn.executescript(SCHEMA)
    rows = conn.execute(
        "SELECT id, ts, key, name, type, price, detail FROM signals WHERE acked=0 ORDER BY id"
    ).fetchall()
    conn.close()
    return {"count": len(rows), "signals": [
        {"id": r[0], "ts": r[1], "key": r[2], "name": r[3], "type": r[4],
         "price": r[5], "detail": json.loads(r[6]) if r[6] else None} for r in rows]}


def ack(ids: list[int]) -> int:
    conn = connect()
    conn.executescript(SCHEMA)
    with conn:
        conn.executemany("UPDATE signals SET acked=1 WHERE id=?", [(i,) for i in ids])
    n = conn.total_changes
    conn.close()
    return n


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cycle"
    if cmd == "cycle":
        print(json.dumps(cycle(), ensure_ascii=False))
    elif cmd == "pending":
        print(json.dumps(pending(), ensure_ascii=False))
    elif cmd == "ack":
        print(json.dumps({"acked": ack([int(x) for x in sys.argv[2:]])}))
    else:
        print(json.dumps({"error": f"unknown cmd {cmd}"}))
        raise SystemExit(2)
