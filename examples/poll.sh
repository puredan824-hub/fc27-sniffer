#!/usr/bin/env bash
# 探嗅单轮轮询示例：R2 全量源 → 探嗅判定 → 有未处理信号才输出 JSON。
# 用法：crontab 每 5 分钟跑一次（见 crontab.txt）；输出可自行接通知/台账/执行层。
set -uo pipefail
cd "$(dirname "$0")/.."

# 先跑 fut.gg R2 全量源（快照 + 全市场异动 + MARKET_MOVE 信号），失败不阻断主线
.venv/bin/python r2_prices.py > /tmp/fc27-r2-cycle.json 2> /tmp/fc27-r2-err.log || true

# 探嗅主线：12 张色卡取价 + 状态机判定
if ! .venv/bin/python sniffer.py cycle > /tmp/fc27-sniffer-cycle.json 2> /tmp/fc27-sniffer-err.log; then
  echo "sniffer cycle failed:" >&2
  head -c 400 /tmp/fc27-sniffer-err.log >&2
  exit 0
fi

PENDING="$(.venv/bin/python sniffer.py pending)"
COUNT="$(printf '%s' "$PENDING" | python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])')"
if [ "${COUNT:-0}" -gt 0 ]; then
  # 有信号：打印出来供上层消费；消费方复核处理后用 sniffer.py ack <id> 标记
  printf '%s\n' "$PENDING"
fi
