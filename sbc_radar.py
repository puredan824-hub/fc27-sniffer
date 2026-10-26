"""FC27 SBC 雷达：futcli 拉 fut.gg 全量 SBC/进化 → 快照 diff → 告警判定 + 落库。

触发（triggers）：
- new_sbc / new_evo：上轮快照没有、本轮新出现
- expiring：剩余时间从 >24h 跨入 <=24h
- price_move：fut.gg 估价较上轮变化 >= ±20%（且两轮都有价）
removed 只记录不告警。首轮无快照时只建基线、triggers 为空。
输出：stdout 一行 JSON {ts, baseline, counts, triggers, removed}
状态：state/last_snapshot.json
"""
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from pricedb import insert_samples  # noqa: E402

import os
FUTCLI = os.environ.get("FUTCLI_BIN", "futcli")
STATE_DIR = Path(__file__).parent / "state"
SNAPSHOT = STATE_DIR / "last_snapshot.json"
TZ = ZoneInfo("Asia/Shanghai")
# 实测（2026-10-25）：sbc.icons / sbc.swaps 在 futcli 0.2.0 输出纯文本 "Invalid SBC option."，不可用，已剔除；
# evolutions 当前返回空列表（fut.gg 页面口径问题），保留调用但可能长期为空。
SBC_CATEGORIES = ["sbc.challenges", "sbc.foundations", "sbc.players", "sbc.upgrades"]
PRICE_MOVE_THRESHOLD = 0.20
EXPIRING_HOURS = 24.0


def parse_price(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    return int(s) if s.isdigit() else None


def parse_exp_hours(raw) -> float | None:
    if not raw:
        return None
    s = str(raw).lower()
    total = 0.0
    found = False
    for pat, mult in ((r"(\d+(?:\.\d+)?)\s*day", 24.0), (r"(\d+(?:\.\d+)?)\s*hour", 1.0), (r"(\d+(?:\.\d+)?)\s*min", 1 / 60)):
        m = re.search(pat, s)
        if m:
            total += float(m.group(1)) * mult
            found = True
    return total if found else None


WARNINGS: list[str] = []


def fetch(category: str) -> list[dict]:
    out = subprocess.run(
        [str(FUTCLI), "-o", "json", category],
        capture_output=True, text=True, timeout=180,
    )
    if out.returncode != 0:
        raise RuntimeError(f"futcli {category} failed: {out.stderr.strip()[:200]}")
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        WARNINGS.append(f"{category}: 非 JSON 输出，已跳过（{out.stdout.strip()[:60]!r}）")
        return []
    if not isinstance(data, list):
        WARNINGS.append(f"{category}: 输出非列表，已跳过")
        return []
    return data


def collect() -> dict[str, dict]:
    items: dict[str, dict] = {}
    for cat in SBC_CATEGORIES:
        for row in fetch(cat):
            name = str(row.get("Name", "")).strip()
            if not name:
                continue
            items[f"{cat}:{name}"] = {
                "kind": "sbc",
                "name": name,
                "price": parse_price(row.get("Price")),
                "exp_hours": parse_exp_hours(row.get("Expiration")),
                "is_new_flag": row.get("New"),
                "repeatable": row.get("Repeatable"),
            }
    for row in fetch("evolutions"):
        name = str(row.get("Name", "")).strip()
        if not name:
            continue
        items[f"evo:{name}"] = {
            "kind": "evo",
            "name": name,
            "price": parse_price(row.get("Price")),
            "exp_hours": parse_exp_hours(row.get("Expiration")),
            "is_new_flag": row.get("New"),
            "repeatable": row.get("Repeatable"),
        }
    return items


def main() -> int:
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    try:
        current = collect()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ts": ts, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    prev = None
    if SNAPSHOT.exists():
        try:
            prev = json.loads(SNAPSHOT.read_text()).get("items")
        except Exception:  # noqa: BLE001
            prev = None

    triggers: list[dict] = []
    removed: list[str] = []
    baseline = prev is None
    if prev is not None:
        for key, cur in current.items():
            old = prev.get(key)
            if old is None:
                triggers.append({
                    "type": "new_sbc" if cur["kind"] == "sbc" else "new_evo",
                    "name": cur["name"], "price": cur["price"], "exp_hours": cur["exp_hours"],
                })
                continue
            if (
                cur["exp_hours"] is not None and old.get("exp_hours") is not None
                and old["exp_hours"] > EXPIRING_HOURS >= cur["exp_hours"] > 0
            ):
                triggers.append({
                    "type": "expiring", "name": cur["name"],
                    "price": cur["price"], "exp_hours": cur["exp_hours"],
                })
            if cur["price"] and old.get("price"):
                pct = (cur["price"] - old["price"]) / old["price"]
                if abs(pct) >= PRICE_MOVE_THRESHOLD:
                    triggers.append({
                        "type": "price_move", "name": cur["name"],
                        "old_price": old["price"], "new_price": cur["price"],
                        "pct": round(pct * 100, 1),
                    })
        removed = sorted(k for k in prev if k not in current)

    SNAPSHOT.write_text(json.dumps({"ts": ts, "items": current}, ensure_ascii=False, indent=1))
    n = insert_samples([
        {"ts": ts, "kind": v["kind"], "key": k, "name": v["name"],
         "price": v["price"], "exp_hours": v["exp_hours"], "source": "fut.gg/futcli"}
        for k, v in current.items()
    ])
    print(json.dumps({
        "ts": ts, "baseline": baseline,
        "counts": {"items": len(current), "db_rows": n},
        "triggers": triggers, "removed": removed, "warnings": WARNINGS,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
