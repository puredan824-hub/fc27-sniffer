"""fut.gg R2 全量价格源（2026-10-27 接入，GitHub 复扫发现）。

fut.gg 把全量价格库以静态 JSON 发布在 Cloudflare R2（其网站/App 自用，无鉴权）：
  manifest.json 给各文件 hash 与 _published_at；文件 URL = <key>.v<manifest._version>.<hash>.json。
  player-prices-index（v2：id0 + delta 数组 d）解码出 cardId 序列；
  player-prices-ps5-dyn（v2：p 价 / s 状态，与 index 等长对齐，p[0] 为占位）；
  streamlined-pool 的 o/r 数组同序对齐评分/稀有度组。R2 cardId == fut.gg players API 的 eaId（实测对齐）。
刷新实测每约 2.5–5 分钟一版全量（20,811 张卡）。价格为 fut.gg 口径，与 futbin 小有差异：
本源用于高频序列与全市场初筛，下单前仍以 futbin 实读核价。

每轮 cycle：
  1. 拉 manifest；若 ps5-dyn 的 published_at 未变则跳过（输出 skipped）。
  2. 全量快照存 r2_snaps/ 环（仅 price>=900，保留最近 24 版）。
  3. 与约 1h 前快照 diff 全市场异动（评分>=80、价 5,000–500,000、可交易、|chg|>=12%），
     前 15 名懒解析卡名（players API 价带查 + eaId 精确匹配，缓存 r2_cards.json），写 r2_movers.json；
     其中 |chg|>=25%、评分>=82 的前 5 名另发 MARKET_MOVE 信号入 signals 表（6h 同卡冷却），
     由 hook 处理员用 futbin 复核后决定是否通知用户。
注意（2026-10-27 实测）：fut.gg 与 futbin 对冷门色卡价差可达 30%+（如 Fabinho fut.gg 70,000 vs
futbin 102,000），R2 绝对价不进任何判定/样本序列，只用其同源快照相对涨跌做全市场发现。
映射表 r2_map.json（futbin_id -> eaId）保留作 eaId 对照，不再据此写 samples。
失败不抛异常：输出 {"error": ...} 且退出码 0，hook 照常跑 futbin 管线。
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from pricedb import connect  # noqa: E402

HERE = Path(__file__).parent
R2 = "https://r2.fut.gg/27"
API = "https://www.fut.gg/api/fut/players/v2/27/"
TZ = ZoneInfo("Asia/Shanghai")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36", "Accept": "application/json"}
MAP_PATH = HERE / "r2_map.json"
CARDS_PATH = HERE / "r2_cards.json"
MOVERS_PATH = HERE / "r2_movers.json"
SNAP_DIR = HERE / "r2_snaps"
UNTRADEABLE_S = {1, 2, 4}  # sbcoverlay 实测：s in {1,2,4} 为不可交易（SBC/目标/绝版锁定）


def get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def decode_ids(index: dict) -> list[int]:
    ids, cur = [], index["id0"]
    d = index["d"]
    for i in range(len(d) + 1):
        if i:
            cur += d[i - 1]
        ids.append(cur)
    return ids


def fetch_market() -> dict:
    man = get_json(f"{R2}/manifest.json")
    ver = man["_version"]
    pub = int(man["_published_at"]["player-prices-ps5-dyn"])

    def blob(key):
        return get_json(f"{R2}/{key}.v{ver}.{man[key]}.json")

    index = blob("player-prices-index")
    dyn = blob("player-prices-ps5-dyn")
    pool = blob("streamlined-pool")
    if index.get("v") != 2 or dyn.get("v") != 2:
        raise ValueError(f"format changed: index v={index.get('v')} dyn v={dyn.get('v')}")
    ids = decode_ids(index)
    if len(dyn["p"]) != len(ids) or len(pool["o"]) != len(ids):
        raise ValueError("array length mismatch")
    market = {}
    for i, cid in enumerate(ids):
        market[cid] = {
            "price": int(dyn["p"][i]), "status": int(dyn["s"][i]),
            "rating": int(pool["o"][i]), "rarity_grp": int(pool["r"][i]),
        }
    return {"published_at": pub, "market": market}


def resolve_names(market: dict, ea_ids: list[int]) -> dict:
    """players API 按价带查 + eaId 精确匹配，缓存到 r2_cards.json。"""
    cache = {}
    if CARDS_PATH.exists():
        cache = json.loads(CARDS_PATH.read_text())
    todo = [e for e in ea_ids if str(e) not in cache]
    for ea in todo[:10]:
        price = market[ea]["price"]
        lo = max(200, int(price * 0.94)); hi = int(price * 1.06) + 100
        try:
            d = get_json(f"{API}?sorts=current_price&price__gte={lo}&price__lte={hi}&pageSize=100")
            for r in d.get("data", []):
                cache[str(r["eaId"])] = {
                    "name": r.get("commonName") or r.get("cardName"),
                    "rating": r.get("overall"), "rarity": r.get("rarityName"),
                }
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    CARDS_PATH.write_text(json.dumps(cache, ensure_ascii=False))
    return cache


def cycle() -> dict:
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    snap = fetch_market()
    pub = snap["published_at"]
    market = snap["market"]
    SNAP_DIR.mkdir(exist_ok=True)
    snap_file = SNAP_DIR / f"snap-{pub}.json"
    if snap_file.exists():
        return {"ts": ts, "skipped": "published_at unchanged", "published_at": pub}

    # 1) 快照环（仅 price>=900 的精简表）
    slim = {str(cid): m["price"] for cid, m in market.items() if m["price"] >= 900}
    snap_file.write_text(json.dumps(slim))
    snaps = sorted(SNAP_DIR.glob("snap-*.json"))
    for old in snaps[:-24]:
        old.unlink()

    # 3) 与约 1h 前快照 diff 全市场异动
    movers, base_age = [], None
    cands = [s for s in snaps if s.name != snap_file.name]
    if cands:
        def snap_ts(p):
            return int(p.name[5:-5])
        target = pub - 3600
        base = min(cands, key=lambda p: abs(snap_ts(p) - target))
        base_age = round((pub - snap_ts(base)) / 60)
        prev = json.loads(base.read_text())
        for cid_s, old_price in prev.items():
            cid = int(cid_s)
            m = market.get(cid)
            if not m or not old_price:
                continue
            new = m["price"]
            if new <= 0 or m["status"] in UNTRADEABLE_S:
                continue
            if m["rating"] < 80 or not (5000 <= new <= 500000):
                continue
            chg = (new - old_price) / old_price
            if abs(chg) >= 0.12:
                movers.append({"eaId": cid, "name": None, "rating": m["rating"],
                               "price": new, "prev": old_price, "chg": round(chg, 3)})
        movers.sort(key=lambda x: -abs(x["chg"]))
        movers = movers[:15]
        if movers:
            cache = resolve_names(market, [m["eaId"] for m in movers])
            for m in movers:
                c = cache.get(str(m["eaId"]))
                if c:
                    m["name"] = c["name"]; m["rarity"] = c.get("rarity")
        # 大异动另发 MARKET_MOVE 信号，hook 处理员用 futbin 复核后决定是否通知用户
        emitted = 0
        try:
            conn = connect()
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS signals("
                " id INTEGER PRIMARY KEY, ts TEXT NOT NULL, key TEXT NOT NULL, name TEXT,"
                " type TEXT NOT NULL, price INTEGER, detail TEXT, acked INTEGER DEFAULT 0);")
            cutoff = (datetime.now(TZ) - timedelta(hours=6)).isoformat(timespec="seconds")
            for m in movers:
                if abs(m["chg"]) < 0.25 or m["rating"] < 82 or emitted >= 5:
                    continue
                key = f"r2:{m['eaId']}"
                dup = conn.execute(
                    "SELECT COUNT(*) FROM signals WHERE key=? AND type='MARKET_MOVE' AND ts>=?",
                    (key, cutoff)).fetchone()[0]
                if dup:
                    continue
                detail = {"chg_1h": m["chg"], "r2_price": m["price"], "r2_prev": m["prev"],
                          "rating": m["rating"], "rarity": m.get("rarity"),
                          "baseline_age_min": base_age,
                          "note": "fut.gg 口径相对涨跌，判定与下单以 futbin 实读为准"}
                with conn:
                    conn.execute(
                        "INSERT INTO signals(ts,key,name,type,price,detail) VALUES(?,?,?,?,?,?)",
                        (ts, key, m["name"] or f"eaId {m['eaId']}", "MARKET_MOVE",
                         m["price"], json.dumps(detail, ensure_ascii=False)))
                emitted += 1
            conn.close()
        except Exception:  # noqa: BLE001
            emitted = -1
    out = {"ts": ts, "published_at": pub, "cards": len(market),
           "signals_emitted": emitted if cands else 0,
           "baseline_age_min": base_age, "movers": movers}
    MOVERS_PATH.write_text(json.dumps(out, ensure_ascii=False))
    return out


if __name__ == "__main__":
    try:
        print(json.dumps(cycle(), ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
