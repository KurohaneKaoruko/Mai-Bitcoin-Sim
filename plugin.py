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
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 插件目录进 sys.path，确保无论以包还是单目录方式加载都能 import core
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool  # noqa: E402
from maibot_sdk.types import ToolParameterInfo, ToolParamType  # noqa: E402
from typing import List  # noqa: E402

from core.chart import curve_html, kline_html, kline_text, sparkline  # noqa: E402
from core.market import MarketAggregator, Ticker24h, normalize_symbol  # noqa: E402
from core.wallet import DCAPlan, FuturesPosition, PriceAlert, Trade, Wallet, WalletStore, now  # noqa: E402

PLUGIN_VERSION = "0.3.0"
KLINE_INTERVALS = ("5m", "15m", "1h", "4h", "1d")
FUNDING_INTERVAL = 8 * 3600  # 资金费结算周期：8 小时（UTC 0/8/16 点对齐）


def next_funding_ts(t: int) -> int:
    """下一个资金费结算时间戳（UTC 0/8/16 点对齐）。"""
    slot = t // FUNDING_INTERVAL
    return (slot + 1) * FUNDING_INTERVAL


# ─────────────────────────── 配置 ───────────────────────────

class MainConfig(PluginConfigBase):
    """基础配置"""
    __ui_label__ = "基础"
    enabled: bool = Field(default=True, description="是否启用插件")
    initial_cash: float = Field(default=100000.0, description="初始资金（USDT）")
    wallet_mode: str = Field(default="global", description="钱包模式：global=全局单钱包（默认）；chat=每个聊天流一个钱包")


class TradingConfig(PluginConfigBase):
    """交易参数"""
    __ui_label__ = "交易参数"
    spot_fee: float = Field(default=0.001, description="现货手续费率（0.001=0.1%）")
    futures_fee: float = Field(default=0.0005, description="合约手续费率（0.0005=0.05%）")
    maintenance_margin_rate: float = Field(default=0.005, description="维持保证金率（爆仓线用）")
    default_leverage: float = Field(default=3.0, description="合约默认杠杆倍数")
    max_leverage: float = Field(default=20.0, description="最大允许杠杆")
    funding_settlement: bool = Field(default=True, description="是否结算永续资金费（每 8 小时）")
    funding_default: float = Field(default=0.0001, description="资金费率获取失败时的兜底费率（0.0001=0.01%）")
    allow_ai_short: bool = Field(default=True, description="是否允许麦麦（AI Tool）开空单")
    allow_ai_long: bool = Field(default=True, description="是否允许麦麦（AI Tool）开多单")
    allow_ai_trade: bool = Field(default=True, description="是否允许麦麦（AI Tool）自主下单（买入/卖出）")


class MarketConfig(PluginConfigBase):
    """行情源"""
    __ui_label__ = "行情源"
    price_cache_ttl: float = Field(default=10.0, description="价格缓存秒数")
    remeasure_hours: float = Field(default=6.0, description="行情源定期重测速间隔（小时）")


class ChartConfig(PluginConfigBase):
    """图表"""
    __ui_label__ = "图表"
    kline_interval: str = Field(default="1h", description="默认K线周期（5m/15m/1h/4h/1d）")
    kline_limit: int = Field(default=72, description="默认K线根数（≤300）")


class TaskConfig(PluginConfigBase):
    """后台任务"""
    __ui_label__ = "后台任务"
    tick_seconds: int = Field(default=60, description="后台巡检间隔秒数（爆仓/止盈止损/警报/定投/快照）")
    snapshot_minutes: int = Field(default=30, description="资产快照间隔分钟")
    liquidation_alert: bool = Field(default=True, description="爆仓时是否主动发消息提醒")
    daily_report: bool = Field(default=True, description="每天定时发送收益报告")
    report_hour: int = Field(default=20, description="每日报告发送时间（24小时制，按宿主时区）")
    alert_targets: List[str] = Field(
        default_factory=list,
        description=(
            "爆仓/定投等主动通知的目标列表，写群号或QQ号（如 ['123456', '987654']）。"
            "留空 = 只发给钱包最近活跃的聊天流。多个目标会逐个尝试发送。"
        ),
    )


class CryptoTraderConfig(PluginConfigBase):
    """麦麦炒币模拟器配置"""
    __ui_label__ = "麦麦炒币模拟器"
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
        self._tick_task = None
        self._last_report_day = -1
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
                        w.funding_next = next_funding_ts(now())
                        changed = True

                    # 2. 止盈止损（自动平仓）
                    events = w.check_tp_sl(prices)
                    for ev in events:
                        changed = True
                        self.ctx.logger.info("钱包 %s 触发 %s", w.key, ev)
                        await self._notify(w, ev)

                    # 3. 爆仓检查 + 通知
                    if self.config.task.liquidation_alert:
                        hit = w.check_liquidation(prices)
                        for sp, mark in hit:
                            changed = True
                            side_name = "空单" if sp.side == "short" else "多单"
                            direct = "涨到" if sp.side == "short" else "跌到"
                            self.ctx.logger.warning("钱包 %s 爆仓: %s %.6g张 @%.6g", w.key, sp.symbol, sp.qty, mark)
                            await self._notify(w, (
                                f"⚠️ 爆仓警报 {sp.symbol}\n"
                                f"{sp.leverage:g}x {side_name}在 {fmt_price(mark)} 触发爆仓线 {fmt_price(sp.liquidation_price)}"
                                f"（价格{direct}爆仓价）\n"
                                f"损失全部保证金 {sp.margin:,.2f} U 💀"
                            ))

                    # 4. 价格警报（触发后删除并通知创建者所在流）
                    for aid in list(w.alerts.keys()):
                        a = w.alerts[aid]
                        mark = prices.get(a.symbol)
                        if mark is not None and a.triggered(mark):
                            del w.alerts[aid]
                            changed = True
                            arrow = "📈" if a.op == ">=" else "📉"
                            await self._notify_stream(a.stream_id, w, (
                                f"🔔 价格警报 {a.symbol}\n"
                                f"{arrow} 已{'上穿' if a.op == '>=' else '下破'} {fmt_price(a.price)}（现价 {fmt_price(mark)}）"
                            ))

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
                        except ValueError as e:
                            w.advance_plan(plan, f"现金不足: {e}")
                        except Exception as e:  # noqa: BLE001
                            w.advance_plan(plan, f"异常: {e}")

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
        raise UserError(f"不认识的子命令「{parts[0]}」，/coin help 看用法")

    # ── 子命令实现 ──

    def _cmd_help(self) -> str:
        return "\n".join([
            "🪙 麦麦炒币模拟器（真实行情 · 模拟钱包）",
            "",
            "/coin                     资产总览",
            "/coin price BTC           实时价格 + 24h涨跌",
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
            raise UserError("用法：/coin price <币种>  例：/coin price BTC")
        sym = normalize_symbol(rest[0])
        if not sym:
            raise UserError("币种不能为空")
        t = await self.market.get_ticker24h(sym)
        arrow = "🔴" if t.change_pct >= 0 else "🟢"
        return (
            f"{sym}/USDT  {fmt_price(t.price)} U\n"
            f"{arrow} 24h {fmt_pct(t.change_pct)}　高 {fmt_price(t.high)}　低 {fmt_price(t.low)}\n"
            f"📡 来源 {t.source}"
        )

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
    )
    async def tool_get_price(self, symbol: str, stream_id: str = "", **kwargs):
        try:
            sym = normalize_symbol(symbol)
            t = await self.market.get_ticker24h(sym)
            return True, f"{sym} 现价 {fmt_price(t.price)} USDT，24h {fmt_pct(t.change_pct)}（高 {fmt_price(t.high)} 低 {fmt_price(t.low)}，源 {t.source}）", 1
        except Exception as e:  # noqa: BLE001
            return False, f"查价失败：{e}", 1

    @Tool(
        name="crypto_get_portfolio",
        description="查询麦麦自己的模拟炒币账户：现金、持仓、合约仓位、总资产和收益率。当有人问麦麦炒币炒得怎么样、看看仓位时调用。",
        parameters=[
            ToolParameterInfo(name="stream_id", param_type=ToolParamType.STRING, description="当前聊天流ID", required=False),
        ],
    )
    async def tool_get_portfolio(self, stream_id: str = "", **kwargs):
        try:
            w = await self.store.get(stream_id)
            lines, _ = await self._portfolio_lines(w)
            return True, "\n".join(lines), 1
        except Exception as e:  # noqa: BLE001
            return False, f"查询持仓失败：{e}", 1

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
    )
    async def tool_trade(self, action: str, symbol: str, amount: str, leverage: str = "", stream_id: str = "", **kwargs):
        cfg = self.config.trading
        action = (action or "").strip().lower()
        try:
            args = [symbol, amount] + ([leverage + "x"] if leverage and action in ("short", "long") else [])
            if action in ("buy", "买", "买入"):
                if not cfg.allow_ai_trade:
                    return False, "配置未允许麦麦自主下单", 1
                return True, await self._cmd_buy(stream_id, args), 3
            if action in ("sell", "卖", "卖出"):
                if not cfg.allow_ai_trade:
                    return False, "配置未允许麦麦自主下单", 1
                return True, await self._cmd_sell(stream_id, args), 3
            if action in ("short", "开空", "做空"):
                if not cfg.allow_ai_short:
                    return False, "配置未允许麦麦开空单", 1
                return True, await self._cmd_short(stream_id, args), 3
            if action in ("long", "开多", "做多"):
                if not cfg.allow_ai_long:
                    return False, "配置未允许麦麦开多单", 1
                return True, await self._cmd_long(stream_id, args), 3
            if action in ("close", "平空", "平仓", "平多"):
                return True, await self._cmd_close(stream_id, [symbol] + amount.split()), 3
            return False, f"未知 action：{action}（buy/sell/long/short/close）", 1
        except ValueError as e:
            return False, str(e), 1
        except Exception as e:  # noqa: BLE001
            return False, f"下单失败：{type(e).__name__}: {e}", 1

    @Tool(
        name="crypto_set_alert",
        description="设置价格警报：当某币种上穿/下破目标价时麦麦会收到提醒并转告大家。当有人说「BTC涨到10万提醒我」「到XX价喊我」时调用。",
        parameters=[
            ToolParameterInfo(name="symbol", param_type=ToolParamType.STRING, description="币种符号，如 BTC", required=True),
            ToolParameterInfo(name="op", param_type=ToolParamType.STRING, description="上穿（>=）或下破（<=）", required=True),
            ToolParameterInfo(name="price", param_type=ToolParamType.STRING, description="目标价", required=True),
            ToolParameterInfo(name="stream_id", param_type=ToolParamType.STRING, description="当前聊天流ID", required=False),
        ],
    )
    async def tool_set_alert(self, symbol: str, op: str, price: str, stream_id: str = "", **kwargs):
        try:
            op_map = {">=": ">=", ">": ">=", "上穿": ">=", "突破": ">=", "<=": "<=", "<": "<=", "下破": "<=", "跌破": "<="}
            cmp_op = op_map.get(op.strip())
            if not cmp_op:
                return False, f"op 要写 >= 或 <=（也认：上穿/突破/下破/跌破）", 1
            args = ["add", symbol, cmp_op, price]
            return True, await self._cmd_price_alert(stream_id, args), 2
        except Exception as e:  # noqa: BLE001
            return False, f"设置警报失败：{e}", 1

    @Tool(
        name="crypto_market_overview",
        description="获取加密货币大盘概览：BTC、ETH 等主流币的24小时行情，用于聊大盘走势、市场情绪。",
        parameters=[],
    )
    async def tool_market_overview(self, **kwargs):
        try:
            pairs = await self.market.get_overview()
            parts = []
            for sym, t in pairs:
                arrow = "🔴" if t.change_pct >= 0 else "🟢"
                parts.append(f"{sym} {fmt_price(t.price)}（{arrow}{fmt_pct(t.change_pct)}）")
            return True, "大盘24h：" + "　".join(parts), 1
        except Exception as e:  # noqa: BLE001
            return False, f"获取大盘行情失败：{e}", 1


class UserError(Exception):
    """面向用户的可读错误（直接回复给用户，不算系统异常）。"""


def create_plugin():
    return MaiBitcoinSimPlugin()
