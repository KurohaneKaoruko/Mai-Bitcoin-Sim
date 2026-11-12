# mai-bitcoin-sim 麦麦炒币模拟器

让麦麦（MaiBot）用**真实加密货币行情**在本地模拟下单的插件。麦麦有自己的虚拟钱包，群友可以指挥（也可以让麦麦自己决定）买卖加密货币，持仓盈亏实时按真实价格结算。

> 🎮 纯模拟盘，不涉及任何真实资金。

## 功能

- 📡 **真实行情**：接入 Binance / OKX / Gate.io / Coinbase 四家公共行情 API（无需 API key），启动自动测速选最快的源，失败自动降级
- 💰 **现货交易**：按数量或金额（`100u`）买入、卖出、清仓
- 📉 **永续合约**：杠杆做多/做空、双向爆仓监控与主动告警（含临近预警）、资金费每 8 小时按真实费率结算
- 🎯 **止盈止损**：现货与合约仓位均支持，后台巡检自动平仓
- 🔔 **价格警报**：上穿/下破目标价自动提醒（含 AI Tool，可对话式设置）
- 🔁 **定投计划**：周期自动买入（DCA），可暂停/恢复/删除
- 📊 **K线图 & 资产曲线**：`render.html2png` 渲染成图片发送，失败自动降级文字迷你图
- 🗓 **每日报告**：定时汇报当日盈亏与持仓（时间可配）
- 🏆 **排行榜**：chat 模式下各钱包总资产排名
- 🤖 **AI 自主交易**：5 个 Tool 让麦麦在聊天中自己查行情、下单、设警报、汇报持仓——「麦麦全仓梭哈ETH」
- 🖥 **WebUI 仪表盘**：MaiBot WebUI 侧栏「炒币模拟器」页面，实时查看总览/持仓/合约/成交/资产曲线（30s 自动刷新）
- 👛 **钱包模式**：`global` 全局单钱包（默认）或 `chat` 每聊天流独立

## 安装

放入 MaiBot 的 `plugins/` 目录：

```bash
git clone https://github.com/KurohaneKaoruko/Mai-Bitcoin-Sim.git plugins/mai-bitcoin-sim
```

重启 MaiBot（或在 WebUI 插件管理中启用）。无需额外 Python 依赖（纯标准库）。

## 命令

入口 `/coin`（别名 `/币`、`/炒币`），支持中文子命令别名：

| 命令 | 说明 |
|---|---|
| `/coin` | 资产总览（现金/持仓/空单/收益率） |
| `/coin help` | 帮助 |
| `/coin price BTC [ETH...]` | 实时价格 + 24h 涨跌（多币可一起查；支持中文币名：`/coin price 比特币`） |
| `/coin funding [BTC]` | 当前资金费率（缺省 = 持仓币种） |
| `/coin buy BTC 100u` | 买入 100 U 的 BTC（`/coin buy BTC 0.01` 按数量） |
| `/coin sell BTC all` | 卖出（`all` 清仓，或写数量） |
| `/coin short ETH 100u 5x` | 合约开空：名义 100U × 5 倍杠杆 |
| `/coin long ETH 100u 5x` | 合约开多（杠杆做多） |
| `/coin close ETH [all] [long\|short]` | 平合约单（多空只持一边时可省 side） |
| `/coin pos` | 持仓明细 |
| `/coin history 20` | 最近成交 |
| `/coin kline BTC [1h] [96]` | K线图（周期 5m/15m/1h/4h/1d） |
| `/coin curve` | 资产曲线 |
| `/coin dca add BTC 100u 6h` | 定投：每 6h 买 100U 的 BTC |
| `/coin dca list / del <id> / pause <id> / resume <id>` | 定投管理 |
| `/coin alert add BTC >= 100000` | 价格警报（list / del <id>） |
| `/coin tp BTC 110000` | 设止盈（`sl` 同理；`clear` 清除；现货/多单/空单并存时加 `spot`/`long`/`short`） |
| `/coin rank` | 钱包排行榜（chat 模式） |
| `/coin market` | 大盘概览（BTC/ETH/SOL/BNB） |
| `/coin source` | 四大交易所测速 |
| `/coin notify` | 查看/检测主动通知目标 |
| `/coin reset 确认` | 重置钱包 |

## 配置

安装后自动生成 `config.toml`，可在 WebUI 中编辑：

| 配置 | 默认 | 说明 |
|---|---|---|
| `main.initial_cash` | `100000` | 初始资金（USDT） |
| `main.wallet_mode` | `global` | `global` 全局单钱包 / `chat` 每聊天流独立 |
| `trading.spot_fee` | `0.001` | 现货手续费率 |
| `trading.futures_fee` | `0.0005` | 合约手续费率 |
| `trading.default_leverage` | `3` | 合约默认杠杆 |
| `trading.max_leverage` | `20` | 杠杆上限 |
| `trading.funding_settlement` | `true` | 是否结算永续资金费（每 8 小时） |
| `trading.funding_default` | `0.0001` | 资金费率获取失败时的兜底费率 |
| `trading.allow_ai_trade` | `true` | 允许麦麦 AI 自主买/卖 |
| `trading.allow_ai_short` | `true` | 允许麦麦 AI 开空 |
| `trading.allow_ai_long` | `true` | 允许麦麦 AI 开多 |
| `trading.maintenance_margin_rate` | `0.005` | 维持保证金率（爆仓线） |
| `market.price_cache_ttl` | `10` | 价格缓存秒数 |
| `chart.kline_interval` / `kline_limit` | `1h` / `72` | 默认 K 线参数 |
| `task.tick_seconds` | `60` | 后台巡检间隔（资金费/爆仓/止盈止损/警报/定投/快照） |
| `task.liquidation_alert` | `true` | 爆仓主动告警 |
| `task.liquidation_near_pct` | `10` | 临近爆仓预警阈值（距爆仓价 %，0=关闭，每仓 30 分钟最多一次） |
| `task.daily_report` | `true` | 每日收益报告 |
| `task.report_hour` | `20` | 每日报告发送时间（24h 制） |
| `task.alert_targets` | `[]` | 主动通知（爆仓/定投异常）发送目标：群号或QQ号列表；留空则发给钱包最近活跃的聊天流 |

## 数据与规则

- 钱包数据持久化在 `data/plugins/kurohanekaoruko.mai-bitcoin-sim/`，原子写入，防崩溃损坏
- 永续合约规则（简化 USDT 本位）：
  - 同一币种多/空各可持一单；开仓占用保证金 = 名义价值 / 杠杆
  - 空单爆仓价 = 开仓价 × (1 + 1/杠杆 − 维持保证金率)；多单爆仓价 = 开仓价 × (1 − 1/杠杆 + 维持保证金率)
  - 标记价触及爆仓线 → 损失全部保证金，插件主动发爆仓警报
  - 资金费每 8 小时（UTC 0/8/16 点）结算：多头付/空头收 `名义价值 × 费率`（负费率反向）；
    费率取自交易所真实资金费率（Binance fapi / OKX SWAP / Gate 合约，先成功者胜），不可达时用兜底费率
- 止盈止损：现货与多单 TP 在上方/SL 在下方；空单相反（TP 下方/SL 上方）。触发即全量平仓（免手续费），方向设反会立即触发（有提示但不阻止）
- 价格警报触发后自动删除，通知发到设置时的聊天流
- 爆仓等主动通知的送达目标由 `task.alert_targets` 控制（群号/QQ号，多个逐个发送）；
  未配置时发给钱包最近活跃的聊天流。目标需是麦麦已知的会话（在该群/私聊里说过话），
  可用 `/coin alert` 实时检测解析结果
- Coinbase 源为 USD 计价（与 USDT 按 1:1 近似处理）

## 开发

```
mai-bitcoin-sim/
├── _manifest.json      # 插件清单
├── plugin.py           # 入口：命令路由 + Tool + 生命周期 + 后台任务
├── core/
│   ├── market.py       # 四交易所行情聚合：测速/降级/缓存/K线
│   ├── wallet.py       # 钱包引擎：现货/空单/爆仓/定投/持久化
│   └── chart.py        # K线 & 资产曲线 SVG（红涨绿跌）
└── tests/              # 离线 + SDK 集成测试
```

测试（SDK 需解压到某目录并指定 PYTHONPATH）：

```bash
python3 tests/test_offline.py                  # 核心逻辑 + 实网行情
PYTHONPATH=/path/to/maibot_sdk python3 tests/test_sdk.py   # 真实 SDK 全流程
```

## License

MIT
