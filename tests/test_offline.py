"""离线测试：不依赖 maibot_sdk，直接测核心逻辑。

跑法：python3 tests/test_offline.py
覆盖：钱包交易引擎（现货/空单/爆仓/定投/序列化）、行情解析、图表HTML、命令路由纯逻辑。
"""

import asyncio
import json
import math
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.market import Candle, MarketAggregator, normalize_symbol  # noqa: E402
from core.wallet import DCAPlan, FuturesPosition, PriceAlert, Trade, Wallet, WalletStore, now  # noqa: E402
from core.chart import curve_html, kline_html, kline_text, sparkline  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# ─── 符号规整 ───
print("[符号规整]")
check("BTC 原样", normalize_symbol("btc") == "BTC")
check("$ETH 去前缀", normalize_symbol("$eth") == "ETH")
check("中文别名 比特币", normalize_symbol("比特币") == "BTC")
check("中文别名 狗狗币", normalize_symbol("狗狗币") == "DOGE")
check("未知符号大写化", normalize_symbol("pepe") == "PEPE")
check("空串", normalize_symbol("") == "")

# ─── 钱包：现货 ───
print("[现货交易]")
w = Wallet("test", 10000.0)
t1 = w.buy("BTC", 5000, 100.0, 0.001)   # 买 5000U 的 BTC @100
check("买入数量", approx(t1.qty, 50.0))
check("现金扣减（含手续费）", approx(w.cash, 10000 - 5000 - 5.0))
check("均价", approx(w.positions["BTC"]["avg_cost"], 100.0))
t2 = w.buy("BTC", 3000, 120.0, 0.001)   # 加仓
check("加仓后数量", approx(w.positions["BTC"]["qty"], 50 + 25))
check("加仓后均价", approx(w.positions["BTC"]["avg_cost"], 8000 / 75))
t3 = w.sell("BTC", None, 150.0, 0.001)  # 全部卖
check("全卖盈亏", approx(t3.pnl, (150 - 8000 / 75) * 75))
check("持仓清空", "BTC" not in w.positions)
check("已实现盈亏累计", approx(w.realized_pnl, t3.pnl))

w2 = Wallet("t2", 1000.0)
try:
    w2.buy("BTC", 2000, 100, 0.001)
    check("超额买入拦截", False)
except ValueError:
    check("超额买入拦截", True)

# ─── 钱包：合约仓位（空/多）与爆仓 ───
print("[杠杆做空]")
w3 = Wallet("t3", 10000.0)
# 1 手 100 U/个，10x：名义 100，保证金 10
w3.open_short("BTC", 1.0, 10.0, 100.0, 0.0, 0.005)
sp = w3.shorts["BTC|S"]
check("保证金占用", approx(sp.margin, 10.0))
check("爆仓价 = entry*(1+1/lev-mmr)", approx(sp.liquidation_price, 100 * (1 + 0.1 - 0.005)))
check("浮盈（跌到90）", approx(sp.unrealized(90.0), 10.0))
check("浮亏（涨到105）", approx(sp.unrealized(105.0), -5.0))
check("空单标记价", sp.side == "short")

tc = w3.close_short("BTC", None, 90.0, 0.0)   # 平空赚10
check("平空盈亏", approx(tc.pnl, 10.0))
check("保证金返还", approx(w3.cash, 10000 + 10.0))

# 爆仓：开 2x 空 @100，爆仓价 100*(1.5-0.005)=149.5
w3.open_short("ETH", 1.0, 2.0, 100.0, 0.0, 0.005)
cash_before = w3.cash
hit = w3.check_liquidation({"ETH": 150.0})
check("爆仓触发", len(hit) == 1)
check("爆仓损失=保证金", approx(w3.realized_pnl, 10.0 - 50.0))  # 前赚10 - 爆仓50
check("空单移除", "ETH|S" not in w3.shorts)
hit2 = w3.check_liquidation({"ETH": 999.0})
check("不重复爆仓", len(hit2) == 0)

# 部分平空
w4 = Wallet("t4", 10000.0)
w4.open_short("SOL", 10.0, 5.0, 10.0, 0.0, 0.005)
w4.close_short("SOL", 5.0, 8.0, 0.0)
check("部分平空后余量", approx(w4.shorts["SOL|S"].qty, 5.0))
check("部分平空盈亏", approx(w4.realized_pnl, (10 - 8) * 5))

# ─── 杠杆做多 ───
print("[杠杆做多]")
wL = Wallet("tL", 10000.0)
# 2 个 @100，5x：名义 200，保证金 40，爆仓价 100*(1-0.2+0.005)=80.5
wL.open_long("BTC", 2.0, 5.0, 100.0, 0.0, 0.005)
lp = wL.shorts["BTC|L"]
check("多单保证金", approx(lp.margin, 40.0))
check("多单爆仓价 = entry*(1-1/lev+mmr)", approx(lp.liquidation_price, 80.5))
check("多单浮盈（涨到110）", approx(lp.unrealized(110.0), 20.0))
check("多单浮亏（跌到95）", approx(lp.unrealized(95.0), -10.0))
check("多空可并存", (wL.open_short("BTC", 1.0, 2.0, 100.0, 0.0, 0.005), "BTC|L" in wL.shorts and "BTC|S" in wL.shorts)[1])
tl = wL.close_long("BTC", None, 110.0, 0.0)
check("平多盈亏", approx(tl.pnl, 20.0))
# 开多占40 + 开空占50 → cash 9910；平多返 40+20 → 9970
check("平多后保证金返还", approx(wL.cash, 9970.0))
# 多单爆仓：1 个 @100 2x，爆仓价 100*(0.5+0.005)=50.5
wL.open_long("ETH", 1.0, 2.0, 100.0, 0.0, 0.005)
hitL = wL.check_liquidation({"ETH": 50.0})
check("多单爆仓触发", len(hitL) == 1 and hitL[0][0].side == "long")
check("多单爆仓损失=保证金", approx(wL.realized_pnl, 20.0 - 50.0))
# 同币重复开多拦截
wL.open_long("SOL", 1.0, 1.0, 50.0, 0.0, 0.005)
try:
    wL.open_long("SOL", 1.0, 1.0, 50.0, 0.0, 0.005)
    check("重复开多拦截", False)
except ValueError:
    check("重复开多拦截", True)

# ─── 定投 ───
print("[定投]")
w5 = Wallet("t5", 100000.0)
plan = DCAPlan(symbol="BTC", amount_usdt=100, interval_hours=6, next_run=now() - 1)
w5.plans[plan.id] = plan
due = w5.due_plans()
check("到期计划检测", len(due) == 1)
w5.buy("BTC", 100, 50.0, 0.001, note=f"dca:{plan.id}")
w5.advance_plan(plan, "ok")
check("执行后下次时间推后", plan.next_run > now())
check("未到期", len(w5.due_plans()) == 0)

# ─── 序列化 round-trip ───
print("[序列化]")
w5.buy("ETH", 300, 100.0, 0.001)
w5.open_short("DOGE", 1000, 3.0, 0.5, 0.001, 0.005)
w5.snapshot({"BTC": 51.0, "ETH": 101.0, "DOGE": 0.48})
d = w5.to_dict()
w5r = Wallet.from_dict(json.loads(json.dumps(d)))
check("现金 round-trip", approx(w5r.cash, w5.cash))
check("持仓 round-trip", approx(w5r.positions["BTC"]["qty"], w5.positions["BTC"]["qty"]))
check("合约 round-trip", approx(w5r.shorts["DOGE|S"].margin, w5.shorts["DOGE|S"].margin))
check("定投 round-trip", w5r.plans[plan.id].symbol == "BTC")
check("快照 round-trip", len(w5r.snapshots) == len(w5.snapshots))
check("成交 round-trip", len(w5r.trades) == len(w5.trades))

# ─── WalletStore ───
print("[WalletStore]")


async def test_store():
    with tempfile.TemporaryDirectory() as td:
        st = WalletStore(Path(td), 50000.0, "global")
        wa = await st.get("stream-1")
        wa.buy("BTC", 1000, 50.0, 0.001)
        await st.save(wa)
        st.forget(wa.key)
        wb = await st.get("stream-2")  # global 模式：同钱包
        check("global 模式共享", approx(wb.positions["BTC"]["qty"], 20.0))

        st2 = WalletStore(Path(td) / "chat", 50000.0, "chat")
        c1 = await st2.get("chat-A")
        c1.buy("BTC", 100, 50.0, 0.001)
        await st2.save(c1)
        st2.forget("chat-A")
        c2 = await st2.get("chat-A")
        c3 = await st2.get("chat-B")
        check("chat 模式隔离恢复", approx(c2.cash, 50000 - 100.1))
        check("chat 模式不同群独立", approx(c3.cash, 50000.0))
        allw = await st2.get_all()
        check("磁盘枚举", len(allw) == 2)


asyncio.run(test_store())

# ─── 止盈止损 ───
print("[止盈止损]")
w6 = Wallet("t6", 10000.0)
w6.buy("BTC", 5000, 100.0, 0.0)                     # 50 个 @100
w6.positions["BTC"]["tp_price"] = 110.0
w6.positions["BTC"]["sl_price"] = 90.0
ev = w6.check_tp_sl({"BTC": 105.0})
check("未到价不触发", ev == [])
ev = w6.check_tp_sl({"BTC": 111.0})
check("现货止盈触发", len(ev) == 1 and "止盈" in ev[0])
check("止盈后持仓清空", "BTC" not in w6.positions)
check("止盈盈亏入账", approx(w6.realized_pnl, (111 - 100) * 50))

w7 = Wallet("t7", 10000.0)
w7.buy("ETH", 2000, 50.0, 0.0)                      # 40 个 @50
w7.positions["ETH"]["sl_price"] = 45.0
ev = w7.check_tp_sl({"ETH": 44.0})
check("现货止损触发", len(ev) == 1 and "止损" in ev[0])
check("止损盈亏为负", approx(w7.realized_pnl, (44 - 50) * 40))

w8 = Wallet("t8", 10000.0)
w8.open_short("SOL", 100, 5.0, 20.0, 0.0, 0.005)    # 空 100 @20
w8.shorts["SOL|S"].tp_price = 18.0
w8.shorts["SOL|S"].sl_price = 22.0
ev = w8.check_tp_sl({"SOL": 19.0})
check("空单未到价不触发", ev == [])
ev = w8.check_tp_sl({"SOL": 17.5})
check("空单止盈触发（跌）", len(ev) == 1 and "止盈" in ev[0])
check("空单止盈后移除", "SOL|S" not in w8.shorts)
check("空单止盈盈亏", approx(w8.realized_pnl, (20 - 17.5) * 100))

# 多单止盈止损（方向与空单相反）
w8b = Wallet("t8b", 10000.0)
w8b.open_long("SOL", 100, 5.0, 20.0, 0.0, 0.005)    # 多 100 @20
w8b.shorts["SOL|L"].tp_price = 22.0
w8b.shorts["SOL|L"].sl_price = 18.0
ev = w8b.check_tp_sl({"SOL": 21.0})
check("多单未到价不触发", ev == [])
ev = w8b.check_tp_sl({"SOL": 22.5})
check("多单止盈触发（涨）", len(ev) == 1 and "止盈" in ev[0])
check("多单止盈盈亏", approx(w8b.realized_pnl, (22.5 - 20.0) * 100))

# set_tp_sl API
w9 = Wallet("t9", 10000.0)
w9.buy("BTC", 1000, 100.0, 0.0)
check("set_tp_sl 现货", w9.set_tp_sl("BTC", tp=120.0) == "spot")
check("set 后值正确", approx(w9.positions["BTC"]["tp_price"], 120.0))
check("set_tp_sl 清除", w9.set_tp_sl("BTC", tp=0.0) == "spot" and w9.positions["BTC"]["tp_price"] == 0.0)
try:
    w9.set_tp_sl("ETH", tp=1.0)
    check("无持仓报错", False)
except ValueError:
    check("无持仓报错", True)
w9.open_short("ETH", 1, 2.0, 100.0, 0.0, 0.005)
check("auto 落到空单", w9.set_tp_sl("ETH", tp=1.0) == "short")
w9.buy("ETH", 500, 100.0, 0.0)  # 现在现货+空单并存
check("同时持有返回歧义", w9.set_tp_sl("ETH", tp=1.0) == "AMBIVALENT")
check("明确 side=short", w9.set_tp_sl("ETH", sl=150.0, side="short") == "short")
check("明确 side=spot", w9.set_tp_sl("ETH", sl=80.0, side="spot") == "spot")

# ─── 资金费结算 ───
print("[资金费]")
wF = Wallet("tF", 10000.0)
wF.open_long("BTC", 1.0, 2.0, 100.0, 0.0, 0.005)    # 多 1 @100，名义 100
wF.open_short("ETH", 2.0, 2.0, 50.0, 0.0, 0.005)     # 空 2 @50，名义 100
cash0 = wF.cash
ev = wF.settle_funding({"BTC": 0.0001, "ETH": 0.0001})  # 正费率：多付 0.01，空收 0.01
check("正费率：多付空收", approx(wF.cash, cash0 - 0.01 + 0.01))
check("多单累计付费", approx(wF.shorts["BTC|L"].funding_paid, 0.01))
check("空单累计收费", approx(wF.shorts["ETH|S"].funding_paid, -0.01))
check("结算事件2条", len(ev) == 2)
ev = wF.settle_funding({"BTC": -0.0002})              # 负费率：多收 0.02
check("负费率：多单收取", approx(wF.cash, cash0 + 0.02))
check("缺费率币种跳过", wF.settle_funding({}) == [])
# 异常费率截断（±5%）
wF.settle_funding({"BTC": 0.5})
check("异常费率截断", approx(wF.shorts["BTC|L"].funding_paid, 0.01 - 0.02 + 100 * 0.05))
# 资金费计入平仓结算：平空返还算清
wF2 = Wallet("tF2", 10000.0)
wF2.open_short("BTC", 1.0, 2.0, 100.0, 0.0, 0.005)   # 保证金 50
wF2.settle_funding({"BTC": 0.001})                    # 空收 0.1
c_before = wF2.cash                                   # 9950 + 0.1
tF2 = wF2.close_short("BTC", None, 100.0, 0.0)        # 平仓不赚不亏
check("平仓时资金费不重复扣", approx(wF2.cash, c_before + 50.0))

# ─── 价格警报 ───
print("[价格警报]")
a1 = PriceAlert(symbol="BTC", op=">=", price=100000, stream_id="s1")
a2 = PriceAlert(symbol="ETH", op="<=", price=3000, stream_id="s1")
check("上穿触发", a1.triggered(100001) and not a1.triggered(99999))
check("下破触发", a2.triggered(2999) and not a2.triggered(3001))

# 序列化含 tp/sl/alerts
w10 = Wallet("t10", 5000.0)
w10.buy("BTC", 1000, 100.0, 0.0)
w10.positions["BTC"]["tp_price"] = 130.0
w10.open_short("DOGE", 1000, 3.0, 0.5, 0.0, 0.005)
w10.shorts["DOGE|S"].sl_price = 0.55
al = PriceAlert(symbol="SOL", op="<=", price=100, stream_id="sx")
w10.alerts[al.id] = al
d10 = json.loads(json.dumps(w10.to_dict()))
w10r = Wallet.from_dict(d10)
check("tp/sl round-trip（现货）", approx(w10r.positions["BTC"]["tp_price"], 130.0))
check("tp/sl round-trip（空单）", approx(w10r.shorts["DOGE|S"].sl_price, 0.55))
check("alerts round-trip", len(w10r.alerts) == 1 and list(w10r.alerts.values())[0].op == "<=")
# 旧格式（无 tp/sl/alerts 字段）向后兼容
old = {"key": "old", "initial_cash": 100, "cash": 100, "positions": {"BTC": {"qty": 1, "avg_cost": 100}},
       "shorts": {"ETH": {"symbol": "ETH", "qty": 1, "entry_price": 100, "leverage": 2, "margin": 50, "liquidation_price": 149.5}}}
wold = Wallet.from_dict(old)
check("旧钱包兼容（key 迁移 ETH|S）", "ETH|S" in wold.shorts and wold.shorts["ETH|S"].side == "short")
check("旧钱包兼容（tp/sl 默认0）", wold.positions["BTC"].get("tp_price", 0) == 0 and wold.shorts["ETH|S"].tp_price == 0)
check("旧钱包兼容（alerts 空）", wold.alerts == {})

# ─── 图表 ───
print("[图表]")
candles = [Candle(ts=1700000000 + i * 3600, open=100 + i, high=102 + i, low=99 + i, close=101 + i, volume=10 + i) for i in range(48)]
kh = kline_html(candles, "BTC", "1h")
check("K线HTML生成", "<svg" in kh and "BTC" in kh and kh.count("<rect") >= 48)
check("K线含涨跌色", UP if False else ("#e64545" in kh))
th = kline_text(candles, "BTC", "1h")
check("文字K线", "▁" in th or "█" in th)
snaps = [[1700000000 + i * 1800, 100000 + i * 137] for i in range(50)]
ch = curve_html(snaps)
check("曲线HTML", "<svg" in ch and "linearGradient" in ch)
check("空快照曲线容错", "快照不足" in curve_html([]))
check("sparkline", len(sparkline([1, 2, 3, 4])) == 4)

# ─── 行情（真实网络，可选） ───
print("[行情源·实网]")
if "--no-net" not in sys.argv:
    async def test_market():
        agg = MarketAggregator()
        try:
            lat = await asyncio.wait_for(agg.measure(), timeout=20)
            ok = {k: v for k, v in lat.items() if v > 0}
            check("至少一个源可用", len(ok) >= 1, str(lat))
            p, src = await agg.get_price("BTC")
            check("BTC 取价", 1000 < p < 1_000_000, f"{p} via {src}")
            p2, _ = await agg.get_price("BTC")  # 命中缓存
            check("价格缓存", p2 == p)
            t = await agg.get_ticker24h("ETH")
            check("ETH 24h", t.price > 0)
            ks = await agg.get_klines("BTC", "1h", 24)
            check("K线拉取", len(ks) >= 20 and ks[-1].ts > ks[0].ts)
            ps = await agg.get_prices(["BTC", "ETH", "SOL"])
            check("批量取价", len(ps) >= 2)
            ov = await agg.get_overview(("BTC", "ETH"))
            check("大盘概览", len(ov) >= 1 and ov[0][0] == "BTC")
        except Exception as e:
            check("行情源（网络不可用则跳过）", False, str(e))
    asyncio.run(test_market())
else:
    print("  （跳过，--no-net）")

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
