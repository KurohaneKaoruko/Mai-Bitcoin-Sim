"""历史回放引擎：把模拟盘拨回过去某个时点，麦麦逐根K线自主决策重演行情，再切回实时盘。

用途：
1. 新开功能后想尽快看到结果——空钱包回放一段行情，麦麦自主交易，跑完即有成交与资产曲线
2. 让麦麦重历某个历史时段（如某次暴跌）：她全程自己做决策，检验交易水平

设计：
- 回放 = 钱包重置 → 拨回历史时点 → 逐根K线驱动 麦麦决策 + 风控（爆仓/止盈止损/资金费）→ 回实时盘
- 麦麦每 N 根K线决策一次（自动节流，单次回放最多约 replay_max_decisions 次调用），
  看历史行情趋势、自身持仓，输出结构化指令（buy/sell/long/short/close/hold），
  引擎按当根K线价格执行；决策与理由经回调交由插件写入决策日志
- 价格采用"每根K线依次暴露 open→low/high→close"的方式，保证影线内触发的
  爆仓价/止盈止损价都能被扫到（与实时盘逐 tick 逼近的行为一致）
- 资金费在回放中按周期固定费率近似结算（历史逐时费率拿不到，用 funding_default）
- 成交/快照时间戳写真实历史时间（历史时点，非 now()）；快照曲线逐点写入钱包
- 结束后自动回到实时盘（后台巡检继续用实时行情）
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Tuple

from .market import Candle, INTERVAL_SECONDS
from .wallet import FuturesPosition, Wallet

logger = logging.getLogger(__name__)

# 单次回放的 K 线根数上限（防止把数万根K线灌进一个钱包）
MAX_CANDLES = 5000

# 自主交易支持的动作
FREE_ACTIONS = ("buy", "sell", "long", "short", "close", "hold")

# 麦麦决策提示词（历史回放专用）：约束输出格式为单行动作指令
FREE_DECISION_PROMPT = """你是模拟盘交易员「麦麦」，正在历史回放中自主交易。请根据当前局面决定下一步动作。

【当前局面】
历史时点：{ts_local}
标的：{symbol}（USDT 本位模拟盘）
现价：{price:.6g}
近12根{interval}K线收盘价（旧→新）：{closes}
现金：{cash:,.2f} U
现货持仓：{spot}
合约仓位：{futures}
最近动作：{recent}

【可发动作用作（输出且仅输出一行，格式严格如下）】
hold                      —— 不动
buy <USDT金额>            —— 市价买入现货，如：buy 500
sell <数量|all>           —— 卖出现货，如：sell all / sell 0.05
long <USDT名义> <杠杆>    —— 开杠杆多单，如：long 300 3
short <USDT名义> <杠杆>   —— 开杠杆空单，如：short 200 5
close <long|short>        —— 平掉对应合约单，如：close long

要求：谨慎使用杠杆（≤10x），单次开仓名义价值不超过现金的一半；没有把握就 hold。
现在直接输出动作指令这一行，不要任何解释。"""


@dataclass
class ReplayResult:
    """一次回放的执行摘要。"""

    symbol: str
    interval: str
    candles: int
    start_ts: int
    end_ts: int
    start_price: float
    end_price: float
    decisions: int = 0           # 麦麦决策次数
    acted: int = 0               # 其中实际发动作用作的次数
    trades: int = 0              # 回放产生的成交笔数（决策单+风控单）
    # 事件计数
    liquidations: int = 0
    tp_sl_hits: int = 0
    fundings: int = 0
    errors: int = 0              # 麦麦指令解析/执行失败次数
    # 资产变化
    value_before: float = 0.0
    value_after: float = 0.0
    curve: List[List[float]] = field(default_factory=list)   # [ts, total_value]
    notes: List[str] = field(default_factory=list)

    @property
    def pnl_pct(self) -> float:
        if not self.value_before:
            return 0.0
        return (self.value_after / self.value_before - 1) * 100


def _candle_extremes(c: Candle) -> List[float]:
    """一根K线内价格的暴露顺序：open → 离 open 近的一端 → 离 open 远的一端 → close。

    近似还原盘中路径，保证影线内的触发价被扫到。
    """
    seq = [c.open]
    lo, hi = min(c.low, c.close, c.open), max(c.high, c.close, c.open)
    # 先去离 open 近的极端
    if abs(lo - c.open) <= abs(hi - c.open):
        near, far = lo, hi
    else:
        near, far = hi, lo
    if near != c.open:
        seq.append(near)
    if far != near and far != c.open:
        seq.append(far)
    if c.close not in (c.open, near, far):
        seq.append(c.close)
    elif c.close != seq[-1]:
        seq.append(c.close)
    return seq


def parse_free_action(text: str) -> Optional[Tuple[str, List[str]]]:
    """解析麦麦的动作指令行 → (action, args)；无效返回 None。

    容忍：前后空话、大小写、全角空格、多余标点、动作词前带其它文字。
    逐行扫描，每行先按空白分词、再对每个词尝试动作词匹配（兼容「我决定：sell all」）。
    """
    if not text:
        return None
    actions = "|".join(FREE_ACTIONS)
    pattern = re.compile(rf"(?:^|[\s：:，,。;；！!？?（）()【】\[\]\"'`])({actions})(?:[\s：:，,]+(.*))?$", re.I)
    for raw in text.strip().splitlines():
        line = raw.strip().replace("\u3000", " ").rstrip("。，！？.!?.")
        if not line:
            continue
        m = pattern.search(line)
        if not m:
            continue
        action = m.group(1).lower()
        rest = (m.group(2) or "").strip().rstrip("。，！？.!?.")
        args = [a for a in rest.split() if a] if rest else []
        return action, args
    return None


class ReplayEngine:
    """对单个钱包执行一段历史行情回放。不落地（调用方负责保存钱包）。"""

    def __init__(
        self,
        wallet: Wallet,
        fee_rate: float,
        futures_fee: float,
        funding_default: float,
        mmr: float = 0.005,
        max_decisions: int = 48,
    ):
        self.w = wallet
        self.fee_rate = fee_rate            # 现货手续费率
        self.futures_fee = futures_fee      # 合约手续费率
        self.funding_default = funding_default
        self.mmr = mmr
        self.max_decisions = max_decisions

    # ── 麦麦单步决策执行 ──

    def _fmt_spot(self) -> str:
        if not self.w.positions:
            return "无"
        return "；".join(
            f"{sym} {pos['qty']:.6g}（成本 {pos['avg_cost']:,.6g}）"
            for sym, pos in self.w.positions.items()
        )

    def _fmt_futures(self, price: float) -> str:
        if not self.w.shorts:
            return "无"
        lines = []
        for sp in self.w.shorts.values():
            side = "多" if sp.side == "long" else "空"
            upnl = (price - sp.entry_price) * sp.qty if sp.side == "long" else (sp.entry_price - price) * sp.qty
            lines.append(f"{sp.symbol} {side}单 {sp.qty:.6g} {sp.leverage:g}x（开仓 {sp.entry_price:,.6g}，浮盈 {upnl:+,.2f} U）")
        return "；".join(lines)

    def execute_action(
        self, action: str, args: List[str], symbol: str, price: float
    ) -> Tuple[bool, str]:
        """按麦麦的指令在 price 执行交易。返回 (是否成功, 描述)。"""
        try:
            if action == "hold":
                return True, "hold"
            if action == "buy":
                amount = float(args[0].rstrip("uU"))
                if amount <= 0:
                    return False, "金额必须大于 0"
                t = self.w.buy(symbol, min(amount, self.w.cash), price, self.fee_rate, note="replay:free")
                return True, f"买入 {symbol} {t.amount:,.2f} U @ {price:,.6g}"
            if action == "sell":
                pos = self.w.positions.get(symbol)
                if not pos:
                    return False, f"没有 {symbol} 现货持仓"
                q = args[0].lower() if args else "all"
                qty = None if q == "all" else float(q)
                t = self.w.sell(symbol, qty, price, self.fee_rate, note="replay:free")
                return True, f"卖出 {symbol} {t.qty:.6g} @ {price:,.6g}（盈亏 {t.pnl:+,.2f} U）"
            if action in ("long", "short"):
                nominal = float(args[0].rstrip("uU"))
                lev = float(args[1].rstrip("xX倍")) if len(args) > 1 else 3.0
                if nominal <= 0 or lev < 1:
                    return False, "名义价值/杠杆无效"
                nominal = min(nominal, self.w.cash / 2)   # 名义不超过现金一半，防一单打光
                qty = nominal / price
                if action == "long":
                    self.w.open_long(symbol, qty, lev, price, self.futures_fee, self.mmr, note="replay:free")
                else:
                    self.w.open_short(symbol, qty, lev, price, self.futures_fee, self.mmr, note="replay:free")
                return True, f"开{'多' if action == 'long' else '空'}单 {nominal:,.2f} U {lev:g}x @ {price:,.6g}"
            if action == "close":
                side = (args[0].lower() if args else "").strip()
                if side not in ("long", "short"):
                    # 没指明方向：先空后多
                    side = "short" if f"{symbol}|S" in self.w.shorts else ("long" if f"{symbol}|L" in self.w.shorts else "")
                if not side:
                    return False, "没有可平的合约单"
                self.w.close_futures(symbol, side, None, price, self.futures_fee, note="replay:free")
                return True, f"平{'多' if side == 'long' else '空'}单 @ {price:,.6g}"
            return False, f"未知动作 {action}"
        except (ValueError, IndexError) as e:
            return False, str(e)

    def build_prompt(
        self, symbol: str, c: Candle, closes: List[float], interval: str,
        ts_local: str, recent: str,
    ) -> str:
        return FREE_DECISION_PROMPT.format(
            ts_local=ts_local, symbol=symbol, price=c.open, interval=interval,
            closes="、".join(f"{p:,.6g}" for p in closes),
            cash=self.w.cash, spot=self._fmt_spot(), futures=self._fmt_futures(c.open),
            recent=recent or "（回放刚开始）",
        )

    async def run(
        self,
        candles: List[Candle],
        symbol: str,
        interval: str,
        funding_hours: int = 8,
        snapshot_every: int = 1,
        on_event: Optional[Callable[[str], Awaitable[None]]] = None,
        decide: Optional[Callable[[Candle, List[float], int], Awaitable[Optional[Tuple[str, List[str], str]]]]] = None,
        reset_wallet: bool = True,
    ) -> ReplayResult:
        """逐根回放。

        decide(candle, closes_前12, 决策序号) → (action, args, reasoning) 或 None（跳过决策）。
        插件负责调 LLM 并写决策日志；引擎只负责执行指令与跑风控。

        reset_wallet=True 时回放前把钱包重置到初始资金（回放=从过去重新开始）。
        """
        if not candles:
            raise ValueError("没有可回放的K线")
        if len(candles) > MAX_CANDLES:
            candles = candles[-MAX_CANDLES:]

        if reset_wallet:
            # 原地重置（不换对象）：调用方仍持有同一钱包引用，保存/断言才看得到回放结果
            self.w.cash = self.w.initial_cash
            self.w.positions = {}
            self.w.shorts = {}
            self.w.trades = []
            self.w.realized_pnl = 0.0
            self.w.fees_paid = 0.0
        self.w.snapshots = []   # 回放快照从本时段重新积累

        step = INTERVAL_SECONDS.get(interval, 3600)
        res = ReplayResult(
            symbol=symbol, interval=interval, candles=len(candles),
            start_ts=candles[0].ts, end_ts=candles[-1].ts,
            start_price=candles[0].open, end_price=candles[-1].close,
        )

        # 决策节流：平均每段最多 max_decisions 次
        decide_step = max(1, -(-len(candles) // self.max_decisions))

        # 回放前快照
        p0 = candles[0].open
        res.value_before = self.w.total_value({symbol: p0})
        self.w.snapshots.append([float(candles[0].ts), res.value_before])

        # 资金费结算点（相对回放起点，每 funding_hours 小时一次）
        funding_gap = funding_hours * 3600
        next_funding = candles[0].ts + funding_gap
        sym_set = {symbol}

        last_price = p0
        self._last_action_desc = ""   # 供 build_prompt 引用：每次发动作后立即更新
        for idx, c in enumerate(candles):
            # 麦麦自主决策（按节流间隔）
            if decide and idx % decide_step == 0 and idx < len(candles) - 1:
                closes = [x.close for x in candles[max(0, idx - 12):idx + 1]]
                decision = await decide(c, closes, res.decisions)
                if decision:
                    action, args, reasoning = decision
                    res.decisions += 1
                    ok, desc = self.execute_action(action, args, symbol, c.open)
                    if ok:
                        if action != "hold":
                            res.acted += 1
                            self._last_action_desc = f"{action} {desc}"
                            res.notes.append(f"🤖 [麦麦] {desc}")
                            if on_event:
                                await on_event(f"[回放] {desc}")
                    else:
                        res.errors += 1
                        res.notes.append(f"⚠️ [麦麦指令无效] {action} {' '.join(args)}：{desc}")
                        if on_event:
                            await on_event(f"[回放] 指令无效 {action}: {desc}")
                    # 决策日志由插件在 decide 回调里写（含 reasoning）

            # 资金费（按固定默认费率，历史逐时费率不可得）
            if self.w.shorts and c.ts >= next_funding:
                rates = {s: self.funding_default for s in sym_set}
                events = self.w.settle_funding(rates)
                for ev in events:
                    res.fundings += 1
                    if on_event:
                        await on_event(f"[回放] {ev}")
                next_funding += funding_gap

            # 逐价格点扫描爆仓 / 止盈止损
            for p in _candle_extremes(c):
                last_price = p
                prices = {symbol: p}
                # 止盈止损优先（真实盘也是先检查 TP/SL 再爆仓）
                hits = self.w.check_tp_sl(prices)
                for ev in hits:
                    res.tp_sl_hits += 1
                    res.notes.append(ev)
                    if on_event:
                        await on_event(f"[回放] {ev}")
                # 爆仓
                liqs = self.w.check_liquidation(prices)
                for sp, mark in liqs:
                    res.liquidations += 1
                    side = "空单" if sp.side == "short" else "多单"
                    note = f"💥 [回放] {sp.symbol} {sp.leverage:g}x {side}爆仓 @ {mark:g}（{interval} K线 {c.ts}）"
                    res.notes.append(note)
                    if on_event:
                        await on_event(note)

            # 快照（每 snapshot_every 根记一点，历史时间戳）
            if snapshot_every > 0 and (len(res.curve) == 0 or c.ts - res.curve[-1][0] >= snapshot_every * step):
                v = self.w.total_value({symbol: last_price})
                res.curve.append([float(c.ts), v])
                self.w.snapshots.append([float(c.ts), v])

        res.value_after = self.w.total_value({symbol: last_price})
        if res.curve:
            # 收尾补终点
            if res.curve[-1][0] < res.end_ts:
                res.curve.append([float(res.end_ts), res.value_after])
                self.w.snapshots.append([float(res.end_ts), res.value_after])
        else:
            res.curve = [[float(res.end_ts), res.value_after]]
            self.w.snapshots = [[float(res.end_ts), res.value_after]]
        res.trades = sum(1 for t in self.w.trades if t.note.startswith(("replay", "auto")) or t.action in ("long_open", "short_open", "liquidated"))
        return res
