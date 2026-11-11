"""钱包与交易引擎：现货、杠杆做空、爆仓、定投、快照与持久化。

所有金额单位 USDT（Coinbase 源按 USD≈USDT 处理）。
数据以 JSON 原子写入 data_dir，进程内 asyncio.Lock 保护并发。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

EPS = 1e-12
# 卖出/平空时数量与持仓差异小于该比例视为"全部"
ALL_RATIO_TOLERANCE = 0.999


def now() -> int:
    return int(time.time())


def fmt_qty_local(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.6f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def fnum(x: float) -> float:
    """清洗浮点：-0.0 → 0.0，极小值归零。"""
    return 0.0 if abs(x) < EPS else x


# ─────────────────────────── 数据模型 ───────────────────────────

@dataclass
class Trade:
    action: str          # buy / sell / short_open / short_close / liquidated / dca_buy / dca_skip
    symbol: str
    qty: float
    price: float
    amount: float        # 成交额（qty*price）
    fee: float
    pnl: float = 0.0     # 平仓/卖出时已实现盈亏
    note: str = ""
    ts: int = field(default_factory=now)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class FuturesPosition:
    """永续合约仓位（支持多/空）。"""

    symbol: str
    side: str                # "long" / "short"
    qty: float               # 仓位大小（正数）
    entry_price: float
    leverage: float
    margin: float            # 占用保证金
    liquidation_price: float
    opened_at: int = field(default_factory=now)
    note: str = ""
    tp_price: float = 0.0    # 止盈价（<=0 = 未设）
    sl_price: float = 0.0    # 止损价（<=0 = 未设）
    funding_paid: float = 0.0  # 累计已付资金费（正=付出，负=收到）

    def unrealized(self, mark: float) -> float:
        """按标记价计算浮盈（正=赚）。多：mark-entry；空：entry-mark。"""
        diff = (mark - self.entry_price) if self.side == "long" else (self.entry_price - mark)
        return fnum(diff * self.qty)


@dataclass
class PriceAlert:
    """到价警报：mark 触发 op 条件后提醒并删除。"""
    symbol: str
    op: str                   # ">=" 或 "<="
    price: float
    stream_id: str = ""       # 创建时的聊天流（通知发回这里）
    note: str = ""
    created_at: int = field(default_factory=now)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])

    def triggered(self, mark: float) -> bool:
        return mark >= self.price if self.op == ">=" else mark <= self.price


@dataclass
class DCAPlan:
    symbol: str
    amount_usdt: float
    interval_hours: float
    next_run: int = field(default_factory=now)
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])
    last_run: int = 0
    last_status: str = ""


class Wallet:
    """一个钱包 = 现金 + 现货持仓 + 合约仓位(多/空) + 成交记录 + 定投计划 + 快照。"""

    MAX_TRADES = 200
    MAX_SNAPSHOTS = 1000

    def __init__(self, key: str, initial_cash: float, created_at: Optional[int] = None):
        self.key = key
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.created_at = created_at or now()
        # positions: {symbol: {"qty": float, "avg_cost": float, "tp_price": float, "sl_price": float}}
        self.positions: Dict[str, Dict[str, float]] = {}
        # 合约仓位（多/空），key: "SYMBOL|L" / "SYMBOL|S"；字段名沿用 shorts 兼容旧数据
        self.shorts: Dict[str, FuturesPosition] = {}
        self.funding_next: int = 0   # 下次资金费结算时间戳（0=未排期）
        self.trades: List[Trade] = []
        self.plans: Dict[str, DCAPlan] = {}
        self.alerts: Dict[str, PriceAlert] = {}
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.funding_total: float = 0.0   # 累计净支付资金费（正=付出，负=收到）
        self.snapshots: List[List[float]] = []   # [ts, total_value]
        self.last_stream_id: str = ""

    # ── 现货 ──

    def buy(self, symbol: str, usdt: float, price: float, fee_rate: float, note: str = "") -> Trade:
        fee = usdt * fee_rate
        cost = usdt + fee
        if cost > self.cash + EPS:
            raise ValueError(f"现金不足：需要 {usdt:,.2f} U（含手续费），可用 {self.cash:,.2f} U")
        qty = usdt / price
        self.cash -= cost
        pos = self.positions.get(symbol) or {"qty": 0.0, "avg_cost": 0.0}
        new_qty = pos["qty"] + qty
        pos["avg_cost"] = fnum((pos["avg_cost"] * pos["qty"] + usdt) / new_qty) if new_qty > 0 else 0.0
        pos["qty"] = new_qty
        self.positions[symbol] = pos
        self.fees_paid += fee
        t = Trade("buy", symbol, qty, price, usdt, fee, note=note)
        self._push(t)
        return t

    def buy_qty(self, symbol: str, qty: float, price: float, fee_rate: float, note: str = "") -> Trade:
        return self.buy(symbol, qty * price, price, fee_rate, note)

    def sell(self, symbol: str, qty_requested: Optional[float], price: float, fee_rate: float, note: str = "") -> Trade:
        pos = self.positions.get(symbol)
        if not pos or pos["qty"] <= EPS:
            raise ValueError(f"没有 {symbol} 现货持仓")
        qty = pos["qty"] if qty_requested is None or qty_requested >= pos["qty"] * ALL_RATIO_TOLERANCE else qty_requested
        qty = min(qty, pos["qty"])
        if qty <= EPS:
            raise ValueError("卖出数量无效")
        gross = qty * price
        fee = gross * fee_rate
        pnl = fnum((price - pos["avg_cost"]) * qty)
        self.cash += gross - fee
        pos["qty"] = fnum(pos["qty"] - qty)
        if pos["qty"] <= EPS:
            del self.positions[symbol]
        self.realized_pnl += pnl
        self.fees_paid += fee
        t = Trade("sell", symbol, qty, price, gross, fee, pnl=pnl, note=note)
        self._push(t)
        return t

    # ── 杠杆做空 ──

    def open_short(
        self, symbol: str, qty: float, leverage: float, price: float,
        fee_rate: float, mmr: float, note: str = "",
    ) -> Trade:
        if symbol in self.shorts:
            raise ValueError(f"{symbol} 已有空单（一次只允许一张，先平仓再加仓）")
    def open_futures(
        self, symbol: str, side: str, qty: float, leverage: float, price: float,
        fee_rate: float, mmr: float, note: str = "",
    ) -> Trade:
        """开永续合约仓位。side: "long"/"short"。"""
        if side not in ("long", "short"):
            raise ValueError("side 必须是 long 或 short")
        key = self._fut_key(symbol, side)
        if key in self.shorts:
            side_name = "多单" if side == "long" else "空单"
            raise ValueError(f"{symbol} 已有{side_name}（一次一张，先平仓再加仓）")
        if leverage < 1 or leverage > 125:
            raise ValueError("杠杆倍数需在 1~125 之间")
        notional = qty * price
        margin = notional / leverage
        fee = notional * fee_rate
        side_name = "开多" if side == "long" else "开空"
        if margin + fee > self.cash + EPS:
            raise ValueError(
                f"现金不足：{side_name}需要保证金 {margin:,.2f} U + 手续费 {fee:,.2f} U，可用 {self.cash:,.2f} U"
            )
        # 爆仓价：空在上方 entry*(1+1/lev-mmr)，多在下方 entry*(1-1/lev+mmr)
        if side == "short":
            liq = price * (1 + 1.0 / leverage - mmr)
        else:
            liq = price * (1 - 1.0 / leverage + mmr)
        self.cash -= margin + fee
        self.shorts[key] = FuturesPosition(symbol, side, qty, price, leverage, margin, liq, note=note)
        self.fees_paid += fee
        action = "long_open" if side == "long" else "short_open"
        t = Trade(action, symbol, qty, price, notional, fee, note=f"lev={leverage:g}x liq@{liq:,.6g}")
        self._push(t)
        return t

    def open_short(self, symbol: str, qty: float, leverage: float, price: float,
                   fee_rate: float, mmr: float, note: str = "") -> Trade:
        return self.open_futures(symbol, "short", qty, leverage, price, fee_rate, mmr, note)

    def open_long(self, symbol: str, qty: float, leverage: float, price: float,
                  fee_rate: float, mmr: float, note: str = "") -> Trade:
        return self.open_futures(symbol, "long", qty, leverage, price, fee_rate, mmr, note)

    @staticmethod
    def _fut_key(symbol: str, side: str) -> str:
        return f"{symbol}|{'L' if side == 'long' else 'S'}"

    def close_futures(self, symbol: str, side: str, qty_requested: Optional[float], price: float,
                      fee_rate: float, note: str = "") -> Trade:
        key = self._fut_key(symbol, side)
        sp = self.shorts.get(key)
        side_name = "多单" if side == "long" else "空单"
        if not sp:
            raise ValueError(f"没有 {symbol} {side_name}")
        qty = sp.qty if qty_requested is None or qty_requested >= sp.qty * ALL_RATIO_TOLERANCE else qty_requested
        qty = min(qty, sp.qty)
        if qty <= EPS:
            raise ValueError("平仓数量无效")
        ratio = qty / sp.qty
        notional = qty * price
        fee = notional * fee_rate
        diff = (price - sp.entry_price) if sp.side == "long" else (sp.entry_price - price)
        pnl = fnum(diff * qty)
        margin_back = sp.margin * ratio
        # 资金费在结算时已实时进出现金，平仓不再重复结算；funding_paid 仅随仓位等比核销（信息性）
        funding_share = sp.funding_paid * ratio
        self.cash += margin_back + pnl - fee
        self.realized_pnl += pnl
        self.fees_paid += fee
        sp.qty = fnum(sp.qty - qty)
        sp.margin = fnum(sp.margin - margin_back)
        sp.funding_paid = fnum(sp.funding_paid - funding_share)
        if sp.qty <= EPS or sp.margin <= EPS:
            del self.shorts[key]
        action = "long_close" if sp.side == "long" else "short_close"
        t = Trade(action, symbol, qty, price, notional, fee, pnl=pnl,
                  note=note or f"lev={sp.leverage:g}x")
        self._push(t)
        return t

    def close_short(self, symbol: str, qty_requested: Optional[float], price: float, fee_rate: float, note: str = "") -> Trade:
        return self.close_futures(symbol, "short", qty_requested, price, fee_rate, note)

    def close_long(self, symbol: str, qty_requested: Optional[float], price: float, fee_rate: float, note: str = "") -> Trade:
        return self.close_futures(symbol, "long", qty_requested, price, fee_rate, note)

    def settle_funding(self, funding_rates: Dict[str, float], max_pct: float = 0.05) -> List[str]:
        """按各币种资金费率结算所有合约仓位（真实规则简化：多头付 rate、空头收 rate）。

        金额 = 名义价值(按开仓价) × rate，直接从现金扣/加。
        返回结算描述列表。
        """
        events: List[str] = []
        for key, sp in list(self.shorts.items()):
            rate = funding_rates.get(sp.symbol)
            if rate is None:
                continue
            rate = max(-max_pct, min(max_pct, rate))  # 防御异常费率
            notional = sp.qty * sp.entry_price
            amount = notional * rate
            if sp.side == "long":
                self.cash -= amount          # 多头：rate>0 付费，rate<0 收取
                sp.funding_paid = fnum(sp.funding_paid + amount)
                who = "支付" if amount >= 0 else "收取"
            else:
                self.cash += amount          # 空头：rate>0 收取，rate<0 付费
                sp.funding_paid = fnum(sp.funding_paid - amount)
                who = "收取" if amount >= 0 else "支付"
            self.funding_total = fnum(self.funding_total + amount if sp.side == "long"
                                      else self.funding_total - amount)
            events.append(
                f"💸 资金费：{sp.symbol} {('多' if sp.side == 'long' else '空')}单 {who} {abs(amount):,.2f} U"
                f"（费率 {rate * 100:+.4f}%）"
            )
        return events

    def check_liquidation(self, prices: Dict[str, float]) -> List[Tuple[FuturesPosition, float]]:
        """检查爆仓：空单 mark >= 爆仓价；多单 mark <= 爆仓价。没收全部保证金。"""
        hit: List[Tuple[FuturesPosition, float]] = []
        for key in list(self.shorts.keys()):
            sp = self.shorts[key]
            mark = prices.get(sp.symbol)
            if mark is None:
                continue
            blown = mark >= sp.liquidation_price if sp.side == "short" else mark <= sp.liquidation_price
            if blown:
                del self.shorts[key]
                self.realized_pnl -= sp.margin
                side_name = "空单" if sp.side == "short" else "多单"
                t = Trade(
                    "liquidated", sp.symbol, sp.qty, mark, sp.qty * mark, 0.0,
                    pnl=-sp.margin,
                    note=f"{sp.leverage:g}x {side_name}爆仓，损失全部保证金 {sp.margin:,.2f} U",
                )
                self._push(t)
                hit.append((sp, mark))
        return hit

    def check_tp_sl(self, prices: Dict[str, float]) -> List[str]:
        """检查止盈止损，直接执行平仓。返回人类可读的成交描述列表。

        现货：tp 在上方（mark >= tp 全卖）、sl 在下方（mark <= sl 全卖）
        空单：tp 在下方（mark <= tp 平空）、sl 在上方（mark >= sl 平空）
        """
        events: List[str] = []
        # 现货
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            mark = prices.get(sym)
            if mark is None:
                continue
            hit_kind = ""
            if pos.get("tp_price", 0) > 0 and mark >= pos["tp_price"]:
                hit_kind = "止盈"
            elif pos.get("sl_price", 0) > 0 and mark <= pos["sl_price"]:
                hit_kind = "止损"
            if not hit_kind:
                continue
            qty, avg = pos["qty"], pos["avg_cost"]
            pnl = fnum((mark - avg) * qty)
            self.sell(sym, None, mark, 0.0, note=f"auto:{hit_kind}")
            events.append(f"🎯 现货{hit_kind}：{sym} {fmt_qty_local(qty)} @ {mark:,.6g}（盈亏 {pnl:+,.2f} U）")
        # 合约仓位（多/空）
        for key in list(self.shorts.keys()):
            sp = self.shorts[key]
            mark = prices.get(sp.symbol)
            if mark is None:
                continue
            side_name = "多单" if sp.side == "long" else "空单"
            hit_kind = ""
            if sp.tp_price > 0:
                # 多单 tp 在上方、空单 tp 在下方
                hit_tp = mark >= sp.tp_price if sp.side == "long" else mark <= sp.tp_price
                if hit_tp:
                    hit_kind = "止盈"
            if not hit_kind and sp.sl_price > 0:
                hit_sl = mark <= sp.sl_price if sp.side == "long" else mark >= sp.sl_price
                if hit_sl:
                    hit_kind = "止损"
            if not hit_kind:
                continue
            qty = sp.qty
            t = self.close_futures(sp.symbol, sp.side, None, mark, 0.0, note=f"auto:{hit_kind}")
            events.append(f"🎯 {side_name}{hit_kind}：{sp.symbol} {fmt_qty_local(qty)} @ {mark:,.6g}（盈亏 {t.pnl:+,.2f} U）")
        return events

    def set_tp_sl(self, symbol: str, tp: float = 0.0, sl: float = 0.0, side: str = "auto") -> str:
        """给现货或合约仓位设置止盈/止损价（0=清除）。side: auto/spot/long/short。返回实际 side。"""
        has_pos = symbol in self.positions
        fut_long = self.shorts.get(self._fut_key(symbol, "long"))
        fut_short = self.shorts.get(self._fut_key(symbol, "short"))
        if side == "auto":
            options = [s for s, p in (("spot", has_pos), ("long", fut_long), ("short", fut_short)) if p]
            if len(options) > 1:
                return "AMBIVALENT"  # 多种持仓并存时需明确 side
            side = options[0] if options else ""
        if side == "spot":
            if not has_pos:
                raise ValueError(f"没有 {symbol} 现货持仓")
            pos = self.positions[symbol]
            pos["tp_price"] = tp
            pos["sl_price"] = sl
            return "spot"
        if side in ("long", "short"):
            sp = self.shorts.get(self._fut_key(symbol, side))
            if not sp:
                side_name = "多单" if side == "long" else "空单"
                raise ValueError(f"没有 {symbol} {side_name}")
            sp.tp_price = tp
            sp.sl_price = sl
            return side
        raise ValueError(f"没有 {symbol} 的持仓/合约仓位")

    # ── 定投 ──

    def due_plans(self, t: Optional[int] = None) -> List[DCAPlan]:
        t = t or now()
        return [p for p in self.plans.values() if p.enabled and t >= p.next_run]

    def advance_plan(self, plan: DCAPlan, status: str) -> None:
        plan.last_run = now()
        plan.last_status = status
        plan.next_run = now() + int(plan.interval_hours * 3600)

    # ── 估值 ──

    def positions_value(self, prices: Dict[str, float]) -> Tuple[float, List[str]]:
        """现货市值合计；缺价的币种跳过并记名。"""
        total, missing = 0.0, []
        for sym, pos in self.positions.items():
            p = prices.get(sym)
            if p is None:
                missing.append(sym)
            else:
                total += pos["qty"] * p
        return fnum(total), missing

    def shorts_margin(self) -> float:
        return fnum(sum(sp.margin for sp in self.shorts.values()))

    def shorts_unrealized(self, prices: Dict[str, float]) -> Tuple[float, List[str]]:
        total, missing = 0.0, []
        for sp in self.shorts.values():
            p = prices.get(sp.symbol)
            if p is None:
                missing.append(sp.symbol)
            else:
                total += sp.unrealized(p)
        return fnum(total), missing

    def total_value(self, prices: Dict[str, float]) -> float:
        pv, _ = self.positions_value(prices)
        su, _ = self.shorts_unrealized(prices)
        return fnum(self.cash + pv + self.shorts_margin() + su)

    def snapshot(self, prices: Dict[str, float], force: bool = False) -> bool:
        """追加资产快照（同窗口去重）。"""
        t = now()
        if not force and self.snapshots and t - int(self.snapshots[-1][0]) < 60:
            return False
        self.snapshots.append([float(t), self.total_value(prices)])
        if len(self.snapshots) > self.MAX_SNAPSHOTS:
            self.snapshots = self.snapshots[-self.MAX_SNAPSHOTS:]
        return True

    def _push(self, t: Trade) -> None:
        self.trades.append(t)
        if len(self.trades) > self.MAX_TRADES:
            self.trades = self.trades[-self.MAX_TRADES:]

    # ── 序列化 ──

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "created_at": self.created_at,
            "positions": self.positions,
            "shorts": {k: vars(sp) for k, sp in self.shorts.items()},
            "funding_next": self.funding_next,
            "trades": [t.to_dict() for t in self.trades],
            "plans": {k: vars(p) for k, p in self.plans.items()},
            "alerts": {k: vars(a) for k, a in self.alerts.items()},
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "funding_total": self.funding_total,
            "snapshots": self.snapshots,
            "last_stream_id": self.last_stream_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Wallet":
        w = cls(d["key"], float(d["initial_cash"]), d.get("created_at"))
        w.cash = float(d.get("cash", d["initial_cash"]))
        w.positions = {
            k: {
                "qty": float(v["qty"]),
                "avg_cost": float(v["avg_cost"]),
                "tp_price": float(v.get("tp_price", 0) or 0),
                "sl_price": float(v.get("sl_price", 0) or 0),
            } for k, v in d.get("positions", {}).items()
        }
        def _load_fut(k: str, v: Dict[str, Any]) -> Tuple[str, FuturesPosition]:
            side = v.get("side")
            if side not in ("long", "short"):  # 旧格式：key 是裸 symbol，一律视为空单
                side = "short"
            key = k if "|" in k else cls._fut_key(v["symbol"], side)
            return key, FuturesPosition(
                symbol=v["symbol"], side=side, qty=float(v["qty"]), entry_price=float(v["entry_price"]),
                leverage=float(v["leverage"]), margin=float(v["margin"]),
                liquidation_price=float(v["liquidation_price"]), opened_at=int(v.get("opened_at", 0)),
                note=v.get("note", ""),
                tp_price=float(v.get("tp_price", 0) or 0),
                sl_price=float(v.get("sl_price", 0) or 0),
                funding_paid=float(v.get("funding_paid", 0) or 0),
            )
        w.shorts = dict(_load_fut(k, v) for k, v in d.get("shorts", {}).items())
        w.funding_next = int(d.get("funding_next", 0) or 0)
        w.trades = [
            Trade(
                action=t["action"], symbol=t["symbol"], qty=float(t["qty"]), price=float(t["price"]),
                amount=float(t["amount"]), fee=float(t["fee"]), pnl=float(t.get("pnl", 0.0)),
                note=t.get("note", ""), ts=int(t.get("ts", 0)), id=t.get("id", uuid.uuid4().hex[:8]),
            ) for t in d.get("trades", [])
        ]
        w.plans = {
            k: DCAPlan(
                symbol=p["symbol"], amount_usdt=float(p["amount_usdt"]),
                interval_hours=float(p["interval_hours"]), next_run=int(p["next_run"]),
                enabled=bool(p.get("enabled", True)), id=k, last_run=int(p.get("last_run", 0)),
                last_status=p.get("last_status", ""),
            ) for k, p in d.get("plans", {}).items()
        }
        w.alerts = {
            k: PriceAlert(
                symbol=a["symbol"], op=a.get("op", ">="), price=float(a["price"]),
                stream_id=a.get("stream_id", ""), note=a.get("note", ""),
                created_at=int(a.get("created_at", 0)), id=k,
            ) for k, a in d.get("alerts", {}).items()
        }
        w.realized_pnl = float(d.get("realized_pnl", 0.0))
        w.fees_paid = float(d.get("fees_paid", 0.0))
        w.funding_total = float(d.get("funding_total", 0.0))
        w.snapshots = [[float(a), float(b)] for a, b in d.get("snapshots", [])]
        w.last_stream_id = d.get("last_stream_id", "")
        return w


class WalletStore:
    """钱包集合：global 单钱包 / chat 每聊天流一钱包，JSON 原子持久化。"""

    GLOBAL_KEY = "__global__"

    def __init__(self, data_dir: Path, initial_cash: float, mode: str = "global"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.initial_cash = initial_cash
        self.mode = mode
        self._wallets: Dict[str, Wallet] = {}
        self._lock = asyncio.Lock()

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace("\\", "_").replace("..", "__")
        return self.data_dir / f"wallet_{safe}.json"

    def wallet_key(self, stream_id: str) -> str:
        if self.mode == "chat":
            return stream_id or self.GLOBAL_KEY
        return self.GLOBAL_KEY

    async def get(self, stream_id: str, touch: bool = True) -> Wallet:
        key = self.wallet_key(stream_id)
        async with self._lock:
            w = self._wallets.get(key)
            if w is None:
                path = self._path(key)
                if path.exists():
                    try:
                        data = await asyncio.to_thread(path.read_text, "utf-8")
                        w = Wallet.from_dict(json.loads(data))
                        logger.info("已加载钱包 %s（现金 %.2f U）", key, w.cash)
                    except Exception as e:  # noqa: BLE001
                        logger.error("钱包 %s 损坏，重建: %s", key, e)
                        w = None
                if w is None:
                    w = Wallet(key, self.initial_cash)
                    logger.info("新建钱包 %s，初始资金 %.2f U", key, w.initial_cash)
                self._wallets[key] = w
            if touch and stream_id:
                w.last_stream_id = stream_id
            return w

    async def get_all(self) -> List[Wallet]:
        """已加载的钱包 + 磁盘上未加载的。"""
        async with self._lock:
            for path in list(self.data_dir.glob("wallet_*.json")):
                key = path.stem[len("wallet_"):]
                if key not in self._wallets:
                    try:
                        data = await asyncio.to_thread(path.read_text, "utf-8")
                        self._wallets[key] = Wallet.from_dict(json.loads(data))
                    except Exception as e:  # noqa: BLE001
                        logger.error("钱包 %s 加载失败: %s", key, e)
            return list(self._wallets.values())

    async def save(self, w: Wallet) -> None:
        async with self._lock:
            await self._save_unlocked(w)

    async def _save_unlocked(self, w: Wallet) -> None:
        path = self._path(w.key)
        payload = json.dumps(w.to_dict(), ensure_ascii=False, indent=1)

        def _atomic_write() -> None:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(payload, "utf-8")
            os.replace(tmp, path)

        await asyncio.to_thread(_atomic_write)

    async def save_all(self) -> None:
        async with self._lock:
            for w in self._wallets.values():
                try:
                    await self._save_unlocked(w)
                except Exception as e:  # noqa: BLE001
                    logger.error("钱包 %s 保存失败: %s", w.key, e)

    async def reset(self, stream_id: str, initial_cash: Optional[float] = None) -> Wallet:
        key = self.wallet_key(stream_id)
        async with self._lock:
            w = Wallet(key, initial_cash if initial_cash is not None else self.initial_cash)
            self._wallets[key] = w
            await self._save_unlocked(w)
            return w

    def forget(self, key: str) -> None:
        self._wallets.pop(key, None)
