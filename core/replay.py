"""历史回放引擎：把模拟盘拨回过去某个时点，按历史K线快速重演，再切回实时盘。

用途：
1. 新开功能后想尽快看到爆仓/止盈止损等结果——回放一段行情让钱包先"经历"市场
2. 让麦麦重历某个历史时段（如某次暴跌）观察策略表现

设计：
- 回放 = 用历史K线逐根驱动与实时巡检相同的风控逻辑（爆仓/止盈止损/资金费/快照）
- 价格采用"每根K线依次暴露 open→low/high→close"的方式，保证影线内触发的
  爆仓价/止盈止损价都能被扫到（与实时盘逐 tick 逼近的行为一致）
- 资金费在回放中按周期固定费率近似结算（历史逐时费率拿不到，用 funding_default）
- 回放期间钱包时间轴整体平移：快照/成交时间用真实历史时间戳
- 结束后自动回到实时盘（后台巡检继续用实时行情）
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from .market import Candle, INTERVAL_SECONDS
from .wallet import FuturesPosition, Wallet, now

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
    ) -> ReplayResult:
        """逐根回放。candles 必须按时间升序。on_event：事件回调（通知/日志）。"""
        if not candles:
            raise ValueError("没有可回放的K线")
        if len(candles) > MAX_CANDLES:
            candles = candles[-MAX_CANDLES:]
        step = INTERVAL_SECONDS.get(interval, 3600)
        res = ReplayResult(
            symbol=symbol, interval=interval, candles=len(candles),
            start_ts=candles[0].ts, end_ts=candles[-1].ts,
            start_price=candles[0].open, end_price=candles[-1].close,
        )

        # 回放前快照
        p0 = candles[0].open
        res.value_before = self.w.total_value({symbol: p0})

        # 资金费结算点（相对回放起点，每 funding_hours 小时一次）
        funding_gap = funding_hours * 3600
        next_funding = candles[0].ts + funding_gap
        sym_set = {symbol}

        last_price = p0
        for c in candles:
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

            # 快照（每 snapshot_every 根记一点）
            if snapshot_every > 0 and (len(res.curve) == 0 or c.ts - res.curve[-1][0] >= snapshot_every * step):
                v = self.w.total_value({symbol: last_price})
                res.curve.append([float(c.ts), v])

        res.value_after = self.w.total_value({symbol: last_price})
        if res.curve:
            # 收尾补终点
            if res.curve[-1][0] < res.end_ts:
                res.curve.append([float(res.end_ts), res.value_after])
        else:
            res.curve = [[float(res.end_ts), res.value_after]]
        return res
