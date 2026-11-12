"""麦麦炒币模拟器（mai-bitcoin-sim）

让麦麦用真实加密货币行情在本地模拟下单：
- 现货买卖（按金额/数量）
- 杠杆做空 + 爆仓
- 定投计划
- K线图 / 资产曲线（render.html2png 渲染，失败降级文字）
- 多交易所行情源（Binance/OKX/Gate/Coinbase）自动测速、失败降级

命令入口 /coin（别名 /币、/炒币），另有 4 个 Tool 供麦麦自主调用。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

# 插件目录进 sys.path，确保无论以包还是单目录方式加载都能 import core
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from maibot_sdk import API, Command, Field, MaiBotPlugin, PluginConfigBase, Tool  # noqa: E402
from maibot_sdk.types import ToolParameterInfo, ToolParamType  # noqa: E402
from typing import List  # noqa: E402

from core.chart import curve_html, kline_html, kline_text, sparkline  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.market import MarketAggregator, Ticker24h, normalize_symbol  # noqa: E402
from core.replay import STRATEGIES, ReplayEngine, ReplayResult  # noqa: E402
from core.wallet import DCAPlan, FuturesPosition, PriceAlert, Trade, Wallet, WalletStore, now  # noqa: E402

PLUGIN_VERSION = "0.4.0"
KLINE_INTERVALS = ("5m", "15m", "1h", "4h", "1d")
FUNDING_INTERVAL = 8 * 3600  # 资金费结算周期：8 小时（UTC 0/8/16 点对齐）


def next_funding_ts(t: int) -> int:
    """下一个资金费结算时间戳（UTC 0/8/16 点对齐）。"""
    slot = t // FUNDING_INTERVAL
    return (slot + 1) * FUNDING_INTERVAL


# ─────────────────────────── 配置 ───────────────────────────

class PluginSection(PluginConfigBase):
    """插件基础设置（宿主保留节）"""
    __ui_label__ = "插件"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="插件总开关", json_schema_extra={"label": "启用插件"})
    config_version: str = Field(default="0.4.0", description="配置版本号；配置结构变更时递增，宿主据此迁移", json_schema_extra={"label": "配置版本"})


class MainConfig(PluginConfigBase):
    """基础配置"""
    __ui_label__ = "基础"
    enabled: bool = Field(default=True, description="是否启用插件", json_schema_extra={"label": "启用插件"})
    initial_cash: float = Field(default=100000.0, description="初始资金（USDT）", json_schema_extra={"label": "初始资金 (USDT)"})
    wallet_mode: Literal["global", "chat"] = Field(
        default="global",
        description="钱包模式：global=全局单钱包（默认）；chat=每个聊天流一个钱包",
        json_schema_extra={"label": "钱包模式"},
    )


class TradingConfig(PluginConfigBase):
    """交易参数"""
    __ui_label__ = "交易参数"
    spot_fee: float = Field(default=0.001, description="现货手续费率（0.001=0.1%）", json_schema_extra={"label": "现货手续费率"})
    futures_fee: float = Field(default=0.0005, description="合约手续费率（0.0005=0.05%）", json_schema_extra={"label": "合约手续费率"})
    maintenance_margin_rate: float = Field(default=0.005, description="维持保证金率（爆仓线用）", json_schema_extra={"label": "维持保证金率"})
    default_leverage: float = Field(default=3.0, description="合约默认杠杆倍数", json_schema_extra={"label": "默认杠杆"})
    max_leverage: float = Field(default=20.0, description="最大允许杠杆", json_schema_extra={"label": "最大杠杆"})
    funding_settlement: bool = Field(default=True, description="是否结算永续资金费（每 8 小时）", json_schema_extra={"label": "资金费结算"})
    funding_default: float = Field(default=0.0001, description="资金费率获取失败时的兜底费率（0.0001=0.01%）", json_schema_extra={"label": "兜底资金费率"})
    allow_ai_short: bool = Field(default=True, description="是否允许麦麦（AI Tool）开空单", json_schema_extra={"label": "允许 AI 开空"})
    allow_ai_long: bool = Field(default=True, description="是否允许麦麦（AI Tool）开多单", json_schema_extra={"label": "允许 AI 开多"})
    allow_ai_trade: bool = Field(default=True, description="是否允许麦麦（AI Tool）自主下单（买入/卖出）", json_schema_extra={"label": "允许 AI 自主交易"})


class MarketConfig(PluginConfigBase):
    """行情源"""
    __ui_label__ = "行情源"
    price_cache_ttl: float = Field(default=10.0, description="价格缓存秒数", json_schema_extra={"label": "价格缓存 (秒)"})
    remeasure_hours: float = Field(default=6.0, description="行情源定期重测速间隔（小时）", json_schema_extra={"label": "重测速间隔 (小时)"})


class ChartConfig(PluginConfigBase):
    """图表"""
    __ui_label__ = "图表"
    kline_interval: Literal["5m", "15m", "1h", "4h", "1d"] = Field(
        default="1h",
        description="默认K线周期",
        json_schema_extra={"label": "默认K线周期"},
    )
    kline_limit: int = Field(default=72, description="默认K线根数（≤300）", json_schema_extra={"label": "默认K线根数"})


class TaskConfig(PluginConfigBase):
    """后台任务"""
    __ui_label__ = "后台任务"
    tick_seconds: int = Field(default=60, description="后台巡检间隔秒数（爆仓/止盈止损/警报/定投/快照）", json_schema_extra={"label": "巡检间隔 (秒)"})
    snapshot_minutes: int = Field(default=30, description="资产快照间隔分钟", json_schema_extra={"label": "快照间隔 (分钟)"})
    liquidation_alert: bool = Field(default=True, description="爆仓时是否主动发消息提醒", json_schema_extra={"label": "爆仓提醒"})
    liquidation_near_pct: float = Field(default=10.0, description="临近爆仓预警阈值（距爆仓价百分比，0=关闭；每仓 30 分钟最多提醒一次）", json_schema_extra={"label": "临近爆仓预警阈值 (%)"})
    daily_report: bool = Field(default=True, description="每天定时发送收益报告", json_schema_extra={"label": "每日报告"})
    report_hour: int = Field(default=20, description="每日报告发送时间（24小时制，按宿主时区）", json_schema_extra={"label": "报告时间 (时)"})
    alert_targets: List[str] = Field(
        default_factory=list,
        description=(
            "爆仓/定投等主动通知的目标列表，写群号或QQ号（如 ['123456', '987654']）。"
            "留空 = 只发给钱包最近活跃的聊天流。多个目标会逐个尝试发送。"
        ),
        json_schema_extra={"label": "主动通知目标", "placeholder": "如：123456, 987654（群号或QQ号）"},
    )
    journal_max_entries: int = Field(default=500, description="决策日志保留条数（LLM 决策/命令/后台事件环形记录）", json_schema_extra={"label": "决策日志上限 (条)"})
    replay_max_candles: int = Field(default=2000, description="历史回放单次最多 K 线根数（防误拉超长时段）", json_schema_extra={"label": "回放K线上限 (根)"})


class CryptoTraderConfig(PluginConfigBase):
    """麦麦炒币模拟器配置"""
    __ui_label__ = "麦麦炒币模拟器"
    plugin: PluginSection = Field(default_factory=PluginSection)
    main: MainConfig = Field(default_factory=MainConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    chart: ChartConfig = Field(default_factory=ChartConfig)
    task: TaskConfig = Field(default_factory=TaskConfig)


# ─────────────────────────── 辅助函数 ───────────────────────────

def fmt_price(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def fmt_qty(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.6f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def fmt_usdt(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:,.2f}"


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def fmt_ts(ts: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def fmt_ago(ts: int) -> str:
    d = max(0, now() - ts)
    if d < 60:
        return f"{d}秒前"
    if d < 3600:
        return f"{d // 60}分钟前"
    if d < 86400:
        return f"{d // 3600}小时前"
    return f"{d // 86400}天前"


def parse_amount(raw: str) -> Tuple[Optional[float], bool]:
    """解析数量/金额参数 → (数值, 是否USDT金额)。支持 100u/100usdt（金额）与 0.5（数量）。"""
    s = raw.strip().lower()
    if not s:
        return None, False
    is_usdt = False
    if s.endswith(("u", "usdt")):
        is_usdt = True
        s = s[: -len("usdt")] if s.endswith("usdt") else s[:-1]
    try:
        v = float(s)
    except ValueError:
        return None, False
    return (v if v > 0 else None), is_usdt


# ─────────────────────────── 插件主体 ───────────────────────────

class MaiBitcoinSimPlugin(MaiBotPlugin):
    config_model = CryptoTraderConfig

    # ── 生命周期 ──

    async def on_load(self) -> None:
        cfg = self.config
        self.ctx.logger.info("mai-bitcoin-sim v%s 加载中（钱包模式=%s）", PLUGIN_VERSION, cfg.main.wallet_mode)

        data_dir = Path(self.ctx.paths.data_dir)
        self.store = WalletStore(data_dir, cfg.main.initial_cash, cfg.main.wallet_mode)
        self.market = MarketAggregator(price_ttl=cfg.market.price_cache_ttl)
        self.journal = Journal(data_dir, max_entries=cfg.task.journal_max_entries)
        self._tick_task = None
        self._last_report_day = -1
        self._liq_warn_ts: Dict[str, int] = {}   # 临近爆仓预警冷却：key -> 上次提醒时间戳
        self._sending = asyncio.Lock()
        self._tick_task = asyncio.create_task(self._background_loop())

        # 启动测速（不阻塞加载太久，失败也能跑：get_price 时会逐源尝试）
        try:
            await asyncio.wait_for(self.market.measure(), timeout=15)
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.warning("初始测速失败（不影响使用，取价时会重试）: %s", e)

        self._tick_task = asyncio.create_task(self._background_loop())
        self.ctx.logger.info("mai-bitcoin-sim 就绪 ✓")

    async def on_unload(self) -> None:
        for t in (getattr(self, "_tick_task", None), getattr(self, "_save_task", None)):
            if t:
                t.cancel()
        try:
            if hasattr(self, "store"):
                await self.store.save_all()
                self.ctx.logger.info("钱包已保存，插件卸载完成")
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.error("卸载保存失败: %s", e)

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        self.ctx.logger.info("配置更新（scope=%s, version=%s）", scope, version)
        if scope == "self" and hasattr(self, "store"):
            # 钱包模式变更时重载 store（已有钱包数据在磁盘，不丢）
            mode = self.config.main.wallet_mode
            if mode != self.store.mode:
                self.ctx.logger.info("钱包模式切换: %s → %s", self.store.mode, mode)
                await self.store.save_all()
                self.store.mode = mode

    # ── WebUI 仪表盘数据 ──

    def _log_tool(self, tool_name: str, kwargs: Dict[str, Any], ok: bool, reply: str) -> None:
        """把一次 Tool 调用记入决策日志（含 LLM 思考 reasoning）。"""
        if not hasattr(self, "journal"):
            return
        reasoning = str(kwargs.get("reasoning") or "").strip()
        args = {k: v for k, v in kwargs.items()
                if k not in ("reasoning", "stream_id") and v not in (None, "")}
        title = f"{tool_name}({' '.join(f'{k}={v}' for k, v in args.items())})"
        asyncio.create_task(self.journal.log(
            "llm", title[:200], reasoning=reasoning,
            detail=reply[:300], stream_id=str(kwargs.get("stream_id") or ""),
            meta={"ok": ok},
        ))

    @API("get_dashboard", description="投资仪表盘：总览/持仓/合约/成交/资产曲线", version="1")
    async def api_get_dashboard(self) -> Dict[str, Any]:
        """WebUI 页面数据源。一次行情取数，返回全部面板快照。"""
        if not hasattr(self, "store"):
            return {"error": "插件尚未完成加载"}
        wallets = await self.store.get_all()
        if not wallets:
            return {"error": "还没有钱包，先在聊天里发 /coin 开户"}

        symbols: set = set()
        for w in wallets:
            symbols.update(w.positions.keys())
            symbols.update(sp.symbol for sp in w.shorts.values())
        prices = await self.market.get_prices(sorted(symbols)) if symbols else {}

        # global 模式只有单钱包；chat 模式下聚合展示主要钱包
        overview_wallet = wallets[0] if self.store.mode == "global" else max(
            wallets, key=lambda w: w.total_value(prices)
        )
        w = overview_wallet
        total = w.total_value(prices)
        pnl = total - w.initial_cash
        pos_val, missing = w.positions_value(prices)
        upnl_spot = sum(
            (prices[sym] - pos["avg_cost"]) * pos["qty"]
            for sym, pos in w.positions.items() if sym in prices
        )
        fut_upnl, _ = w.shorts_unrealized(prices)

        overview = {
            "wallet": w.key if self.store.mode == "chat" else "全局钱包",
            "total": round(total, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / w.initial_cash * 100, 2) if w.initial_cash else 0.0,
            "cash": round(w.cash, 2),
            "spot_value": round(pos_val, 2),
            "margin": round(w.shorts_margin(), 2),
            "spot_upnl": round(upnl_spot, 2),
            "fut_upnl": round(fut_upnl, 2),
            "realized_pnl": round(w.realized_pnl, 2),
            "fees": round(w.fees_paid, 2),
            "funding_total": round(w.funding_total, 2),
            "initial_cash": round(w.initial_cash, 2),
        }

        spot_rows = []
        for sym in sorted(w.positions):
            pos = w.positions[sym]
            p = prices.get(sym)
            row = {
                "symbol": sym,
                "qty": round(pos["qty"], 8),
                "avg_cost": round(pos["avg_cost"], 8),
                "price": round(p, 8) if p is not None else None,
                "tp": round(pos.get("tp_price", 0) or 0, 8) or None,
                "sl": round(pos.get("sl_price", 0) or 0, 8) or None,
            }
            if p is not None:
                val = pos["qty"] * p
                u = (p - pos["avg_cost"]) * pos["qty"]
                row["value"] = round(val, 2)
                row["upnl"] = round(u, 2)
                row["upnl_pct"] = round((p / pos["avg_cost"] - 1) * 100, 2) if pos["avg_cost"] else 0.0
            spot_rows.append(row)

        fut_rows = []
        for key, sp in sorted(w.shorts.items()):
            p = prices.get(sp.symbol)
            row = {
                "symbol": sp.symbol,
                "side": "做多" if sp.side == "long" else "做空",
                "qty": round(sp.qty, 8),
                "leverage": f"{sp.leverage:g}x",
                "entry": round(sp.entry_price, 8),
                "liq": round(sp.liquidation_price, 8),
                "margin": round(sp.margin, 2),
                "tp": round(sp.tp_price, 8) or None,
                "sl": round(sp.sl_price, 8) or None,
            }
            if p is not None:
                u = sp.unrealized(p)
                row["price"] = round(p, 8)
                row["upnl"] = round(u, 2)
                row["upnl_pct"] = round(u / sp.margin * 100, 2) if sp.margin else 0.0
            fut_rows.append(row)

        acts = {
            "buy": "买入", "sell": "卖出", "short_open": "开空", "short_close": "平空",
            "long_open": "开多", "long_close": "平多",
            "liquidated": "爆仓", "dca_buy": "定投买", "dca_skip": "定投跳过",
        }
        trade_rows = []
        for t in reversed(w.trades[-20:]):
            trade_rows.append({
                "time": fmt_ts(t.ts),
                "action": acts.get(t.action, t.action),
                "symbol": t.symbol,
                "qty": round(t.qty, 8),
                "price": round(t.price, 8),
                "amount": round(t.amount, 2),
                "pnl": round(t.pnl, 2) if t.action in ("sell", "short_close", "long_close", "liquidated") else None,
            })

        curve = [
            {"t": fmt_ts(int(ts)), "v": round(v, 2)}
            for ts, v in (w.snapshots or [])[-200:]
        ]
        if not curve:
            w.snapshot(prices, force=True)
            curve = [{"t": fmt_ts(int(w.snapshots[-1][0])), "v": round(w.snapshots[-1][1], 2)}]
            await self.store.save(w)

        return {
            "mode": self.store.mode,
            "updated_at": fmt_ts(now()),
            "missing": missing,
            "overview": overview,
            "spot": spot_rows,
            "futures": fut_rows,
            "trades": trade_rows,
            "curve": curve,
        }

    @API("get_journal", description="决策日志：LLM 思考/命令/后台事件流水", version="1")
    async def api_get_journal(self, limit: int = 100, kind: str = "") -> Dict[str, Any]:
        """决策日志数据源。kind: ''=全部 / llm / command / system。"""
        if not hasattr(self, "journal"):
            return {"entries": []}
        limit = max(1, min(200, int(limit)))
        kind = "" if kind in ("", "all") else (kind if kind in ("llm", "command", "system") else "")
        entries = await self.journal.tail(limit, kind)
        rows = [{
            "time": fmt_ts(int(e.get("ts", 0))),
            "kind": e.get("kind", ""),
            "title": e.get("title", ""),
            "reasoning": e.get("reasoning", ""),
            "detail": e.get("detail", ""),
            "ok": bool(e.get("meta", {}).get("ok", True)),
        } for e in entries]
        counts = {"llm": 0, "command": 0, "system": 0}
        all_entries = await self.journal.load()
        for e in all_entries:
            k = e.get("kind", "")
            if k in counts:
                counts[k] += 1
        return {"total": len(all_entries), "counts": counts, "entries": rows}

    @API("run_replay", description="页面触发历史回放（钱包拨回过去，逐K线重演爆仓/止盈止损/资金费）", version="1")
    async def api_run_replay(
        self, preset: str = "week", until: str = "",
        symbol: str = "BTC", strategy: str = "dca", amount: float = 100.0,
    ) -> Dict[str, Any]:
        """WebUI 触发回放。preset: day/week/month/quarter/year；until: 可选 'YYYY-MM-DD'。

        strategy: dca/hold/none；amount 为 USDT 金额（strategy=none 时忽略）。
        """
        try:
            rest = [symbol] if symbol else []
            rest.append(preset)
            if strategy and strategy != "none":
                rest += [strategy, f"{amount}u"]
            if until:
                u = self._parse_until(until)
                if u is None:
                    return {"success": False, "message": f"日期无效：{until}（格式 YYYY-MM-DD）"}
                rest += ["until", until]
            # 回放作用于全局钱包（WebUI 侧无聊天流上下文）
            text = await self._cmd_replay(WalletStore.GLOBAL_KEY, rest)
            return {"success": True, "message": text}
        except UserError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.exception("WebUI 回放失败")
            return {"success": False, "message": f"{type(e).__name__}: {e}"}

    @API("reset_wallet", description="页面重置全局钱包（清空持仓与记录，恢复初始资金）", version="1")
    async def api_reset_wallet(self) -> Dict[str, Any]:
        """WebUI 触发钱包重置（危险操作，页面有确认弹窗）。"""
        try:
            text = await self._cmd_reset_confirm(WalletStore.GLOBAL_KEY)
            return {"success": True, "message": text}
        except UserError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.exception("WebUI 重置失败")
            return {"success": False, "message": f"{type(e).__name__}: {e}"}

    @API("get_replay_status", description="回放参数预设与钱包可回放状态", version="1")
    async def api_replay_status(self) -> Dict[str, Any]:
        presets = {k: {"interval": v[0], "candles": v[1]} for k, v in self._REPLAY_PRESETS.items()}
        if not hasattr(self, "store"):
            return {"ready": True, "presets": presets}
        w = await self.store.get(WalletStore.GLOBAL_KEY)
        has_activity = bool(w.trades or w.positions or w.shorts)
        return {
            "ready": True,   # 带策略回放空钱包也能跑；ready 只影响提示文案
            "has_activity": has_activity,
            "presets": presets,
            "max_candles": int(self.config.task.replay_max_candles),
            "hint": "" if has_activity else "钱包是空的：选好策略和金额即可回放，跑完即有成交与曲线",
        }

    # ── 后台任务：爆仓检查 + 定投 + 快照 + 定期重测速 ──

    async def _background_loop(self) -> None:
        last_measure = now()
        remeasure_sec = max(300, int(self.config.market.remeasure_hours * 3600))
        while True:
            try:
                await asyncio.sleep(max(1, self.config.task.tick_seconds))
                wallets = await self.store.get_all()
                # 1. 汇总需要查价的币种（持仓/合约/定投/警报）
                symbols: set = set()
                for w in wallets:
                    symbols.update(w.positions.keys())
                    symbols.update(sp.symbol for sp in w.shorts.values())
                    symbols.update(p.symbol for p in w.plans.values())
                    symbols.update(a.symbol for a in w.alerts.values())
                prices = await self.market.get_prices(sorted(symbols)) if symbols else {}

                for w in wallets:
                    changed = False
                    # 1.5 资金费结算（8h 一次；名义按开仓价计，不依赖最新价，放最前）
                    if (
                        self.config.trading.funding_settlement
                        and w.shorts and now() >= w.funding_next
                    ):
                        fund_syms = sorted({sp.symbol for sp in w.shorts.values()})
                        rates: Dict[str, float] = {}
                        for sym in fund_syms:
                            rate = await self.market.get_funding(sym, default=self.config.trading.funding_default)
                            if rate is not None:
                                rates[sym] = rate
                        events = w.settle_funding(rates)
                        for ev in events:
                            self.ctx.logger.info("钱包 %s 资金费: %s", w.key, ev)
                            await self._notify(w, ev)
                            await self.journal.log("system", f"资金费结算（{w.key}）", detail=ev)
                        w.funding_next = next_funding_ts(now())
                        changed = True

                    # 2. 止盈止损（自动平仓）
                    events = w.check_tp_sl(prices)
                    for ev in events:
                        changed = True
                        self.ctx.logger.info("钱包 %s 触发 %s", w.key, ev)
                        await self._notify(w, ev)
                        await self.journal.log("system", f"止盈止损（{w.key}）", detail=ev)

                    # 3. 爆仓检查 + 通知 + 临近预警
                    if self.config.task.liquidation_alert:
                        hit = w.check_liquidation(prices)
                        for sp, mark in hit:
                            changed = True
                            side_name = "空单" if sp.side == "short" else "多单"
                            direct = "涨到" if sp.side == "short" else "跌到"
                            self.ctx.logger.warning("钱包 %s 爆仓: %s %.6g张 @%.6g", w.key, sp.symbol, sp.qty, mark)
                            liq_text = (
                                f"⚠️ 爆仓警报 {sp.symbol}\n"
                                f"{sp.leverage:g}x {side_name}在 {fmt_price(mark)} 触发爆仓线 {fmt_price(sp.liquidation_price)}"
                                f"（价格{direct}爆仓价）\n"
                                f"损失全部保证金 {sp.margin:,.2f} U 💀"
                            )
                            await self._notify(w, liq_text)
                            await self.journal.log("system", f"💥 爆仓 {sp.symbol} {side_name}（{w.key}）", detail=liq_text)
                        # 临近预警：距爆仓 < 阈值且未爆，每仓冷却 30 分钟
                        near_pct = self.config.task.liquidation_near_pct
                        if near_pct > 0:
                            for sp in w.shorts.values():
                                mark = prices.get(sp.symbol)
                                if mark is None:
                                    continue
                                dist = abs(sp.liquidation_price / mark - 1) * 100
                                key = f"{w.key}:{sp.symbol}:{sp.side}"
                                if dist >= near_pct or now() - self._liq_warn_ts.get(key, 0) < 1800:
                                    continue
                                self._liq_warn_ts[key] = now()
                                side_name = "空单" if sp.side == "short" else "多单"
                                await self._notify(w, (
                                    f"⚠️ 临近爆仓 {sp.symbol}\n"
                                    f"{sp.leverage:g}x {side_name}距爆仓价仅 {dist:.1f}%"
                                    f"（爆仓价 {fmt_price(sp.liquidation_price)}，现价 {fmt_price(mark)}）\n"
                                    f"保证金 {sp.margin:,.2f} U，注意风险"
                                ))

                    # 4. 价格警报（触发后删除并通知创建者所在流）
                    for aid in list(w.alerts.keys()):
                        a = w.alerts[aid]
                        mark = prices.get(a.symbol)
                        if mark is not None and a.triggered(mark):
                            del w.alerts[aid]
                            changed = True
                            arrow = "📈" if a.op == ">=" else "📉"
                            alert_text = (
                                f"🔔 价格警报 {a.symbol}\n"
                                f"{arrow} 已{'上穿' if a.op == '>=' else '下破'} {fmt_price(a.price)}（现价 {fmt_price(mark)}）"
                            )
                            await self._notify_stream(a.stream_id, w, alert_text)
                            await self.journal.log("system", f"🔔 价格警报触发 {a.symbol}", detail=alert_text)

                    # 5. 定投到期执行
                    for plan in w.due_plans():
                        changed = True
                        price = prices.get(plan.symbol)
                        if price is None:
                            w.advance_plan(plan, "无行情")
                            continue
                        try:
                            w.buy(plan.symbol, plan.amount_usdt, price, self.config.trading.spot_fee, note=f"dca:{plan.id}")
                            w.advance_plan(plan, f"✓ {fmt_ts(now())} 买 {fmt_usdt(plan.amount_usdt)} U")
                            await self.journal.log("system", f"定投买入 {plan.symbol}（计划 {plan.id}）", detail=f"{fmt_usdt(plan.amount_usdt)} U @ {fmt_price(price)}")
                        except ValueError as e:
                            w.advance_plan(plan, f"现金不足: {e}")
                            await self.journal.log("system", f"定投失败 {plan.symbol}（计划 {plan.id}）", detail=f"现金不足: {e}")
                        except Exception as e:  # noqa: BLE001
                            w.advance_plan(plan, f"异常: {e}")
                            await self.journal.log("system", f"定投异常 {plan.symbol}（计划 {plan.id}）", detail=str(e))

                    # 6. 资产快照
                    snap_gap = max(60, self.config.task.snapshot_minutes * 60)
                    if (not w.snapshots) or (now() - int(w.snapshots[-1][0]) >= snap_gap):
                        if w.snapshot(prices, force=False):
                            changed = True

                await self.store.save_all()

                # 7. 每日报告
                if self.config.task.daily_report:
                    await self._maybe_daily_report(wallets, prices)

                # 8. 定期重测速
                if now() - last_measure >= remeasure_sec:
                    last_measure = now()
                    asyncio.create_task(self._safe_measure())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.ctx.logger.error("后台巡检异常: %s", e)

    async def _notify_stream(self, stream_id: str, w: Wallet, text: str) -> None:
        """发到指定流；流不可用则回退常规通知路径。"""
        if stream_id and await self._try_send(text, stream_id):
            return
        await self._notify(w, text)

    async def _maybe_daily_report(self, wallets: List[Wallet], prices: Dict[str, float]) -> None:
        """每天 report_hour 点发一次收益报告（跨天只发一次）。"""
        import datetime
        today = datetime.datetime.now()
        if today.hour < max(0, min(23, self.config.task.report_hour)):
            return
        day = today.date().toordinal()
        if day == self._last_report_day:
            return
        self._last_report_day = day
        for w in wallets:
            if not w.trades and not w.positions and not w.shorts:
                continue  # 从未交易过的钱包不发
            lines, _ = await self._portfolio_lines(w, day_scope=True)
            await self._notify(w, "\n".join(lines))

    async def _safe_measure(self) -> None:
        try:
            await self.market.measure()
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.warning("行情源重测速失败: %s", e)

    async def _notify(self, w: Wallet, text: str) -> None:
        """发送主动通知（爆仓/定投异常等）。

        优先发给配置的 alert_targets（群号/QQ号，逐个解析为聊天流）；
        未配置或全部解析失败时，回退到钱包最近活跃的聊天流。
        """
        sent_any = False
        targets: List[str] = list(getattr(self.config.task, "alert_targets", []) or [])
        for t in targets:
            sid = await self._resolve_target(str(t).strip())
            if not sid:
                self.ctx.logger.warning("通知目标 %s 解析失败（不是已知群/好友），跳过", t)
                continue
            sent_any = await self._try_send(text, sid) or sent_any
        if sent_any:
            return
        # 回退：钱包最近活跃的聊天流
        sid = w.last_stream_id
        if sid:
            await self._try_send(text, sid)

    async def _resolve_target(self, target: str) -> str:
        """把群号/QQ号解析为聊天流 ID。先按群、再按私聊用户查；失败返回空串。"""
        if not target:
            return ""
        try:
            r = await self.ctx.chat.get_stream_by_group_id(target)
            sid = self._extract_stream_id(r)
            if sid:
                return sid
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.debug("按群解析 %s 失败: %s", target, e)
        try:
            r = await self.ctx.chat.get_stream_by_user_id(target)
            sid = self._extract_stream_id(r)
            if sid:
                return sid
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.debug("按用户解析 %s 失败: %s", target, e)
        return ""

    @staticmethod
    def _extract_stream_id(result: Any) -> str:
        """从 chat 能力返回值中提取 stream_id，兼容 dict / list / str 形态。"""
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, dict):
            for key in ("stream_id", "session_id", "id"):
                v = result.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            data = result.get("data")
            if data:
                return MaiBitcoinSimPlugin._extract_stream_id(data)
        if isinstance(result, list):
            for item in result:
                sid = MaiBitcoinSimPlugin._extract_stream_id(item)
                if sid:
                    return sid
        return ""

    async def _try_send(self, text: str, stream_id: str) -> bool:
        try:
            async with self._sending:
                await self.ctx.send.text(text, stream_id)
            return True
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.warning("通知发送失败（stream=%s）: %s", stream_id, e)
            return False

    # ── 通用回复 ──

    async def _reply(self, stream_id: str, text: str) -> None:
        async with self._sending:
            await self.ctx.send.text(text, stream_id)

    async def _reply_image_html(self, stream_id: str, html: str, fallback_text: str) -> bool:
        """渲染 HTML → PNG 发送；任何失败降级为文字。"""
        try:
            result = await self.ctx.render.html2png(
                html, selector=".page", viewport={"width": 1200, "height": 900}
            )
            if isinstance(result, dict) and result.get("success") is not False:
                b64 = str(result.get("image_base64") or "").strip()
                if b64:
                    async with self._sending:
                        ok = await self.ctx.send.image(b64, stream_id)
                    if ok:
                        return True
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.warning("图片渲染/发送失败，降级文字: %s", e)
        await self._reply(stream_id, fallback_text)
        return False

    # ── 业务视图 ──

    async def _portfolio_lines(self, w: Wallet, day_scope: bool = False) -> Tuple[List[str], Dict[str, float]]:
        """生成资产总览文本行与持仓价格表。day_scope=True 额外算今日盈亏。"""
        cfg = self.config
        symbols = sorted(set(w.positions) | {sp.symbol for sp in w.shorts.values()})
        prices = await self.market.get_prices(symbols) if symbols else {}
        pos_val, missing = w.positions_value(prices)
        short_mar = w.shorts_margin()
        short_upnl, _ = w.shorts_unrealized(prices)
        total = w.total_value(prices)
        pnl = total - w.initial_cash
        pnl_pct = pnl / w.initial_cash * 100 if w.initial_cash else 0.0

        lines = ["📊 麦麦的炒币账户"]
        mode_note = "（global）" if self.store.mode == "global" else "（chat）"
        lines.append(f"💰 总资产 {total:,.2f} U{mode_note}　收益率 {fmt_pct(pnl_pct)}")
        lines.append(f"💵 现金 {w.cash:,.2f} U　现货市值 {pos_val:,.2f} U　保证金 {short_mar:,.2f} U")
        if w.shorts:
            lines.append(f"📉 合约浮盈 {fmt_usdt(short_upnl)} U")
        lines.append(f"📈 已实现盈亏 {fmt_usdt(w.realized_pnl)} U　累计手续费 {w.fees_paid:,.2f} U")
        if w.shorts or w.funding_total:
            lines.append(f"💸 累计资金费 {fmt_usdt(-w.funding_total)} U（正=净收，负=净付）")

        if w.positions:
            lines.append("")
            lines.append("── 现货持仓 ──")
            for sym in sorted(w.positions):
                pos = w.positions[sym]
                p = prices.get(sym)
                if p is None:
                    lines.append(f"· {sym} × {fmt_qty(pos['qty'])}（成本 {fmt_price(pos['avg_cost'])}）〔无行情〕")
                    continue
                val = pos["qty"] * p
                upnl = (p - pos["avg_cost"]) * pos["qty"]
                pct = (p / pos["avg_cost"] - 1) * 100 if pos["avg_cost"] else 0.0
                tpsl = ""
                if pos.get("tp_price", 0) > 0:
                    tpsl += f"　TP {fmt_price(pos['tp_price'])}"
                if pos.get("sl_price", 0) > 0:
                    tpsl += f"　SL {fmt_price(pos['sl_price'])}"
                lines.append(
                    f"· {sym} × {fmt_qty(pos['qty'])} @ {fmt_price(pos['avg_cost'])} → {fmt_price(p)}"
                    f"　值 {val:,.2f} U　{fmt_usdt(upnl)} U（{fmt_pct(pct)}）{tpsl}"
                )
        if w.shorts:
            lines.append("")
            lines.append("── 合约仓位 ──")
            for key, sp in sorted(w.shorts.items()):
                p = prices.get(sp.symbol)
                side_tag = "多" if sp.side == "long" else "空"
                if p is None:
                    lines.append(f"· {sp.symbol} {side_tag} {fmt_qty(sp.qty)} @ {fmt_price(sp.entry_price)} {sp.leverage:g}x〔无行情〕")
                    continue
                upnl = sp.unrealized(p)
                pct = upnl / sp.margin * 100 if sp.margin else 0.0
                dist = abs(sp.liquidation_price / p - 1) * 100 if p else 0.0
                tpsl = ""
                if sp.tp_price > 0:
                    tpsl += f"　TP {fmt_price(sp.tp_price)}"
                if sp.sl_price > 0:
                    tpsl += f"　SL {fmt_price(sp.sl_price)}"
                lines.append(
                    f"· {sp.symbol} {side_tag} {fmt_qty(sp.qty)} @ {fmt_price(sp.entry_price)} {sp.leverage:g}x → {fmt_price(p)}"
                    f"　浮盈 {fmt_usdt(upnl)} U（{fmt_pct(pct)}）　距爆仓 {dist:.1f}%{tpsl}"
                )
        if w.plans:
            lines.append("")
            lines.append("── 定投 ──")
            for p in w.plans.values():
                state = "运行中" if p.enabled else "已暂停"
                lines.append(
                    f"· [{p.id}] {p.symbol} 每 {p.interval_hours:g}h 买 {p.amount_usdt:,.0f} U（{state}，下次 {fmt_ago(p.next_run) if p.next_run > now() else '马上'}）"
                )
        if missing:
            lines.append("")
            lines.append(f"⚠️ 部分币种暂无行情：{', '.join(missing)}")
        return lines, prices

    # ════════════════════════ Command ════════════════════════

    @Command(
        "coin",
        pattern=r"^/(?:coin|币|炒币)(?:\s+(?P<args>.+))?$",
        aliases=["/币", "/炒币", "/coin"],
    )
    async def handle_coin(self, **kwargs):
        stream_id: str = kwargs.get("stream_id", "")
        args: str = (kwargs.get("matched_groups", {}).get("args") or "").strip()
        try:
            text = await self._dispatch(stream_id, args)
            ok = True
        except UserError as e:
            text = str(e)
            ok = False
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.exception("命令处理异常")
            text = f"💀 出了点问题：{type(e).__name__}: {e}"
            ok = False
        if hasattr(self, "journal"):
            sub = args.split()[0] if args else "总览"
            asyncio.create_task(self.journal.log(
                "command", f"/coin {sub}".strip(), detail=text[:300], stream_id=stream_id,
            ))
        return ok, text, 2

    async def _dispatch(self, stream_id: str, args: str) -> str:
        parts = args.split()
        if not parts:
            return await self._cmd_overview(stream_id)
        sub = parts[0].lower()
        rest = parts[1:]
        if sub in ("help", "帮助", "?", "h"):
            return self._cmd_help()
        if sub in ("price", "价", "行情", "p"):
            return await self._cmd_price(stream_id, rest)
        if sub in ("buy", "买", "买入"):
            return await self._cmd_buy(stream_id, rest)
        if sub in ("sell", "卖", "卖出"):
            return await self._cmd_sell(stream_id, rest)
        if sub in ("short", "开空", "做空", "空"):
            return await self._cmd_short(stream_id, rest)
        if sub in ("long", "开多", "做多", "多"):
            return await self._cmd_long(stream_id, rest)
        if sub in ("close", "平空", "平多", "平仓"):
            return await self._cmd_close(stream_id, rest)
        if sub in ("pos", "positions", "持仓", "仓位", "资产", "overview", "总览"):
            return await self._cmd_overview(stream_id, full=True)
        if sub in ("history", "历史", "成交", "记录"):
            return await self._cmd_history(stream_id, rest)
        if sub in ("kline", "k", "蜡烛"):
            return await self._cmd_kline(stream_id, rest)
        if sub in ("curve", "曲线", "资产曲线"):
            return await self._cmd_curve(stream_id)
        if sub in ("dca", "定投"):
            return await self._cmd_dca(stream_id, rest)
        if sub in ("source", "sources", "源", "交易所", "测速"):
            return await self._cmd_source(stream_id)
        if sub in ("notify", "通知", "通知目标"):
            return await self._cmd_notify(stream_id)
        if sub in ("alert", "警报", "提醒", "价格警报"):
            return await self._cmd_price_alert(stream_id, rest)
        if sub in ("tp", "止盈"):
            return await self._cmd_tp_sl(stream_id, rest, "tp")
        if sub in ("sl", "止损"):
            return await self._cmd_tp_sl(stream_id, rest, "sl")
        if sub in ("rank", "排行", "排行榜"):
            return await self._cmd_rank(stream_id)
        if sub in ("reset", "重置"):
            if len(parts) >= 2 and parts[1] in ("确认", "yes", "ok"):
                return await self._cmd_reset_confirm(stream_id)
            raise UserError("重置会清空钱包所有持仓与记录！确认请发：/coin reset 确认")
        if sub in ("market", "大盘"):
            return await self._cmd_market(stream_id)
        if sub in ("funding", "资金费", "费率"):
            return await self._cmd_funding(stream_id, rest)
        if sub in ("replay", "回放", "历史模拟"):
            return await self._cmd_replay(stream_id, rest)
        raise UserError(f"不认识的子命令「{parts[0]}」，/coin help 看用法")

    # ── 子命令实现 ──

    def _cmd_help(self) -> str:
        return "\n".join([
            "🪙 麦麦炒币模拟器（真实行情 · 模拟钱包）",
            "",
            "/coin                     资产总览",
            "/coin price BTC [ETH...]  实时价格 + 24h涨跌（支持中文币名）",
            "/coin funding [BTC]       当前资金费率（缺省=持仓币种）",
            "/coin replay [币种] <时段> [策略 金额u]  历史回放：拨回过去重演行情",
            "/coin replay BTC week dca 100u    例：回放一周，每日定投100U买入BTC",
            "/coin replay BTC month hold 1000u 例：回放一月，期初一次性买入",
            "/coin replay ... until 2025-01-01 从指定日期回放（让麦麦经历那段市场）",
            "/coin buy BTC 100u        买入100U的BTC（或 /coin buy BTC 0.01 按数量）",
            "/coin sell BTC all        卖出（all=清仓，也可写数量）",
            "/coin short BTC 0.01 5x   开空单（默认杠杆见配置，0.01张×5倍）",
            "/coin long BTC 500u 3x    开杠杆多单",
            "/coin close BTC [all|数量] [long|short] 平合约单",
            "/coin pos                 持仓明细",
            "/coin history [条数]       成交记录",
            "/coin kline BTC [1h] [96] K线图",
            "/coin curve               资产曲线",
            "/coin dca add BTC 100u 6h 添加定投（list/del <id>/pause <id>/resume <id>）",
            "/coin alert add BTC >= 100000 价格警报（list/del）",
            "/coin tp BTC 110000       设止盈（sl 止损同理，clear 清除）",
            "/coin rank                钱包排行榜（chat 模式）",
            "/coin market              大盘概览",
            "/coin source              行情源测速",
            "/coin notify              查看/检测主动通知目标",
            "/coin reset               重置钱包（需管理员）",
            "",
            "💡 也可以直接喊麦麦：帮我看下BTC多少钱 / 麦麦全仓梭哈ETH（AI自主调用工具）",
        ])

    async def _cmd_overview(self, stream_id: str, full: bool = False) -> str:
        w = await self.store.get(stream_id)
        lines, prices = await self._portfolio_lines(w)
        await self.store.save(w)
        return "\n".join(lines)

    async def _cmd_price(self, stream_id: str, rest: List[str]) -> str:
        if not rest:
            raise UserError("用法：/coin price <币种>[ <币种>...]  例：/coin price BTC ETH")
        syms = [normalize_symbol(x) for x in rest if normalize_symbol(x)]
        if not syms:
            raise UserError("币种不能为空")
        syms = list(dict.fromkeys(syms))[:6]  # 去重限 6 个
        if len(syms) == 1:
            t = await self.market.get_ticker24h(syms[0])
            arrow = "🔴" if t.change_pct >= 0 else "🟢"
            return (
                f"{syms[0]}/USDT  {fmt_price(t.price)} U\n"
                f"{arrow} 24h {fmt_pct(t.change_pct)}　高 {fmt_price(t.high)}　低 {fmt_price(t.low)}\n"
                f"📡 来源 {t.source}"
            )
        tickers = await self.market.get_overview(syms)
        lines = []
        for sym, t in tickers:
            arrow = "🔴" if t.change_pct >= 0 else "🟢"
            lines.append(f"{sym}  {fmt_price(t.price)} U　{arrow} {fmt_pct(t.change_pct)}")
        return "\n".join(lines)

    async def _cmd_funding(self, stream_id: str, rest: List[str]) -> str:
        """当前资金费率：/coin funding [币种]（默认列出持仓/空单币种）"""
        if rest:
            syms = [normalize_symbol(rest[0])]
        else:
            w = await self.store.get(stream_id)
            syms = sorted({sp.symbol for sp in w.shorts.values()})
            if not syms:
                raise UserError("没有合约仓位。用法：/coin funding <币种>  例：/coin funding BTC")
        lines = []
        for sym in syms:
            rate = await self.market.get_funding(sym)
            if rate is None:
                lines.append(f"{sym}  资金费率暂不可得")
                continue
            direction = "多头付 → 空头收" if rate >= 0 else "空头付 → 多头收"
            lines.append(f"{sym}  资金费率 {rate * 100:+.4f}%（{direction}）")
        return "\n".join(lines)

    # ── 历史回放 ──

    _REPLAY_PRESETS = {
        "day":   ("1h", 24),     # 最近1天
        "week":  ("1h", 168),    # 最近一周
        "month": ("4h", 180),    # 最近一月（4h×180）
        "quarter": ("4h", 540),  # 最近一季
        "year":  ("1d", 365),    # 最近一年
    }

    @staticmethod
    def _parse_until(spec: str) -> Optional[int]:
        """把 '2025-01-01' / '2025-01-01T08:00' / '2025-01-01 08:00' 解析为秒级时间戳。"""
        import datetime
        spec = spec.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return int(datetime.datetime.strptime(spec, fmt).timestamp())
            except ValueError:
                continue
        return None

    async def _cmd_replay(self, stream_id: str, rest: List[str]) -> str:
        """历史回放：/coin replay [币种] <预设> [dca|hold 金额u] [until 日期]

        例：
          /coin replay BTC week dca 100u     回放最近一周，每日定投 100 U 买入 BTC
          /coin replay BTC month hold 1000u  回放最近一月，期初一次性买入 1000 U
          /coin replay week                  不带策略=只重演已有持仓的风控（爆仓/止盈止损）
          /coin replay BTC week dca 100u until 2025-01-01   从指定日期回放一周
        带策略时钱包先重置到初始资金，回放产生真实成交与资产曲线，结束后回到实时盘。
        """
        cfg = self.config
        w = await self.store.get(stream_id)

        until_ts: Optional[int] = None
        args = list(rest)
        if "until" in args:
            i = args.index("until")
            if i + 2 > len(args):
                raise UserError("until 后要跟日期，例：until 2025-01-01")
            until_ts = self._parse_until(args[i + 1])
            if until_ts is None:
                raise UserError(f"日期无效：{args[i + 1]}（格式 2025-01-01 或 2025-01-01 08:00）")
            args = args[:i] + args[i + 2:]
        if not args:
            raise UserError(
                "用法：/coin replay [币种] <day|week|month|quarter|year> [dca|hold 金额u] [until 日期]\n"
                "例：/coin replay BTC week dca 100u（每日定投）　/coin replay BTC month hold 1000u（一次性买入）\n"
                "不带策略 = 只重演已有持仓的风控事件"
            )
        symbol = ""
        preset = args[0].lower()
        if preset not in self._REPLAY_PRESETS:
            # 第一个参数是币种，预设后移
            symbol = normalize_symbol(args[0])
            args = args[1:]
            if not args:
                raise UserError(f"缺少时段预设（{'/'.join(self._REPLAY_PRESETS)}），例：/coin replay {symbol} week dca 100u")
            preset = args[0].lower()
            args = args[1:]
        if preset not in self._REPLAY_PRESETS:
            raise UserError(f"未知预设：{preset}（可选 {'/'.join(self._REPLAY_PRESETS)}）")
        interval, count = self._REPLAY_PRESETS[preset]
        count = min(count, cfg.task.replay_max_candles)

        # 策略解析（可选）
        strategy, amount, is_usdt = "", 0.0, False
        if args:
            strategy = args[0].lower()
            if strategy in STRATEGIES:
                if len(args) < 2:
                    raise UserError(f"策略 {strategy} 需要金额，例：dca 100u")
                amount, is_usdt = parse_amount(args[1])
                if amount is None or not is_usdt or amount <= 0:
                    raise UserError(f"金额无效：{args[1]}（写 100u 这样的 USDT 金额）")
                args = args[2:]
            else:
                raise UserError(f"未知策略：{args[0]}（可选 dca=每日定投 / hold=一次性买入；不带策略=只重演风控）")
        if args:
            raise UserError(f"多余参数：{' '.join(args)}")

        # 回放币种：显式 > 持仓最大敞口 > BTC
        if not symbol:
            if w.positions:
                symbol = max(w.positions, key=lambda s: w.positions[s]["qty"])
            elif w.shorts:
                symbol = next(iter(w.shorts.values())).symbol
            else:
                symbol = "BTC"

        # 带策略时重置钱包，从初始资金开始回放；否则要求钱包有内容可重演
        if strategy:
            reset = True
        else:
            if not w.trades and not w.positions and not w.shorts:
                raise UserError(
                    "钱包还是空的，回放无事可做。带个策略吧，例：/coin replay BTC week dca 100u"
                )
            reset = False

        # 拉历史K线：endTime = until（缺省=现在）
        import time as _time
        end_ms = (until_ts if until_ts else int(_time.time())) * 1000
        try:
            candles = await self.market.get_klines_history(symbol, interval, end_ms, limit=count + 1)
        except Exception as e:  # noqa: BLE001
            raise UserError(f"历史K线拉取失败：{e}") from e
        if len(candles) < 2:
            raise UserError("该时段历史K线不足")
        start_dt = candles[0].ts
        end_dt = candles[-1].ts

        engine = ReplayEngine(
            w, fee_rate=cfg.trading.spot_fee if strategy else cfg.trading.futures_fee,
            funding_default=cfg.trading.funding_default,
            mmr=cfg.trading.maintenance_margin_rate,
        )

        async def on_event(text: str) -> None:
            if hasattr(self, "journal"):
                await self.journal.log("system", "历史回放事件", detail=text[:500])

        res: ReplayResult = await engine.run(
            candles, symbol, interval, on_event=on_event,
            strategy=strategy, strategy_amount=amount, reset_wallet=reset,
        )

        # 回放产生的快照与成交保留（历史时间戳）；保存钱包
        await self.store.save(w)
        strat_desc = (
            f"{strategy.upper()}×{res.strategy_trades} 笔投入 {res.strategy_cost:,.0f} U"
            if strategy else "纯风控重演（无新交易）"
        )
        await self.journal.log(
            "system",
            f"历史回放 {symbol} {preset}（{fmt_ts(start_dt)} → {fmt_ts(end_dt)}）",
            detail=(
                f"K线 {res.candles} 根；策略 {strat_desc}；爆仓 {res.liquidations}、"
                f"止盈止损 {res.tp_sl_hits}、资金费 {res.fundings} 次；"
                f"资产 {res.value_before:,.0f} → {res.value_after:,.0f} U"
            ),
        )

        import datetime
        s_dt = datetime.datetime.fromtimestamp(start_dt).strftime("%m-%d")
        e_dt = datetime.datetime.fromtimestamp(end_dt).strftime("%m-%d")
        chg = (res.end_price / res.start_price - 1) * 100
        lines = [
            f"🎬 历史回放完成 {symbol}（{s_dt} → {e_dt}，{interval}×{res.candles}）",
            f"行情：{fmt_price(res.start_price)} → {fmt_price(res.end_price)}（{fmt_pct(chg)}）",
        ]
        if strategy:
            pos = w.positions.get(symbol)
            hold = f"，现持 {fmt_qty(pos['qty'])} {symbol}" if pos else ""
            ret = (res.value_after / res.strategy_cost - 1) * 100 if res.strategy_cost else 0.0
            lines.append(f"策略：{strategy.upper()} {res.strategy_trades} 笔投入 {res.strategy_cost:,.2f} U → 现值 {res.value_after:,.2f} U（{fmt_pct(ret)}）{hold}")
        lines.append(f"事件：💥爆仓 {res.liquidations}　🎯止盈止损 {res.tp_sl_hits}　💸资金费 {res.fundings}")
        lines.append(f"资产：{res.value_before:,.2f} → {res.value_after:,.2f} U（{fmt_pct(res.pnl_pct)}）")
        lines.append("⏰ 已回到实时盘，用 /coin 看当前持仓")
        for n in res.notes[-5:]:
            lines.append(f"· {n}")
        if len(res.notes) > 5:
            lines.append(f"…（共 {len(res.notes)} 条事件，WebUI 决策日志可查全部）")
        return "\n".join(lines)

    async def _cmd_buy(self, stream_id: str, rest: List[str]) -> str:
        if len(rest) < 2:
            raise UserError("用法：/coin buy <币种> <数量|金额u>  例：/coin buy BTC 500u")
        sym = normalize_symbol(rest[0])
        val, is_usdt = parse_amount(rest[1])
        if val is None:
            raise UserError(f"数量无效：{rest[1]}（可写 0.01（数量）或 500u（金额））")
        price, src = await self.market.get_price(sym)
        w = await self.store.get(stream_id)
        if is_usdt:
            t = w.buy(sym, val, price, self.config.trading.spot_fee)
            qty_desc = fmt_qty(t.qty)
            amt_desc = f"{val:,.2f} U"
        else:
            t = w.buy_qty(sym, val, price, self.config.trading.spot_fee)
            qty_desc = fmt_qty(val)
            amt_desc = f"{t.amount:,.2f} U"
        await self.store.save(w)
        pos = w.positions.get(sym, {"qty": 0, "avg_cost": 0})
        return (
            f"✅ 买入 {sym} {qty_desc} @ {fmt_price(price)}（{amt_desc}，手续费 {t.fee:,.2f} U，{src}）\n"
            f"现持有 {fmt_qty(pos['qty'])} @ 成本 {fmt_price(pos['avg_cost'])}　现金 {w.cash:,.2f} U"
        )

    async def _cmd_sell(self, stream_id: str, rest: List[str]) -> str:
        if len(rest) < 2:
            raise UserError("用法：/coin sell <币种> <数量|all>  例：/coin sell BTC all")
        sym = normalize_symbol(rest[0])
        raw = rest[1].lower()
        qty: Optional[float] = None
        if raw not in ("all", "全部", "清仓"):
            qty, _ = parse_amount(raw)
            if qty is None:
                raise UserError(f"数量无效：{rest[1]}")
        price, src = await self.market.get_price(sym)
        w = await self.store.get(stream_id)
        t = w.sell(sym, qty, price, self.config.trading.spot_fee)
        await self.store.save(w)
        mood = "😎" if t.pnl >= 0 else "🥲"
        return (
            f"{mood} 卖出 {sym} {fmt_qty(t.qty)} @ {fmt_price(price)}（手续费 {t.fee:,.2f} U，{src}）\n"
            f"本次盈亏 {fmt_usdt(t.pnl)} U　现金 {w.cash:,.2f} U"
        )

    async def _open_futures_cmd(self, stream_id: str, rest: List[str], side: str) -> str:
        """开永续合约仓位（long/short 通用）。"""
        side_name = "开多" if side == "long" else "开空"
        if len(rest) < 2:
            raise UserError(f"用法：/coin {'long' if side == 'long' else 'short'} <币种> <数量|名义金额u> [杠杆x]  例：/coin {'long' if side == 'long' else 'short'} BTC 500u 5x")
        sym = normalize_symbol(rest[0])
        val, is_usdt = parse_amount(rest[1])
        if val is None:
            raise UserError(f"数量无效：{rest[1]}（可写 0.01（数量）或 500u（名义金额））")
        lev = self.config.trading.default_leverage
        if len(rest) >= 3:
            m = re.match(r"^(\d+(?:\.\d+)?)x$", rest[2].lower())
            if not m:
                raise UserError(f"杠杆格式：5x（1~{self.config.trading.max_leverage:.0f}）")
            lev = float(m.group(1))
        if lev > self.config.trading.max_leverage:
            raise UserError(f"杠杆超上限 {self.config.trading.max_leverage:.0f}x")
        price, src = await self.market.get_price(sym)
        w = await self.store.get(stream_id)
        qty = val / price if is_usdt else val  # 金额按名义价值折算
        w.open_futures(
            sym, side, qty, lev, price,
            self.config.trading.futures_fee, self.config.trading.maintenance_margin_rate,
        )
        await self.store.save(w)
        sp = w.shorts[Wallet._fut_key(sym, side)]
        dist_pct = abs(sp.liquidation_price / price - 1) * 100
        direct = "涨" if side == "short" else "跌"
        return (
            f"✅ {side_name} {sym} {fmt_qty(qty)} @ {fmt_price(price)} {lev:g}x（{src}）\n"
            f"保证金 {sp.margin:,.2f} U　爆仓价 {fmt_price(sp.liquidation_price)}（{direct} {dist_pct:.1f}% 触发）\n"
            f"⚠️ 价格{direct}到爆仓价即损失全部保证金，注意风险"
        )

    async def _cmd_short(self, stream_id: str, rest: List[str]) -> str:
        return await self._open_futures_cmd(stream_id, rest, "short")

    async def _cmd_long(self, stream_id: str, rest: List[str]) -> str:
        return await self._open_futures_cmd(stream_id, rest, "long")

    async def _cmd_close(self, stream_id: str, rest: List[str]) -> str:
        if not rest:
            raise UserError("用法：/coin close <币种> [数量|all] [long|short]")
        sym = normalize_symbol(rest[0])
        side: Optional[str] = None
        qty: Optional[float] = None
        for tok in rest[1:]:
            low = tok.lower()
            if low in ("long", "多", "多单", "l"):
                side = "long"
            elif low in ("short", "空", "空单", "s"):
                side = "short"
            elif low not in ("all", "全部"):
                q, _ = parse_amount(low)
                if q is None:
                    raise UserError(f"数量无效：{tok}")
                qty = q
        w = await self.store.get(stream_id)
        has_long = w.shorts.get(Wallet._fut_key(sym, "long"))
        has_short = w.shorts.get(Wallet._fut_key(sym, "short"))
        if side is None:
            if has_long and has_short:
                raise UserError(f"{sym} 多空都有，请写 /coin close {sym} <数量|all> long 或 short")
            side = "long" if has_long else "short" if has_short else None
        if side is None:
            raise UserError(f"没有 {sym} 的合约仓位")
        side_name = "平多" if side == "long" else "平空"
        price, src = await self.market.get_price(sym)
        t = w.close_futures(sym, side, qty, price, self.config.trading.futures_fee)
        await self.store.save(w)
        mood = "😎" if t.pnl >= 0 else "🥲"
        return (
            f"{mood} {side_name} {sym} {fmt_qty(t.qty)} @ {fmt_price(price)}（{src}）\n"
            f"本次盈亏 {fmt_usdt(t.pnl)} U　现金 {w.cash:,.2f} U"
        )

    async def _cmd_history(self, stream_id: str, rest: List[str]) -> str:
        n = 10
        if rest:
            try:
                n = max(1, min(50, int(rest[0])))
            except ValueError:
                raise UserError("条数要写数字，如 /coin history 20")
        w = await self.store.get(stream_id)
        if not w.trades:
            return "📜 还没有成交记录，先 /coin buy 试试"
        acts = {
            "buy": "买入", "sell": "卖出", "short_open": "开空", "short_close": "平空",
            "long_open": "开多", "long_close": "平多",
            "liquidated": "爆仓", "dca_buy": "定投买", "dca_skip": "定投跳过",
        }
        lines = [f"📜 最近 {min(n, len(w.trades))} 笔成交"]
        for t in reversed(w.trades[-n:]):
            act = acts.get(t.action, t.action)
            pnl = f"　盈亏 {fmt_usdt(t.pnl)}" if t.action in ("sell", "short_close", "long_close", "liquidated") else ""
            note = f"（{t.note}）" if t.note else ""
            lines.append(f"· {fmt_ts(t.ts)} {act} {t.symbol} {fmt_qty(t.qty)} @ {fmt_price(t.price)}{pnl}{note}")
        return "\n".join(lines)

    async def _cmd_kline(self, stream_id: str, rest: List[str]) -> str:
        sym = normalize_symbol(rest[0]) if rest else "BTC"
        interval = rest[1].lower() if len(rest) >= 2 and rest[1].lower() in KLINE_INTERVALS else self.config.chart.kline_interval
        limit = self.config.chart.kline_limit
        if len(rest) >= 3:
            try:
                limit = max(10, min(300, int(rest[2])))
            except ValueError:
                pass
        candles = await self.market.get_klines(sym, interval, limit)
        html = kline_html(candles, sym, interval)
        sent = await self._reply_image_html(stream_id, html, kline_text(candles, sym, interval))
        return "K线图已发送" if sent else "K线图（文字版）已发送"

    async def _cmd_curve(self, stream_id: str) -> str:
        w = await self.store.get(stream_id)
        if not w.snapshots:
            w.snapshot({}, force=True)
            await self.store.save(w)
            return "📈 资产曲线刚开始记录，先去交易几笔，过阵子再来看～"
        html = curve_html(w.snapshots, "资产曲线（USDT）")
        sent = await self._reply_image_html(stream_id, html, f"📈 快照 {len(w.snapshots)} 个\n{sparkline([s[1] for s in w.snapshots])}")
        return "资产曲线已发送" if sent else "资产曲线（文字版）已发送"

    async def _cmd_dca(self, stream_id: str, rest: List[str]) -> str:
        w = await self.store.get(stream_id)
        if not rest or rest[0].lower() in ("list", "列表"):
            if not w.plans:
                return "📭 没有定投计划\n用法：/coin dca add <币种> <金额u> <间隔h>  例：/coin dca add BTC 100u 6h"
            lines = ["🔁 定投计划"]
            for p in sorted(w.plans.values(), key=lambda x: x.id):
                state = "✅运行中" if p.enabled else "⏸已暂停"
                nxt = fmt_ago(p.next_run) if p.next_run > now() else "即将执行"
                last = f"　上次：{p.last_status}" if p.last_status else ""
                lines.append(f"· [{p.id}] {p.symbol} 每{p.interval_hours:g}h 买 {p.amount_usdt:,.0f} U {state} 下次：{nxt}{last}")
            return "\n".join(lines)
        op = rest[0].lower()
        if op in ("add", "添加"):
            if len(rest) < 4:
                raise UserError("用法：/coin dca add <币种> <金额u> <间隔h>  例：/coin dca add BTC 100u 6h")
            sym = normalize_symbol(rest[1])
            amt, is_u = parse_amount(rest[2])
            hm = re.match(r"^(\d+(?:\.\d+)?)h$", rest[3].lower())
            if not amt or not is_u:
                raise UserError("金额要写成 100u 形式")
            if not hm:
                raise UserError("间隔要写成 6h 形式（小时数+h）")
            hours = float(hm.group(1))
            if hours < 0.1:
                raise UserError("间隔最少 0.1h（6分钟）")
            price, _ = await self.market.get_price(sym)  # 顺带校验币种存在
            plan = DCAPlan(symbol=sym, amount_usdt=amt, interval_hours=hours, next_run=now() + int(hours * 3600))
            w.plans[plan.id] = plan
            await self.store.save(w)
            return f"✅ 定投已添加 [{plan.id}]：每 {hours:g}h 买入 {amt:,.0f} U 的 {sym}（当前价 {fmt_price(price)}）"
        if op in ("del", "rm", "删除"):
            if len(rest) < 2 or rest[1] not in w.plans:
                raise UserError("要删除的计划 id 不存在，/coin dca list 看看")
            p = w.plans.pop(rest[1])
            await self.store.save(w)
            return f"🗑 已删除定投 [{p.id}] {p.symbol}"
        if op in ("pause", "暂停"):
            if len(rest) < 2 or rest[1] not in w.plans:
                raise UserError("计划 id 不存在")
            w.plans[rest[1]].enabled = False
            await self.store.save(w)
            return f"⏸ 定投 [{rest[1]}] 已暂停"
        if op in ("resume", "恢复"):
            if len(rest) < 2 or rest[1] not in w.plans:
                raise UserError("计划 id 不存在")
            w.plans[rest[1]].enabled = True
            await self.store.save(w)
            return f"▶️ 定投 [{rest[1]}] 已恢复"
        raise UserError(f"不认识的定投操作「{rest[0]}」：add/list/del/pause/resume")

    async def _cmd_source(self, stream_id: str) -> str:
        await self._reply(stream_id, "⏱ 正在测速四大交易所……")
        latency = await self.market.measure()
        rows = sorted(latency.items(), key=lambda x: x[1] if x[1] > 0 else 1e9)
        lines = ["📡 行情源测速结果"]
        medal = {0: "🥇", 1: "🥈", 2: "🥉"}
        for i, (name, ms) in enumerate(rows):
            status = f"{ms:.0f}ms" if ms > 0 else "不可达"
            note = "（USD计价）" if name == "Coinbase" else ""
            lines.append(f"{medal.get(i, '　')} {name}: {status}{note}")
        lines.append("")
        lines.append("取价顺序按测速结果自动降级，无需手动切换")
        return "\n".join(lines)

    async def _cmd_notify(self, stream_id: str) -> str:
        """查看通知目标配置 + 实测解析是否成功。"""
        targets: List[str] = list(getattr(self.config.task, "alert_targets", []) or [])
        lines = ["🔔 主动通知（爆仓/定投异常）目标"]
        if not targets:
            lines.append("· 未配置：通知将发到钱包最近活跃的聊天流")
            lines.append("")
            lines.append("在 config.toml 或 WebUI 中设置 [task] alert_targets = [\"群号或QQ号\", ...]")
            return "\n".join(lines)
        for t in targets:
            sid = await self._resolve_target(str(t).strip())
            mark = f"✅ {sid[:12]}…" if sid else "❌ 未找到对应群/好友"
            kind = "群" if sid else "？"
            lines.append(f"· {t}（{kind}）{mark}")
        lines.append("")
        lines.append("✅=解析成功可送达；❌=麦麦不认识这个群/好友（需先在目标群/私聊里说过话）")
        return "\n".join(lines)

    async def _cmd_price_alert(self, stream_id: str, rest: List[str]) -> str:
        """价格警报：/coin alert add BTC >= 100000 | list | del <id>"""
        w = await self.store.get(stream_id)
        if not rest or rest[0].lower() in ("list", "列表"):
            if not w.alerts:
                return "📭 没有价格警报\n用法：/coin alert add <币种> >= 目标价（或 <=）"
            lines = ["🔔 价格警报"]
            for a in sorted(w.alerts.values(), key=lambda x: x.created_at):
                arrow = "📈 上穿" if a.op == ">=" else "📉 下破"
                lines.append(f"· [{a.id}] {a.symbol} {arrow} {fmt_price(a.price)}（{fmt_ago(a.created_at)}设）")
            return "\n".join(lines)
        op = rest[0].lower()
        if op in ("add", "添加"):
            if len(rest) < 4:
                raise UserError("用法：/coin alert add <币种> <>=|<=|上穿|下破> <目标价>")
            sym = normalize_symbol(rest[1])
            op_map = {">=": ">=", ">": ">=", "上穿": ">=", "突破": ">=", "<=": "<=", "<": "<=", "下破": "<=", "跌破": "<="}
            cmp_op = op_map.get(rest[2])
            if not cmp_op:
                raise UserError("条件写 >= 或 <=（也认：上穿/突破/下破/跌破）")
            try:
                target = float(rest[3].replace(",", ""))
            except ValueError:
                raise UserError(f"目标价无效：{rest[3]}")
            if target <= 0:
                raise UserError("目标价要大于 0")
            price, _ = await self.market.get_price(sym)  # 顺带校验币种
            a = PriceAlert(symbol=sym, op=cmp_op, price=target, stream_id=stream_id)
            w.alerts[a.id] = a
            await self.store.save(w)
            arrow = "📈 上穿" if cmp_op == ">=" else "📉 下破"
            return f"✅ 警报已设 [{a.id}]：{sym} {arrow} {fmt_price(target)}（现价 {fmt_price(price)}）"
        if op in ("del", "rm", "删除"):
            if len(rest) < 2 or rest[1] not in w.alerts:
                raise UserError("要删除的警报 id 不存在，/coin alert 看看")
            a = w.alerts.pop(rest[1])
            await self.store.save(w)
            return f"🗑 已删除警报 [{a.id}] {a.symbol}"
        raise UserError(f"不认识的警报操作「{rest[0]}」：add / list / del")

    async def _cmd_tp_sl(self, stream_id: str, rest: List[str], kind: str) -> str:
        """止盈止损：/coin tp BTC 110000 | clear。现货/多单/空单方向自动适配。"""
        kind_name = "止盈" if kind == "tp" else "止损"
        if not rest:
            raise UserError(f"用法：/coin {kind} <币种> <价格|clear> [spot|long|short]")
        sym = normalize_symbol(rest[0])
        w = await self.store.get(stream_id)
        has_pos = sym in w.positions
        has_long = bool(w.shorts.get(Wallet._fut_key(sym, "long")))
        has_short = bool(w.shorts.get(Wallet._fut_key(sym, "short")))
        n_opts = sum([has_pos, has_long, has_short])
        if n_opts == 0:
            raise UserError(f"没有 {sym} 的持仓/合约仓位，先开仓再设{kind_name}")
        side = "auto"
        if rest[-1].lower() in ("spot", "long", "short"):
            side = rest[-1].lower()
            rest = rest[:-1]
        if len(rest) < 2:
            hint_side = " [spot|long|short]" if n_opts > 1 else ""
            raise UserError(f"用法：/coin {kind} {sym} <价格|clear>{hint_side}")
        raw = rest[1].lower()
        hint = ""
        if raw in ("clear", "清除", "取消"):
            price_val = 0.0
        else:
            try:
                price_val = float(raw.replace(",", ""))
            except ValueError:
                raise UserError(f"价格无效：{raw}")
            if price_val <= 0:
                raise UserError("价格要大于 0")
            # 方向合理性提示（按将生效的持仓类型），只提示不阻止
            target_side = side
            if target_side == "auto":
                target_side = "spot" if has_pos else "long" if has_long else "short"
            cur, _ = await self.market.get_price(sym)
            if target_side in ("spot", "long"):
                expect = cur < price_val if kind == "tp" else cur > price_val
            else:
                expect = cur > price_val if kind == "tp" else cur < price_val
            if not expect:
                hint = f"\n💡 注意：按当前价 {fmt_price(cur)}，这个{kind_name}价可能立即触发"
        try:
            applied = w.set_tp_sl(sym, tp=price_val if kind == "tp" else 0.0, sl=price_val if kind == "sl" else 0.0, side=side)
        except ValueError as e:
            raise UserError(str(e))
        if applied == "AMBIVALENT":
            raise UserError(f"{sym} 同时有多种持仓，请明确写 /coin {kind} {sym} <价格> spot / long / short")
        await self.store.save(w)
        side_names = {"spot": "现货", "long": "多单", "short": "空单"}
        side_name = side_names.get(applied, applied)
        if price_val == 0.0:
            return f"🗑 已清除 {sym} {side_name}的{kind_name}"
        return f"✅ {sym} {side_name}{kind_name}已设：{fmt_price(price_val)}{hint}"

    async def _cmd_rank(self, stream_id: str) -> str:
        """排行榜（chat 模式：各钱包总资产排名）。"""
        if self.store.mode != "chat":
            raise UserError("排行榜只在 chat 钱包模式下有意义（当前是 global 全局单钱包）")
        wallets = await self.store.get_all()
        if not wallets:
            raise UserError("还没有任何钱包")
        symbols: set = set()
        for w in wallets:
            symbols.update(w.positions.keys())
            symbols.update(sp.symbol for sp in w.shorts.values())
        prices = await self.market.get_prices(sorted(symbols)) if symbols else {}
        rows = []
        for w in wallets:
            rows.append((w.total_value(prices), w))
        rows.sort(key=lambda x: x[0], reverse=True)
        medal = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = ["🏆 钱包排行榜（总资产）"]
        for i, (v, w) in enumerate(rows[:10]):
            key = w.key[:18] + "…" if len(w.key) > 18 else w.key
            lines.append(f"{medal.get(i, f'{i+1}.')} {key}　{v:,.2f} U（{fmt_pct((v / w.initial_cash - 1) * 100)}）")
        return "\n".join(lines)

    async def _cmd_market(self, stream_id: str) -> str:
        names = ("BTC", "ETH", "SOL", "BNB")
        pairs = await self.market.get_overview(names)
        if not pairs:
            raise UserError("行情源暂时都不可用，稍后再试")
        lines = ["🌐 大盘概览（24h）"]
        for sym, t in pairs:
            arrow = "🔴" if t.change_pct >= 0 else "🟢"
            lines.append(f"{arrow} {sym} {fmt_price(t.price)} U　{fmt_pct(t.change_pct)}")
        lines.append("")
        lines.append(f"📡 源 {pairs[0][1].source} · /coin price <币> 查单币")
        return "\n".join(lines)

    async def _cmd_reset_confirm(self, stream_id: str) -> str:
        w = await self.store.reset(stream_id, self.config.main.initial_cash)
        return f"🔄 钱包已重置：初始资金 {w.initial_cash:,.2f} U，从头再来！"

    # ════════════════════════ Tool（AI 自主调用） ════════════════════════

    @Tool(
        name="crypto_get_price",
        description="查询加密货币的实时价格和24小时涨跌幅（真实行情）。当有人问币价、想了解某个币现在多少钱时调用。",
        parameters=[
            ToolParameterInfo(name="symbol", param_type=ToolParamType.STRING, description="币种符号，如 BTC、ETH、SOL、DOGE", required=True),
            ToolParameterInfo(name="stream_id", param_type=ToolParamType.STRING, description="当前聊天流ID（若上下文提供）", required=False),
        ],
        visibility="visible",
        core_tool=True,
    )
    async def tool_get_price(self, symbol: str, stream_id: str = "", **kwargs):
        try:
            sym = normalize_symbol(symbol)
            t = await self.market.get_ticker24h(sym)
            __r = (True, f"{sym} 现价 {fmt_price(t.price)} USDT，24h {fmt_pct(t.change_pct)}（高 {fmt_price(t.high)} 低 {fmt_price(t.low)}，源 {t.source}）", 1)
        except Exception as e:  # noqa: BLE001
            __r = (False, f"查价失败：{e}", 1)
        self._log_tool("crypto_get_price", kwargs, __r[0], str(__r[1]))
        return __r

    @Tool(
        name="crypto_get_portfolio",
        description="查询麦麦自己的模拟炒币账户：现金、持仓、合约仓位、总资产和收益率。当有人问麦麦炒币炒得怎么样、看看仓位时调用。",
        parameters=[
            ToolParameterInfo(name="stream_id", param_type=ToolParamType.STRING, description="当前聊天流ID", required=False),
        ],
        visibility="visible",
        core_tool=True,
    )
    async def tool_get_portfolio(self, stream_id: str = "", **kwargs):
        try:
            w = await self.store.get(stream_id)
            lines, _ = await self._portfolio_lines(w)
            __r = (True, "\n".join(lines), 1)
        except Exception as e:  # noqa: BLE001
            __r = (False, f"查询持仓失败：{e}", 1)
        self._log_tool("crypto_get_portfolio", kwargs, __r[0], str(__r[1]))
        return __r

    @Tool(
        name="crypto_trade",
        description=(
            "用麦麦的模拟钱包按真实行情下单。action 可选：buy（买入）、sell（卖出）、long（开杠杆多单）、short（开空单）、close（平合约单）。"
            "金额可用 USDT 计价（如 100u）或币量（如 0.5）。合约可带杠杆 leverage（如 5）。"
            "当用户让麦麦买/卖/做多/做空某个币时调用。"
        ),
        parameters=[
            ToolParameterInfo(name="action", param_type=ToolParamType.STRING, description="buy / sell / long / short / close", required=True),
            ToolParameterInfo(name="symbol", param_type=ToolParamType.STRING, description="币种符号，如 BTC", required=True),
            ToolParameterInfo(name="amount", param_type=ToolParamType.STRING, description="数量或金额，如 100u 或 0.01", required=True),
            ToolParameterInfo(name="leverage", param_type=ToolParamType.STRING, description="杠杆倍数（仅 long/short 用），如 5", required=False),
            ToolParameterInfo(name="stream_id", param_type=ToolParamType.STRING, description="当前聊天流ID", required=False),
        ],
        visibility="visible",
        core_tool=True,
    )
    async def tool_trade(self, action: str, symbol: str, amount: str, leverage: str = "", stream_id: str = "", **kwargs):
        cfg = self.config.trading
        action = (action or "").strip().lower()
        try:
            args = [symbol, amount] + ([leverage + "x"] if leverage and action in ("short", "long") else [])
            if action in ("buy", "买", "买入"):
                if not cfg.allow_ai_trade:
                    __r = (False, "配置未允许麦麦自主下单", 1)
                else:
                    __r = (True, await self._cmd_buy(stream_id, args), 3)
            elif action in ("sell", "卖", "卖出"):
                if not cfg.allow_ai_trade:
                    __r = (False, "配置未允许麦麦自主下单", 1)
                else:
                    __r = (True, await self._cmd_sell(stream_id, args), 3)
            elif action in ("short", "开空", "做空"):
                if not cfg.allow_ai_short:
                    __r = (False, "配置未允许麦麦开空单", 1)
                else:
                    __r = (True, await self._cmd_short(stream_id, args), 3)
            elif action in ("long", "开多", "做多"):
                if not cfg.allow_ai_long:
                    __r = (False, "配置未允许麦麦开多单", 1)
                else:
                    __r = (True, await self._cmd_long(stream_id, args), 3)
            elif action in ("close", "平空", "平仓", "平多"):
                __r = (True, await self._cmd_close(stream_id, [symbol] + amount.split()), 3)
            else:
                __r = (False, f"未知 action：{action}（buy/sell/long/short/close）", 1)
        except ValueError as e:
            __r = (False, str(e), 1)
        except Exception as e:  # noqa: BLE001
            __r = (False, f"下单失败：{type(e).__name__}: {e}", 1)
        self._log_tool("crypto_trade", kwargs, __r[0], str(__r[1]))
        return __r

    @Tool(
        name="crypto_set_alert",
        description="设置价格警报：当某币种上穿/下破目标价时麦麦会收到提醒并转告大家。当有人说「BTC涨到10万提醒我」「到XX价喊我」时调用。",
        parameters=[
            ToolParameterInfo(name="symbol", param_type=ToolParamType.STRING, description="币种符号，如 BTC", required=True),
            ToolParameterInfo(name="op", param_type=ToolParamType.STRING, description="上穿（>=）或下破（<=）", required=True),
            ToolParameterInfo(name="price", param_type=ToolParamType.STRING, description="目标价", required=True),
            ToolParameterInfo(name="stream_id", param_type=ToolParamType.STRING, description="当前聊天流ID", required=False),
        ],
        visibility="visible",
        core_tool=True,
    )
    async def tool_set_alert(self, symbol: str, op: str, price: str, stream_id: str = "", **kwargs):
        try:
            op_map = {">=": ">=", ">": ">=", "上穿": ">=", "突破": ">=", "<=": "<=", "<": "<=", "下破": "<=", "跌破": "<="}
            cmp_op = op_map.get(op.strip())
            if not cmp_op:
                __r = (False, "op 要写 >= 或 <=（也认：上穿/突破/下破/跌破）", 1)
            else:
                args = ["add", symbol, cmp_op, price]
                __r = (True, await self._cmd_price_alert(stream_id, args), 2)
        except Exception as e:  # noqa: BLE001
            __r = (False, f"设置警报失败：{e}", 1)
        self._log_tool("crypto_set_alert", kwargs, __r[0], str(__r[1]))
        return __r

    @Tool(
        name="crypto_market_overview",
        description="获取加密货币大盘概览：BTC、ETH 等主流币的24小时行情，用于聊大盘走势、市场情绪。",
        parameters=[],
        visibility="visible",
        core_tool=True,
    )
    async def tool_market_overview(self, **kwargs):
        try:
            pairs = await self.market.get_overview()
            parts = []
            for sym, t in pairs:
                arrow = "🔴" if t.change_pct >= 0 else "🟢"
                parts.append(f"{sym} {fmt_price(t.price)}（{arrow}{fmt_pct(t.change_pct)}）")
            __r = (True, "大盘24h：" + "　".join(parts), 1)
        except Exception as e:  # noqa: BLE001
            __r = (False, f"获取大盘行情失败：{e}", 1)
        self._log_tool("crypto_market_overview", kwargs, __r[0], str(__r[1]))
        return __r


class UserError(Exception):
    """面向用户的可读错误（直接回复给用户，不算系统异常）。"""


def create_plugin():
    return MaiBitcoinSimPlugin()
