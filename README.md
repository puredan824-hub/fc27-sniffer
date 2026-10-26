# FC27 Sniffer · 探嗅引擎

**English:** A real-time market sniffer for EA FC 27 Ultimate Team on PlayStation. It polls special-card prices every five minutes, classifies market conditions with a trend state machine, scans more than 20,000 cards for abnormal moves, backtests every signal, and cautiously self-tunes its decision thresholds when the evidence is strong enough.

**中文：** EA FC 27 Ultimate Team（PS 平台）市场实时探嗅系统。5 分钟级轮询特殊卡价格，用趋势状态机判定「还在跌 / 触底 / 企稳可入场 / 已反弹」，并接入 fut.gg 全量价格库扫描 2 万多张卡的异常波动；所有信号都会回测打分，证据达标时判定阈值才自动微调。

[English](#english-documentation) · [中文说明](#中文说明)

> **Discovery and decision support only.** This project contains no account login or order-execution code. A signal is market intelligence for a human or a separate execution layer; it is not an unconditional trading instruction.
>
> **只做发现与判定。** 本项目不含任何账号登录或下单执行代码。信号是给人（或你自己的执行层）使用的市场情报，不是无条件买卖指令。

## English Documentation

### Overview

Hourly manual market checks are too slow for short-lived special-card opportunities. A card can finish a sell-off and stabilize for only a few dozen minutes before rebounding; by the next manual check, the entry may be gone—or a trader may mistake a brief pause in a continuing decline for stabilization and “catch a falling knife.”

FC27 Sniffer encodes a mean-reversion (“flip”) discipline into a continuously running state machine:

- **Explicit entry rules:** time since the latest new low, 45-minute price range, one-hour slope, and drawdown from the historical high.
- **Edge-triggered alerts:** a signal is emitted when a card transitions into `READY` or `RALLY`, with a six-hour cooldown for the same signal.
- **Strict source separation:** FUTBIN and fut.gg can differ by more than 30% on illiquid special cards. Their absolute prices are never mixed into one decision series, because that would create false lows and false slopes.
- **Fresh-price verification:** engine output is a preliminary classification. Any downstream action must verify the latest independently read price first.
- **Backtest-driven evolution:** historical signals are graded every day. Threshold changes require an evidence gate, are bounded, change at most one parameter at a time, and are recorded in the parameter history.

### Architecture

```text
Data layer (sources kept separate)
├─ FUTBIN player pages (SSR) ───── watchlist prices + recent-price series
├─ fut.gg R2 full price dump ───── 20,000+ cards, market-wide discovery
├─ fut.gg SBC / Price Ranges ───── demand and official repricing events
├─ FUTBIN segmented list sampling ─ 83–88 rated fodder floor index
└─ FUTBIN market movers ─────────── new themes outside the watchlist
                              ↓
                    prices.db (SQLite time series)
                              ↓
Decision layer: FALLING / BOTTOMING / NEUTRAL / READY / RALLY
Features: latest-new-low age · 1h slope · 45m range · historical drawdown
                              ↓ edge-triggered, 6h cooldown
Signals: FLIP_READY · CANDIDATE_READY · RALLY · MARKET_MOVE
                              ↓ fresh-price verification
Evolution: daily backtest → evidence gate → bounded parameter tuning
```

### Components

| File | Purpose | Example |
|---|---|---|
| `futbin_prices.py` | Fetch watchlist prices from FUTBIN's server-rendered player pages and store them automatically | `python futbin_prices.py` |
| `floor_index.py` | Build 83–88 rating fodder floor indices from segmented list samples | `python floor_index.py` |
| `movers_scan.py` | Collect market gainers/losers, then verify each candidate on its player page | `python movers_scan.py` |
| `r2_prices.py` | Snapshot fut.gg's full R2 price feed and detect roughly one-hour same-source moves | `python r2_prices.py` |
| `sbc_radar.py` | Diff SBC/evolution releases, expiries, and estimated completion costs through futcli | `python sbc_radar.py` |
| `price_ranges_radar.py` | Diff EA Price Ranges events, prioritizing extinct cards | `python price_ranges_radar.py` |
| `sniffer.py` | The core trend state machine and signal queue | `sniffer.py cycle \| pending \| ack <id...>` |
| `sniffer_eval.py` | Grade historical signals, detect missed bottoms, and emit JSON for tuning decisions | `python sniffer_eval.py` |
| `pricedb.py` | SQLite storage layer | Imported by other modules |
| `record_prices.py` | Insert a manually verified price observation into the time series | `echo '[{...}]' \| python record_prices.py` |

Configuration lives in `watchlist.json`, the `SPECIALS` / `TRIGGERS` / `SEED_HIGHS` sections in `sniffer.py`, `r2_map.json`, and the external decision thresholds in `sniffer_params.json`.

### Signals

| Signal | Meaning |
|---|---|
| `FLIP_READY` | A tracked card meets every entry rule: price is at or below its trigger, no new low for at least 45 minutes, 45-minute range is at most 4%, and the one-hour slope is at least −1%. |
| `CANDIDATE_READY` | An untracked card has drawn down at least 30% from its historical high and appears stabilized; intelligence only. |
| `RALLY` | The rebound slope from a low meets the configured threshold; useful as a potential exit window, not a reason to chase. |
| `MARKET_MOVE` | A same-source R2 move of at least 25% in about one hour for a card rated 82+; it must be confirmed in the same direction with FUTBIN data. |

### Self-tuning safeguards

Only READY signals older than 12 hours are scored. Three or more false stabilizations tighten one parameter; three or more missed bottoms with no more than one false stabilization loosen one parameter; at least eight scored signals with a GOOD rate of 60% or higher and zero false stabilizations may justify a small loosening. Each run changes no more than one parameter, limits the step to 25% (or 0.01 for slope/range parameters), prevents reversing the same parameter within 48 hours, and appends the evidence to `history`. Insufficient evidence means no change.

### Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Fetch one round of watchlist prices; this creates and populates prices.db
.venv/bin/python futbin_prices.py

# Run the sniffer and inspect or acknowledge queued signals
.venv/bin/python sniffer.py cycle
.venv/bin/python sniffer.py pending
.venv/bin/python sniffer.py ack 1

# Start the market-wide R2 baseline; meaningful diffs need about one hour
.venv/bin/python r2_prices.py

# Backtest historical signals
.venv/bin/python sniffer_eval.py
```

Before live use, replace `watchlist.json` with your own cards and fill in your observation set, trigger prices, and historical-high seeds in `sniffer.py`. See `examples/` for a five-minute polling script and a reference crontab.

### Field lessons built into the code

- FUTBIN's internal API and public web pages are different access paths; complete player slugs are required, and bare numeric IDs may return 404.
- Do not scan a broad price band in ascending-price order to find a rating segment: huge numbers of cheap low-rated cards will bury it. Use a narrow price band and FUTBIN's default rating-descending order.
- One-snapshot price spikes are common. A dramatic move may reverse in the next snapshot, so cross-source or next-snapshot verification is mandatory.
- Official Price Ranges events—especially extinct cards—can precede visible market moves and deserve a separate radar.
- fut.gg's player API returns only about 24 records per default page; price-band lookups must paginate to avoid missing expensive same-name versions.

### Disclaimer

This is an independent research project and is not affiliated with EA, FUTBIN, or fut.gg. Data comes from public pages; evaluate the applicable terms yourself. Markets are risky, signals are not financial advice, and the mandatory fresh-price verification step should never be bypassed.

---

## 中文说明

## 为什么做这个

每小时看一轮盘、靠肉眼读价格序列判企稳，机会窗口经常就这么错过：卡跌透企稳可能只有几十分钟，等下一轮人工盯盘时要么已经反弹，要么还在阴跌里误判「走平了」接了飞刀。探嗅引擎把一套 flip（砸盘企稳均值回归）纪律编码成状态机，常驻高频运行：

- **判定标准写死在参数里**，不靠临场感觉：多久没创新低、振幅收没收窄、1 小时斜率止没止稳、离历史高点回撤够不够。
- **边沿触发**：只在状态跃迁进 READY/RALLY 的那一刻发信号，同信号 6 小时冷却，不刷屏。
- **双源分工防混源**：futbin 与 fut.gg 对冷门色卡的绝对价差实测可达 30%+，混进同一条时间序列会造出假新低、假斜率。futbin 序列是唯一的判定源；fut.gg R2 全量源只用**同源相对涨跌**做全市场发现，绝对价永不进判定。
- **信号要复核**：引擎给的是预判，执行前必须用最新实读价再验一次。实测中出现过「29,750 判 READY、3 分钟后跌到 29,000」的假企稳，复核环节就是为它设的。
- **判定会被回测纠正**：每天对历史信号打分（真企稳 / 假企稳 / 漏检），证据达标才允许动一个参数、有界微调、全程留痕——引擎自己变准，而不是越调越飘。

## 架构

```
数据层（多源，口径隔离）
├─ futbin.com 球员页 SSR 直读 ── watchlist 现价 + recent 序列 ──┐
├─ fut.gg R2 全量价格库（约 4 分钟一版，2 万+ 卡）── 异动初筛 ─┤
├─ fut.gg SBC / Price Ranges 雷达 ── 需求与官方调价事件 ──────┤
├─ futbin 列表页分段采样 ── 83–88 评分段 fodder 地板指数 ─────┤
└─ futbin 全市场涨跌榜 ── 名单外新主题发现 ───────────────────┘
                              ↓ 全部落库
                    prices.db（SQLite 时间序列）
                              ↓
判定层  sniffer 状态机：FALLING / BOTTOMING / NEUTRAL / READY / RALLY
        特征 = 6h 最近创新低距今 · 1h 斜率 · 45min 振幅 · 历史高点回撤
                              ↓ 边沿触发（6h 冷却）
信号层  FLIP_READY · CANDIDATE_READY · RALLY · MARKET_MOVE
                              ↓ 复核后消费（信号不替代新鲜实读）
进化层  sniffer_eval 每日回测 → 证据门 → 有界微调 sniffer_params.json
```

## 组件

| 文件 | 作用 | 用法 |
|---|---|---|
| `futbin_prices.py` | watchlist 批量取价（futbin.com SSR），现价自动落库 | `python futbin_prices.py` |
| `floor_index.py` | 83–88 段 fodder 地板指数（列表页价格带 + 默认评分降序采样，每段最便宜 10 张中位数） | `python floor_index.py` |
| `movers_scan.py` | 全市场涨/跌幅榜候选 + 球员页逐张核实价 | `python movers_scan.py` |
| `r2_prices.py` | fut.gg R2 全量源：快照环 + 约 1h 同源 diff，≥25% 异动发 MARKET_MOVE 信号 | `python r2_prices.py` |
| `sbc_radar.py` | SBC/进化雷达（经 futcli 拉 fut.gg 数据）：新上线 / 临期 / 估价跳变 diff | `python sbc_radar.py` |
| `price_ranges_radar.py` | EA 官方调价事件雷达（fut.gg Price Ranges 页 diff，断货优先） | `python price_ranges_radar.py` |
| `sniffer.py` | **探嗅引擎本体**：状态机判定 + 信号发射/消费 | `sniffer.py cycle \| pending \| ack <id...>` |
| `sniffer_eval.py` | 信号回测评分 + 漏检扫描，输出 JSON 供自优化决策 | `python sniffer_eval.py` |
| `pricedb.py` | SQLite 存储层（samples / range_events 等表） | 被其他模块 import |
| `record_prices.py` | 手工补录一轮实读价格进序列库 | `echo '[{...}]' \| python record_prices.py` |

配置：`watchlist.json`（取价名单）、`sniffer.py` 内 `SPECIALS`（探嗅观察面）/ `TRIGGERS`（flip 触发价）/ `SEED_HIGHS`（历史高点种子）、`r2_map.json`（futbin_id → fut.gg eaId 对照）、`sniffer_params.json`（判定阈值，外置可调）。

## 信号语义

| 信号 | 含义 |
|---|---|
| `FLIP_READY` | 名单内卡满足全部入场纪律：现价 ≤ 触发价、≥45 分钟未创新低、45 分钟振幅 ≤4%、1h 斜率 ≥ −1% |
| `CANDIDATE_READY` | 名单外卡自历史高点回撤 ≥30% 且企稳，只作候选情报 |
| `RALLY` | 自低点 1h 反弹斜率达标，持仓的卖出窗口 / 未持仓不追 |
| `MARKET_MOVE` | R2 全市场 1h 相对异动 ≥25%（评分 ≥82），须 futbin 复核同向才成立 |

## 判定参数（`sniffer_params.json`）

| 参数 | 默认 | 含义 | 自优化边界 |
|---|---|---|---|
| `falling_low_age_min` | 30 | 多久内创新低判 FALLING | [15, 60] |
| `falling_slope` | −0.03 | 1h 斜率 ≤ 此值判 FALLING | [−0.06, −0.015] |
| `ready_low_age_min` | 45 | 未创新低满此分钟数才可能 READY | [30, 90] |
| `ready_range45_max` | 0.04 | 45 分钟振幅上限 | [0.02, 0.08] |
| `ready_slope_min` | −0.01 | READY 的 1h 斜率下限 | [−0.03, 0] |
| `running_slope` | 0.04 | RALLY 的 1h 反弹斜率门槛 | [0.03, 0.08] |
| `dd_min` | 0.30 | READY 的历史高点回撤门槛 | [0.22, 0.45] |
| `rally_dd_min` | 0.20 | RALLY 的回撤门槛 | [0.10, 0.35] |

自优化规则（每日一轮）：只给发出满 12 小时的 READY 信号计票；假企稳 ≥3 收紧、漏检 ≥3 且假企稳 ≤1 放宽、已计票 ≥8 且优良率 ≥60% 且零假企稳可小幅放宽；每轮最多改 1 个参数、步长 ≤25%、同参数 48 小时内不得反向；每次改动写回 `history` 留痕。样本不足就明确不动。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1) 取一轮 watchlist 价格（自动建库落库）
.venv/bin/python futbin_prices.py

# 2) 跑一轮探嗅判定
.venv/bin/python sniffer.py cycle
.venv/bin/python sniffer.py pending     # 看未处理信号
.venv/bin/python sniffer.py ack 1       # 标记已处理

# 3) 全市场发现（首轮只建基线快照，约 1 小时后异动开始有意义）
.venv/bin/python r2_prices.py

# 4) 每日回测
.venv/bin/python sniffer_eval.py
```

上线前按自己的打法改三处：`watchlist.json` 换成你的名单；`sniffer.py` 的 `SPECIALS` / `TRIGGERS` / `SEED_HIGHS` 填你的观察面与触发价；`sniffer_params.json` 一般不用动，让回测慢慢校准。

调度示例见 `examples/`：`poll.sh` 是单轮轮询脚本（R2 → 探嗅 → 有信号才输出），用 cron 每 5 分钟跑即可；判定与执行解耦，信号怎么消费（通知、写台账、喂执行层）由你自己的系统决定。

## 实测教训（都写进代码了）

- **futbin.org 内部 API 与 futbin.com 网页是两套通路**：前者可能被出口 IP 挡（403），后者 SSR 直读稳定；URL 必须带完整球员 slug，裸 ID 会 404。
- **地板采样别用价格升序扫宽带**：低价平台有海量低分卡，目标评分段永远翻不到。用价格带 + 默认评分降序，段内连续排布取最便宜一批。
- **单版跳价是常态**：全量源偶发单版假印（−33% 下一版弹回），任何异动都要第二源或下一版复验后才当真。
- **价格区间断货是先行信号**：EA 调价事件（尤其 Extinct）往往比价格异动早一步，雷达单独成线。
- **fut.gg players API 默认页只有约 24 条**，价带查询要分页抓全，否则会漏掉同名高价版本。

## 免责声明

个人研究工具，与 EA、futbin、fut.gg 无任何隶属关系；数据均来自公开页面，请自行评估使用方式与相关服务条款。市场有风险，信号不构成投资建议；本工具在实盘中的全部判定都保留了「人工/执行层复核」环节，请勿直接把信号当成无条件买卖指令。

## License

MIT © 2026 Puredan
