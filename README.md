# mai-bitcoin-sim 麦麦炒币模拟器

让麦麦（MaiBot）用**真实加密货币行情**在本地模拟下单的插件。麦麦有自己的虚拟钱包，群友可以指挥（也可以让麦麦自己决定）买卖加密货币，持仓盈亏实时按真实价格结算。

> 🎮 纯模拟盘，不涉及任何真实资金。

## 功能

- 📡 **真实行情**：接入 Binance / OKX / Gate.io / Coinbase 四家公共行情 API（无需 API key），启动自动测速选最快的源，失败自动降级
- 💰 **现货交易**：按数量或金额（`100u`）买入、卖出、清仓
- 📉 **杠杆做空**：开空单、可设杠杆、爆仓线实时监控、爆仓主动告警
- 🔁 **定投计划**：周期自动买入（DCA），可暂停/恢复/删除
- 📊 **K线图 & 资产曲线**：`render.html2png` 渲染成图片发送，失败自动降级文字迷你图
- 🤖 **AI 自主交易**：4 个 Tool 让麦麦在聊天中自己查行情、下单、汇报持仓——「麦麦全仓梭哈ETH」
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
| `/coin price BTC` | 实时价格 + 24h 涨跌（支持中文币名：`/coin price 比特币`） |
| `/coin buy BTC 100u` | 买入 100 U 的 BTC（`/coin buy BTC 0.01` 按数量） |
| `/coin sell BTC all` | 卖出（`all` 清仓，或写数量） |
| `/coin short ETH 100u 5x` | 开空：名义 100U × 5 倍杠杆 |
| `/coin close ETH` | 平空单（可带数量） |
| `/coin pos` | 持仓明细 |
| `/coin history 20` | 最近成交 |
| `/coin kline BTC [1h] [96]` | K线图（周期 5m/15m/1h/4h/1d） |
| `/coin curve` | 资产曲线 |
| `/coin dca add BTC 100u 6h` | 定投：每 6h 买 100U 的 BTC |
| `/coin dca list / del <id> / pause <id> / resume <id>` | 定投管理 |
| `/coin market` | 大盘概览（BTC/ETH/SOL/BNB） |
| `/coin source` | 四大交易所测速 |
| `/coin alert` | 查看/检测爆仓通知目标解析是否成功 |
| `/coin reset 确认` | 重置钱包 |

## 配置

安装后自动生成 `config.toml`，可在 WebUI 中编辑：

| 配置 | 默认 | 说明 |
|---|---|---|
| `main.initial_cash` | `100000` | 初始资金（USDT） |
| `main.wallet_mode` | `global` | `global` 全局单钱包 / `chat` 每聊天流独立 |
| `trading.spot_fee` | `0.001` | 现货手续费率 |
| `trading.futures_fee` | `0.0005` | 做空手续费率 |
| `trading.default_leverage` | `3` | 开空默认杠杆 |
| `trading.max_leverage` | `20` | 杠杆上限 |
| `trading.allow_ai_trade` | `true` | 允许麦麦 AI 自主买/卖 |
| `trading.allow_ai_short` | `true` | 允许麦麦 AI 开空 |
| `trading.maintenance_margin_rate` | `0.005` | 维持保证金率（爆仓线） |
| `market.price_cache_ttl` | `10` | 价格缓存秒数 |
| `chart.kline_interval` / `kline_limit` | `1h` / `72` | 默认 K 线参数 |
| `task.tick_seconds` | `60` | 后台巡检间隔（爆仓/定投/快照） |
| `task.liquidation_alert` | `true` | 爆仓主动告警 |
| `task.alert_targets` | `[]` | 主动通知（爆仓/定投异常）发送目标：群号或QQ号列表；留空则发给钱包最近活跃的聊天流 |

## 数据与规则

- 钱包数据持久化在 `data/plugins/kurohanekaoruko.mai-bitcoin-sim/`，原子写入，防崩溃损坏
- 做空规则（简化永续合约）：
  - 开空占用保证金 = 名义价值 / 杠杆
  - 爆仓价 = 开仓价 × (1 + 1/杠杆 − 维持保证金率)
  - 标记价触及爆仓线 → 损失全部保证金，插件主动发爆仓警报
- 爆仓等主动通知的送达目标由 `task.alert_targets` 控制（群号/QQ号，多个逐个发送）；
  未配置时发给钱包最近活跃的聊天流。目标需是麦麦已知的会话（在该群/私聊里说过话），
  可用 `/coin alert` 实时检测解析结果
- Coinbas 源为 USD 计价（与 USDT 按 1:1 近似处理）

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
