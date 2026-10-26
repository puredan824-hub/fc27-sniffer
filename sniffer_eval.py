"""探嗅引擎回测评分 sniffer_eval：用 prices.db 已落库序列给历史信号与状态判定打分。

- READY 类信号（FLIP_READY/CANDIDATE_READY）：信号后 24h（futbin 系样本）内
  max_gain≥+12% 且 max_dd>-10% → GOOD；max_dd≤-10% 且 max_gain<+5% → FALSE_STAB（假企稳）；
  样本<3 → INSUFFICIENT；其余 NEUTRAL。
- RALLY 信号：信号后 6h 内 max_gain≥+5% → GOOD，否则 WEAK。
- MARKET_MOVE 信号：用 r2_snaps 快照环比对信号后约 1h 的同卡 R2 价，同向且幅度≥信号一半 → CONFIRMED。
- 漏检（missed bottoms）：色卡 futbin 序列中局部低点后 24h 内涨≥15%，且 sniffer_states_log
  在该低点 ±2h 内无 READY 记录 → MISSED（状态历史自 2026-10-27 起积累，早期样本不足会少报）。
输出一行 JSON，供每日自优化 cron 决策；样本不足时如实标注，不硬凑结论。
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
TZ = ZoneInfo("Asia/Shanghai")
DB = HERE / "prices.db"
SNAPS = HERE / "r2_snaps"


def series(conn, key, t0, t1):
    rows = conn.execute(
        "SELECT ts, price FROM samples WHERE kind='player' AND key=? AND source<>'fut.gg R2'"
        " AND ts>=? AND ts<=? ORDER BY ts", (key, t0, t1)).fetchall()
    return [(datetime.fromisoformat(a), b) for a, b in rows]


def score_ready(conn, sig):
    t0 = datetime.fromisoformat(sig["ts"])
    pts = series(conn, sig["key"], sig["ts"], (t0 + timedelta(hours=24)).isoformat())
    if len(pts) < 3:
        return "INSUFFICIENT", None, None
    p0 = sig["price"]
    gain = max(p for _, p in pts) / p0 - 1
    dd = min(p for _, p in pts) / p0 - 1
    if gain >= 0.12 and dd > -0.10:
        v = "GOOD"
    elif dd <= -0.10 and gain < 0.05:
        v = "FALSE_STAB"
    else:
        v = "NEUTRAL"
    return v, round(gain, 3), round(dd, 3)


def score_rally(conn, sig):
    t0 = datetime.fromisoformat(sig["ts"])
    pts = series(conn, sig["key"], sig["ts"], (t0 + timedelta(hours=6)).isoformat())
    if len(pts) < 3:
        return "INSUFFICIENT", None
    gain = max(p for _, p in pts) / sig["price"] - 1
    return ("GOOD" if gain >= 0.05 else "WEAK"), round(gain, 3)


def score_market_move(sig):
    try:
        detail = json.loads(sig["detail"] or "{}")
        ea = sig["key"].split(":", 1)[1]
        t0 = datetime.fromisoformat(sig["ts"]).timestamp()
        files = sorted(SNAPS.glob("snap-*.json"))
        def price_at(target):
            best, bd = None, 1e18
            for f in files:
                ts = int(f.name[5:-5])
                if abs(ts - target) < bd:
                    bd, best = abs(ts - target), f
            if best is None or bd > 5400:
                return None
            return json.loads(best.read_text()).get(ea)
        p_then = price_at(t0 + 3600)
        if p_then is None or not detail.get("r2_price"):
            return "INSUFFICIENT"
        chg = p_then / detail["r2_price"] - 1
        want = detail.get("chg_1h", 0)
        ok = (chg * want > 0) and abs(chg) >= abs(want) * 0.5
        return "CONFIRMED" if ok else "UNCONFIRMED"
    except Exception:  # noqa: BLE001
        return "INSUFFICIENT"


def missed_bottoms(conn):
    keys = [r[0] for r in conn.execute(
        "SELECT DISTINCT key FROM sniffer_states_log").fetchall()]
    # 状态历史起点之前的低点无从判定漏检（没有当时的状态记录），一律跳过
    row = conn.execute("SELECT MIN(ts) FROM sniffer_states_log").fetchone()
    hist_start = datetime.fromisoformat(row[0]) if row and row[0] else None
    out = []
    now = datetime.now(TZ)
    for key in keys:
        pts = series(conn, key, (now - timedelta(days=7)).isoformat(),
                     now.isoformat())
        if hist_start:
            pts = [(t, p) for t, p in pts if t >= hist_start]
        for i, (ts, p) in enumerate(pts):
            if p <= 0:
                continue
            window = [(t2, p2) for t2, p2 in pts if ts < t2 <= ts + timedelta(hours=24)]
            if len(window) < 3:
                continue
            if max(p2 for _, p2 in window) / p < 1.15:
                continue
            prior = [p2 for t2, p2 in pts if ts - timedelta(hours=24) <= t2 < ts]
            if prior and min(prior) < p:
                continue  # 不是局部低点
            ready = conn.execute(
                "SELECT COUNT(*) FROM sniffer_states_log WHERE key=? AND state='READY'"
                " AND ts>=? AND ts<=?", (key, (ts - timedelta(hours=2)).isoformat(),
                                         (ts + timedelta(hours=2)).isoformat())).fetchone()[0]
            if not ready:
                out.append({"key": key, "low_ts": ts.isoformat(), "low": p,
                            "gain_24h": round(max(p2 for _, p2 in window) / p - 1, 3)})
    # 合并同卡相邻低点（同卡 24h 内只报一次）
    merged, last_by_key = [], {}
    for m in sorted(out, key=lambda x: x["low_ts"]):
        prev = last_by_key.get(m["key"])
        if prev and (datetime.fromisoformat(m["low_ts"]) - prev) < timedelta(hours=24):
            continue
        last_by_key[m["key"]] = datetime.fromisoformat(m["low_ts"])
        merged.append(m)
    return merged


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sigs = [dict(r) for r in conn.execute("SELECT * FROM signals ORDER BY id").fetchall()]
    ready_scores, rally_scores, mm_scores = [], [], []
    for s in sigs:
        if s["type"] in ("FLIP_READY", "CANDIDATE_READY"):
            v, g, d = score_ready(conn, s)
            ready_scores.append({"id": s["id"], "name": s["name"], "verdict": v,
                                 "gain": g, "dd": d})
        elif s["type"] == "RALLY":
            v, g = score_rally(conn, s)
            rally_scores.append({"id": s["id"], "name": s["name"], "verdict": v, "gain": g})
        elif s["type"] == "MARKET_MOVE":
            mm_scores.append({"id": s["id"], "name": s["name"],
                              "verdict": score_market_move(s)})
    missed = missed_bottoms(conn)
    conn.close()
    def tally(rows):
        t = {}
        for r in rows:
            t[r["verdict"]] = t.get(r["verdict"], 0) + 1
        return t
    print(json.dumps({
        "ready_signals": len(ready_scores), "ready_tally": tally(ready_scores),
        "ready_detail": ready_scores,
        "rally_signals": len(rally_scores), "rally_tally": tally(rally_scores),
        "market_move_signals": len(mm_scores), "market_move_tally": tally(mm_scores),
        "missed_bottoms": missed,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
