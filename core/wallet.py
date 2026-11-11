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
class ShortPosition:
    symbol: str
    qty: float               # 做空数量（正数表示空头头寸大小）
    entry_price: float
    leverage: float
    margin: float            # 占用保证金
    liquidation_price: float
    opened_at: int = field(default_factory=now)
    note: str = ""
    tp_price: float = 0.0    # 止盈价（<=0 = 未设）；标记价 <= tp 触发平空
    sl_price: float = 0.0    # 止损价（<=0 = 未设）；标记价 >= sl 触发平空

    def unrealized(self, mark: float) -> float:
        """按标记价计算浮盈（正=赚）。"""
        return fnum((self.entry_price - mark) * self.qty)


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
    """一个钱包 = 现金 + 现货持仓 + 空单 + 成交记录 + 定投计划 + 快照。"""

    MAX_TRADES = 200
    MAX_SNAPSHOTS = 1000

    def __init__(self, key: str, initial_cash: float, created_at: Optional[int] = None):
        self.key = key
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.created_at = created_at or now()
        # positions: {symbol: {"qty": float, "avg_cost": float, "tp_price": float, "sl_price": float}}
        self.positions: Dict[str, Dict[str, float]] = {}
        self.shorts: Dict[str, ShortPosition] = {}
        self.trades: List[Trade] = []
        self.plans: Dict[str, DCAPlan] = {}
        self.alerts: Dict[str, PriceAlert] = {}
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
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
        if leverage < 1 or leverage > 125:
            raise ValueError("杠杆倍数需在 1~125 之间")
        notional = qty * price
        margin = notional / leverage
        fee = notional * fee_rate
        if margin + fee > self.cash + EPS:
            raise ValueError(
                f"现金不足：开空需要保证金 {margin:,.2f} U + 手续费 {fee:,.2f} U，可用 {self.cash:,.2f} U"
            )
        liq = price * (1 + 1.0 / leverage - mmr)
        self.cash -= margin + fee
        self.shorts[symbol] = ShortPosition(symbol, qty, price, leverage, margin, liq, note=note)
        self.fees_paid += fee
        t = Trade("short_open", symbol, qty, price, notional, fee, note=f"lev={leverage:.0f}x liq@{liq:,.6g}")
        self._push(t)
        return t

    def close_short(self, symbol: str, qty_requested: Optional[float], price: float, fee_rate: float, note: str = "") -> Trade:
        sp = self.shorts.get(symbol)
        if not sp:
            raise ValueError(f"没有 {symbol} 空单")
        qty = sp.qty if qty_requested is None or qty_requested >= sp.qty * ALL_RATIO_TOLERANCE else qty_requested
        qty = min(qty, sp.qty)
        if qty <= EPS:
            raise ValueError("平仓数量无效")
        ratio = qty / sp.qty
        notional = qty * price
        fee = notional * fee_rate
        pnl = fnum((sp.entry_price - price) * qty)
        margin_back = sp.margin * ratio
        self.cash += margin_back + pnl - fee
        self.realized_pnl += pnl
        self.fees_paid += fee
        sp.qty = fnum(sp.qty - qty)
        sp.margin = fnum(sp.margin - margin_back)
        if sp.qty <= EPS or sp.margin <= EPS:
            del self.shorts[symbol]
        t = Trade("short_close", symbol, qty, price, notional, fee, pnl=pnl, note=note or f"lev={sp.leverage:.0f}x")
        self._push(t)
        return t

    def check_liquidation(self, prices: Dict[str, float]) -> List[Tuple[ShortPosition, float]]:
        """检查爆仓：标记价 >= 爆仓价 → 没收全部保证金。返回被爆空单与爆仓价。"""
        hit: List[Tuple[ShortPosition, float]] = []
        for sym in list(self.shorts.keys()):
            sp = self.shorts[sym]
            mark = prices.get(sym)
            if mark is not None and mark >= sp.liquidation_price:
                del self.shorts[sym]
                self.realized_pnl -= sp.margin
                t = Trade(
                    "liquidated", sym, sp.qty, mark, sp.qty * mark, 0.0,
                    pnl=-sp.margin, note=f"{sp.leverage:.0f}x 空单爆仓，损失全部保证金 {sp.margin:,.2f} U",
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
        # 空单
        for sym in list(self.shorts.keys()):
            sp = self.shorts[sym]
            mark = prices.get(sym)
            if mark is None:
                continue
            hit_kind = ""
            if sp.tp_price > 0 and mark <= sp.tp_price:
                hit_kind = "止盈"
            elif sp.sl_price > 0 and mark >= sp.sl_price:
                hit_kind = "止损"
            if not hit_kind:
                continue
            t = self.close_short(sym, None, mark, 0.0, note=f"auto:{hit_kind}")
            events.append(f"🎯 空单{hit_kind}：{sym} {fmt_qty_local(sp.qty)} @ {mark:,.6g}（盈亏 {t.pnl:+,.2f} U）")
        return events

    def set_tp_sl(self, symbol: str, tp: float = 0.0, sl: float = 0.0, side: str = "auto") -> str:
        """给现货或空单设置止盈/止损价（0=清除）。side: auto/spot/short。返回操作描述。"""
        has_pos = symbol in self.positions
        has_short = symbol in self.shorts
        if side == "auto":
            if has_pos and has_short:
                return "AMBIVALENT"  # 同时持有时需明确 side
            side = "spot" if has_pos else "short" if has_short else ""
        if side == "spot":
            if not has_pos:
                raise ValueError(f"没有 {symbol} 现货持仓")
            pos = self.positions[symbol]
            pos["tp_price"] = tp
            pos["sl_price"] = sl
            return "spot"
        if side == "short":
            if not has_short:
                raise ValueError(f"没有 {symbol} 空单")
            self.shorts[symbol].tp_price = tp
            self.shorts[symbol].sl_price = sl
            return "short"
        raise ValueError(f"没有 {symbol} 的持仓/空单")

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
        for sym, sp in self.shorts.items():
            p = prices.get(sym)
            if p is None:
                missing.append(sym)
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
            "trades": [t.to_dict() for t in self.trades],
            "plans": {k: vars(p) for k, p in self.plans.items()},
            "alerts": {k: vars(a) for k, a in self.alerts.items()},
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
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
        w.shorts = {
            k: ShortPosition(
                symbol=v["symbol"], qty=float(v["qty"]), entry_price=float(v["entry_price"]),
                leverage=float(v["leverage"]), margin=float(v["margin"]),
                liquidation_price=float(v["liquidation_price"]), opened_at=int(v.get("opened_at", 0)),
                note=v.get("note", ""),
                tp_price=float(v.get("tp_price", 0) or 0),
                sl_price=float(v.get("sl_price", 0) or 0),
            ) for k, v in d.get("shorts", {}).items()
        }
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
