"""历史回放引擎：把模拟盘拨回过去某个时点，按历史K线快速重演，再切回实时盘。

用途：
1. 新开功能后想尽快看到结果——空钱包选个策略回放一段行情，跑完即有成交与资产曲线
2. 让麦麦重历某个历史时段（如某次暴跌）观察策略表现

设计：
- 回放 = 用历史K线逐根驱动交易与风控逻辑（策略下单/爆仓/止盈止损/资金费/快照）
- 价格采用"每根K线依次暴露 open→low/high→close"的方式，保证影线内触发的
  爆仓价/止盈止损价都能被扫到（与实时盘逐 tick 逼近的行为一致）
- 资金费在回放中按周期固定费率近似结算（历史逐时费率拿不到，用 funding_default）
- 回放期间钱包时间轴平移：成交/快照时间戳写真实历史时间（历史时点，非 now()）
- 结束后自动回到实时盘（后台巡检继续用实时行情）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Tuple

from .market import Candle, INTERVAL_SECONDS
from .wallet import FuturesPosition, Wallet

logger = logging.getLogger(__name__)

# 单次回放的 K 线根数上限（防止把数万根K线灌进一个钱包）
MAX_CANDLES = 5000


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
    strategy: str = ""           # none / dca / hold
    strategy_amount: float = 0.0
    strategy_trades: int = 0     # 策略成交笔数
    strategy_cost: float = 0.0   # 策略投入总额（含手续费）
    # 事件计数
    liquidations: int = 0
    tp_sl_hits: int = 0
    fundings: int = 0
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


# ── 策略：回放期间主动下单 ──

STRATEGIES = ("dca", "hold")


def _run_strategy(
    wallet: Wallet,
    strategy: str,
    amount: float,
    symbol: str,
    candle: Candle,
    fee_rate: float,
    day_seen: set,
) -> Optional[str]:
    """在单根K线上执行一次策略决策。返回事件描述或 None。

    dca : 每个 UTC 日的第一根K线按 open 定投 amount（USDT）
    hold: 第一根K线一次性买入 amount（USDT）
    """
    if amount <= 0 or wallet.cash < amount:
        return None
    if strategy == "hold":
        if wallet.trades:   # 只在第一根执行（回放中钱包成交即含回放单）
            return None
    else:  # dca
        day = candle.ts // 86400
        if day in day_seen:
            return None
        day_seen.add(day)
    t = wallet.buy(symbol, amount, candle.open, fee_rate, note=f"replay:{strategy}")
    t.ts = candle.ts  # 历史时间戳
    return f"🤖 [回放] {strategy.upper()} 买入 {symbol} {amount:,.0f} U @ {candle.open:,.6g}"


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


class ReplayEngine:
    """对单个钱包执行一段历史行情回放。不落地（调用方负责保存钱包）。"""

    def __init__(
        self,
        wallet: Wallet,
        fee_rate: float,
        funding_default: float,
        mmr: float = 0.005,
    ):
        self.w = wallet
        self.fee_rate = fee_rate
        self.funding_default = funding_default
        self.mmr = mmr

    async def run(
        self,
        candles: List[Candle],
        symbol: str,
        interval: str,
        funding_hours: int = 8,
        snapshot_every: int = 1,
        on_event: Optional[Callable[[str], Awaitable[None]]] = None,
        strategy: str = "",
        strategy_amount: float = 0.0,
        reset_wallet: bool = False,
    ) -> ReplayResult:
        """逐根回放。candles 必须按时间升序。on_event：事件回调（通知/日志）。

        strategy: ""=只重演已有持仓的风控（爆仓/止盈止损/资金费）；
                  "dca"=每 UTC 日第一根K线定投 strategy_amount；
                  "hold"=首根一次性买入 strategy_amount。
        reset_wallet: True 时回放前清空持仓/成交/快照（配合策略从零开始）。
        """
        if not candles:
            raise ValueError("没有可回放的K线")
        if len(candles) > MAX_CANDLES:
            candles = candles[-MAX_CANDLES:]
        if strategy and strategy not in STRATEGIES:
            raise ValueError(f"未知策略: {strategy}（可选 {STRATEGIES}）")
        if strategy and strategy_amount <= 0:
            raise ValueError("策略金额必须大于 0")

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
            strategy=strategy, strategy_amount=strategy_amount,
        )

        # 回放前快照
        p0 = candles[0].open
        res.value_before = self.w.total_value({symbol: p0})
        self.w.snapshots.append([float(candles[0].ts), res.value_before])

        # 资金费结算点（相对回放起点，每 funding_hours 小时一次）
        funding_gap = funding_hours * 3600
        next_funding = candles[0].ts + funding_gap
        sym_set = {symbol}
        day_seen: set = set()

        last_price = p0
        for c in candles:
            # 策略下单（dca/hold）
            if strategy:
                ev = _run_strategy(self.w, strategy, strategy_amount, symbol, c, self.fee_rate, day_seen)
                if ev:
                    res.strategy_trades += 1
                    res.strategy_cost += strategy_amount + strategy_amount * self.fee_rate
                    res.notes.append(ev)
                    if on_event:
                        await on_event(ev)

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
        return res
