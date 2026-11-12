"""SDK 集成冒烟测试：用真实 maibot_sdk 加载插件，mock PluginContext。

跑法（SDK 已解压到 /tmp/opencode/sdk）：
    PYTHONPATH=/tmp/opencode/sdk python3 tests/test_sdk.py
覆盖：create_plugin、生命周期、组件收集（Command/Tool 元数据）、
Command 处理器全流程（含行情实网）、渲染降级。
"""

import asyncio
import json
import logging
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import maibot_sdk  # noqa: F401
except ImportError:
    print("!! maibot_sdk 不可用（需 PYTHONPATH 指向 SDK），跳过")
    sys.exit(0)

from maibot_sdk.components import collect_components  # noqa: E402

import plugin as plugin_mod  # noqa: E402

PASS, FAIL = 0, 0


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


class MockSend:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def text(self, text: str, stream_id: str, **kw):
        self.sent.append(("text", text))
        return True

    async def image(self, image_data: str, stream_id: str, **kw):
        self.sent.append(("image", image_data[:32]))
        return True


class MockRender:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def html2png(self, html: str, **kw):
        if self.fail:
            raise RuntimeError("mock: driver not ready")
        return {"success": True, "image_base64": "ZmFrZQ==", "mime_type": "image/png", "width": 800, "height": 400}


class MockChat:
    """chat 能力 mock：预置群/用户 → stream_id 映射。"""

    def __init__(self):
        self.groups = {"123456": "stream-group-1"}
        self.users = {"987654": "stream-user-1"}

    async def get_stream_by_group_id(self, group_id: str, platform: str = "qq"):
        return {"stream_id": self.groups.get(group_id)} if group_id in self.groups else None

    async def get_stream_by_user_id(self, user_id: str, platform: str = "qq"):
        return {"stream_id": self.users.get(user_id)} if user_id in self.users else None


class MockCtx:
    def __init__(self, render_fail: bool = False):
        self.send = MockSend()
        self.render = MockRender(render_fail)
        self.chat = MockChat()
        self.logger = logging.getLogger("test.plugin")
        self.paths = SimpleNamespace(data_dir=ROOT / "tests" / ".tmp_data")
        # LLM mock：回放决策用。脚本按调用次序返回动作指令
        self.llm_script: List[str] = []
        self.llm_reasoning = "测试推理过程"
        self.llm_calls = 0

    class _LLM:
        def __init__(self, outer: "MockCtx"):
            self._outer = outer

        async def generate(self, prompt: str = "", **kwargs):
            self._outer.llm_calls += 1
            i = min(self._outer.llm_calls - 1, len(self._outer.llm_script) - 1)
            text = self._outer.llm_script[i] if self._outer.llm_script else "hold"
            return {"success": True, "response": text, "reasoning": self._outer.llm_reasoning, "model": "mock"}

    def __post_init__(self):
        pass

    @property
    def llm(self):
        return MockCtx._LLM(self)


async def make_plugin(render_fail: bool = False):
    p = plugin_mod.create_plugin()
    ctx = MockCtx(render_fail)
    p._ctx = ctx
    # 模拟 Runner 注入配置实例
    from maibot_sdk.config import PluginConfigBase  # noqa: F401
    model = type(p).config_model
    p._plugin_config_instance = model()
    p._plugin_config_data = {}
    return p, ctx


async def main():
    print("[加载与组件]")
    p, ctx = await make_plugin()

    # 生命周期
    await p.on_load()
    check("on_load 无异常", True)
    check("store 初始化", hasattr(p, "store") and p.store.mode == "global")
    check("后台任务已启动", p._tick_task is not None and not p._tick_task.done())

    infos = collect_components(p)
    cmds = [i for i in infos if i.get("type") == "COMMAND"]
    tools = [i for i in infos if i.get("type") == "TOOL"]
    check("注册 1 个 Command", len(cmds) == 1 and cmds[0]["name"] == "coin", str([i.get("name") for i in infos]))
    check("注册 5 个 Tool", len(tools) == 5, str([i.get("name") for i in infos]))
    tool_names = {i["name"] for i in tools}
    check("Tool 名称齐全", {"crypto_get_price", "crypto_get_portfolio", "crypto_trade", "crypto_set_alert", "crypto_market_overview"} <= tool_names)
    if cmds:
        import re as _re
        pat = _re.compile(cmds[0].get("metadata", {}).get("command_pattern") or "")
        check("pattern 匹配 /coin", bool(pat.match("/coin")))
        check("pattern 匹配 /coin buy BTC 100u", bool(pat.match("/coin buy BTC 100u")))
        check("pattern 匹配别名 /炒币", bool(pat.match("/炒币 仓位")))

    print("[Command 全流程]")
    async def run_cmd(arg_str: str):
        m = None
        import re as _re
        m = _re.search(r"^/(?:coin|币|炒币)(?:\s+(.+))?$", "/coin" + (f" {arg_str}" if arg_str else ""))
        return await p.handle_coin(stream_id="s1", raw_message="", matched_groups={"args": m.group(1) or ""} if m else {})

    ok, msg, _ = await run_cmd("help")
    check("help 命令", ok and "炒币" in msg)

    ok, msg, _ = await run_cmd("")
    check("资产总览（空仓）", ok and "总资产" in msg and "100,000" in msg)

    ok, msg, _ = await run_cmd("price 比特币")
    check("价格查询（中文别名）", ok and "BTC" in msg, msg)

    ok, msg, _ = await run_cmd("buy BTC 1000u")
    check("买入 1000u", ok and "买入" in msg, msg)

    ok, msg, _ = await run_cmd("buy ETH 0.5")
    check("按数量买入", ok and "买入" in msg, msg)

    ok, msg, _ = await run_cmd("pos")
    check("持仓总览", ok and "BTC" in msg and "ETH" in msg)

    ok, msg, _ = await run_cmd("sell BTC all")
    check("全仓卖出", ok and "卖出" in msg, msg)

    ok, msg, _ = await run_cmd("short ETH 100u 3x")
    check("开空 3x", ok and "开空" in msg and "爆仓" in msg, msg)

    ok, msg, _ = await run_cmd("long ETH 200u 5x")
    check("开多 5x", ok and "开多" in msg and "爆仓" in msg, msg)

    ok, msg, _ = await run_cmd("pos")
    check("多空并存总览", ok and "多" in msg and "空" in msg and "合约" in msg, msg)

    ok, msg, _ = await run_cmd("close ETH short")
    check("指定 side 平空", ok and "平空" in msg, msg)

    ok, msg, _ = await run_cmd("close ETH")
    check("自动 side 平多", ok and "平多" in msg, msg)

    ok, msg, _ = await run_cmd("long BTC 100u 2x")
    check("再开多（备资金费测试）", ok and "开多" in msg, msg)

    ok, msg, _ = await run_cmd("close BTC")
    check("平掉备仓", ok and "平多" in msg, msg)

    ok, msg, _ = await run_cmd("dca add BTC 100u 0.5h")
    check("添加定投", ok and "定投" in msg, msg)

    ok, msg, _ = await run_cmd("dca list")
    check("定投列表", ok and "BTC" in msg)

    ok, msg, _ = await run_cmd("history 10")
    check("成交记录", ok and ("买入" in msg and "开空" in msg and "开多" in msg), msg)

    ok, msg, _ = await run_cmd("source")
    check("测速", ok and ("ms" in msg or "不可达" in msg))

    ok, msg, _ = await run_cmd("market")
    check("大盘概览", ok and "BTC" in msg)

    ok, msg, _ = await run_cmd("reset")
    check("reset 需确认", not ok and "确认" in msg)
    ok, msg, _ = await run_cmd("reset 确认")
    check("reset 确认执行", ok and "重置" in msg)

    print("[新功能命令]")
    ok, msg, _ = await run_cmd("price BTC ETH")
    check("多币查价", ok and "BTC" in msg and "ETH" in msg and msg.count("\n") >= 1, msg)

    ok, msg, _ = await run_cmd("funding BTC")
    check("funding 指定币种", ok and "资金费率" in msg and "%" in msg, msg)

    ok, msg, _ = await run_cmd("funding")
    check("funding 无仓位无参数报错", not ok and "用法" in msg, msg)

    ok, msg, _ = await run_cmd("alert add BTC >= 99999999")
    check("添加价格警报", ok and "警报已设" in msg, msg)
    ok, msg, _ = await run_cmd("alert")
    check("警报列表", ok and "99,999,999" in msg, msg)
    aid = msg.split("[")[1].split("]")[0] if "[" in msg else ""
    ok, msg, _ = await run_cmd(f"alert del {aid}")
    check("删除警报", ok and "已删除" in msg, msg)
    ok, msg, _ = await run_cmd("alert add BTC 瞎写 123")
    check("警报非法条件报错", not ok and ">=" in msg, msg)

    # 警报触发路径：设一个立刻会触发的（低价的 >=），跑一轮巡检
    ok, msg, _ = await run_cmd("alert add BTC >= 1")
    check("添加必触发警报", ok)
    # 同时备一个已到期的资金费结算（有合约仓位才会结算）
    ok, msg, _ = await run_cmd("long BTC 100u 20x")
    check("开多备资金费", ok)
    ok, msg, _ = await run_cmd("funding")
    check("funding 缺省=持仓币种", ok and "BTC" in msg and "资金费率" in msg, msg)
    ok, msg, _ = await run_cmd("pos")
    check("总览含累计资金费", ok and "累计资金费" in msg, msg)
    w_g = await p.store.get("s1")
    w_g.funding_next = 1  # 已过期 → 巡检首轮就结算
    await p.store.save(w_g)
    await p.market.get_funding("BTC", default=p.config.trading.funding_default)  # 预热缓存，结算路径不再依赖网络时延
    p._tick_task.cancel()
    try:
        await p._tick_task
    except asyncio.CancelledError:
        pass
    p.config.task.tick_seconds = 1   # 先改间隔再起任务（loop 首轮 sleep 读的就是它）
    tick_fast = asyncio.create_task(p._background_loop())
    await asyncio.sleep(2.5)
    texts_all = [t[1] for t in ctx.send.sent if t[0] == "text"]
    check("警报触发通知", any("价格警报" in t for t in texts_all), str(texts_all[-2:]))
    check("资金费结算通知", any("资金费" in t for t in texts_all), str(texts_all[-2:]))
    check("临近爆仓预警", any("临近爆仓" in t for t in texts_all), str(texts_all[-2:]))
    w_g2 = await p.store.get("s1")
    check("结算后 funding_next 推进", w_g2.funding_next > int(__import__("time").time()), str(w_g2.funding_next))
    check("多单付/收资金费记录", any("资金费" in t and "多" in t for t in texts_all), str(texts_all[-2:]))
    tick_fast.cancel()
    try:
        await tick_fast
    except asyncio.CancelledError:
        pass
    ok, msg, _ = await run_cmd("close BTC")
    check("平掉资金费测试仓", ok and "平多" in msg, msg)
    p.config.task.tick_seconds = 60
    p._tick_task = asyncio.create_task(p._background_loop())

    ok, msg, _ = await run_cmd("buy ETH 500u")
    check("买入 ETH（备设 tp）", ok)
    import re as _re2
    cur_eth = float(_re2.search(r"@ ([\d,]+\.?\d*)", msg).group(1).replace(",", ""))  # 「@ 现价」处抓价格
    ok, msg, _ = await run_cmd(f"tp ETH {cur_eth * 2:.0f}")
    check("设止盈", ok and "止盈已设" in msg, msg)
    ok, msg, _ = await run_cmd("pos")
    check("持仓显示 TP", ok and "TP" in msg, msg)
    ok, msg, _ = await run_cmd("tp ETH clear")
    check("清除止盈", ok and "清除" in msg, msg)
    ok, msg, _ = await run_cmd("sl ETH 0.0000001")
    check("设必触止损（带提示）", ok and "止损已设" in msg, msg)
    ok, msg, _ = await run_cmd("tp DOGE 123")
    check("无持仓设tp报错", not ok and "没有" in msg, msg)

    ok, msg, _ = await run_cmd("rank")
    check("global 模式 rank 拒绝", not ok and "chat" in msg, msg)
    p.store.mode = "chat"
    ok, msg, _ = await run_cmd("rank")
    check("chat 模式 rank", ok and ("🥇" in msg or "U" in msg), msg)
    p.store.mode = "global"

    ok, msg, _ = await run_cmd("notify")
    

    ok, msg, _ = await p.tool_set_alert(symbol="BTC", op="上穿", price="88888888", stream_id="s1")
    check("tool 设警报", ok and "警报已设" in msg, msg)

    ok, msg, _ = await run_cmd("buy BTC abc")
    check("非法参数报错", not ok)

    ok, msg, _ = await run_cmd("kline BTC")
    check("K线（渲染成功路径）", ok, msg)
    check("K线图片已发送", any(t[0] == "image" for t in ctx.send.sent))

    print("[渲染降级]")
    p2, ctx2 = await make_plugin(render_fail=True)
    await p2.on_load()
    ok, msg, _ = await p2.handle_coin(stream_id="s2", matched_groups={"args": "kline BTC"})
    check("渲染失败降级文字", ok, msg)
    texts = [t[1] for t in ctx2.send.sent if t[0] == "text"]
    check("降级文字含迷你图", any("▁" in t or "█" in t for t in texts), str(texts[-1:]))

    print("[Tool 调用]")
    ok, msg, _ = await p.tool_get_price(symbol="BTC")
    check("tool 查价", ok and "BTC" in msg)
    ok, msg, _ = await p.tool_get_portfolio(stream_id="s1")
    check("tool 持仓", ok and "总资产" in msg)
    ok, msg, _ = await p.tool_trade(action="buy", symbol="BTC", amount="100u", stream_id="s1")
    check("tool 买入", ok and "买入" in msg, msg)
    ok, msg, _ = await p.tool_trade(action="short", symbol="ETH", amount="100u", leverage="5", stream_id="s1")
    check("tool 开空", ok and "开空" in msg, msg)
    ok, msg, _ = await p.tool_trade(action="long", symbol="ETH", amount="50u", leverage="2", stream_id="s1")
    check("tool 开多", ok and "开多" in msg, msg)
    ok, msg, _ = await p.tool_trade(action="close", symbol="ETH", amount="all", leverage="", stream_id="s1")
    check("tool 平空（多空并存报错）", (not ok and "多空都有" in msg), msg)
    ok, msg, _ = await p.tool_trade(action="short", symbol="SOL", amount="10u", leverage="2", stream_id="s1")
    check("tool 开空 SOL", ok and "开空" in msg, msg)
    ok, msg, _ = await p.tool_trade(action="close", symbol="SOL", amount="all", stream_id="s1")
    check("tool 平空（单侧）", ok and "平空" in msg, msg)
    ok, msg, _ = await p.tool_trade(action="close", symbol="ETH", amount="all short", stream_id="s1")
    check("tool 平空（明确 short）", ok and "平空" in msg, msg)
    ok, msg, _ = await p.tool_trade(action="close", symbol="ETH", amount="all", stream_id="s1")
    check("tool 平多（自动 side）", ok and "平多" in msg, msg)
    ok, msg, _ = await p.tool_market_overview()
    check("tool 大盘", ok and "BTC" in msg)

    print("[通知目标]")
    # 配置了两个目标：一个群（mock 可解析）、一个未知号码
    p.config.task.alert_targets = ["123456", "000000"]
    ctx.send.sent.clear()
    await p._notify(p.store and await p.store.get("s1") or type("W", (), {"last_stream_id": ""})(), "⚠️ 测试通知")
    texts = [t[1] for t in ctx.send.sent if t[0] == "text"]
    check("配置目标发送（可解析的群）", any("测试通知" in t for t in texts), str(texts))
    check("未知目标跳过且不重复回退", len([t for t in texts if "测试通知" in t]) == 1, str(texts))

    # 解析单元
    sid = await p._resolve_target("123456")
    check("群号解析", sid == "stream-group-1", sid)
    sid = await p._resolve_target("987654")
    check("QQ号解析", sid == "stream-user-1", sid)
    sid = await p._resolve_target("000000")
    check("未知目标解析为空", sid == "")

    # 无配置 → 回退最近活跃流
    p.config.task.alert_targets = []
    ctx.send.sent.clear()
    w1 = await p.store.get("s1")
    await p._notify(w1, "⚠️ 回退通知")
    texts = [t[1] for t in ctx.send.sent if t[0] == "text"]
    check("未配置回退最近活跃流", len(texts) == 1 and "回退通知" in texts[0], str(texts))

    ok, msg, _ = await run_cmd("notify")
    check("notify 命令（无目标）", ok and "未配置" in msg, msg)

    # 恢复目标配置再测 notify 命令
    p.config.task.alert_targets = ["123456", "000000"]
    ok, msg, _ = await run_cmd("notify")
    check("notify 命令（有目标）", ok and "123456" in msg and "✅" in msg, msg)

    print("[WebUI 仪表盘 API]")
    infos = collect_components(p)
    api_comps = [i for i in infos if i.get("type") == "API"]
    check("注册 dashboard API", any(i["name"] == "get_dashboard" for i in api_comps), str([i.get("name") for i in api_comps]))

    dash = await p.api_get_dashboard()
    check("仪表盘返回结构", all(k in dash for k in ("overview", "spot", "futures", "trades", "curve")), str(dash.keys()))
    ov = dash["overview"]
    check("总览数值完备", all(k in ov for k in ("total", "pnl", "cash", "spot_value", "margin", "pnl_pct")), str(ov.keys()))
    check("现货持仓行", isinstance(dash["spot"], list) and any(r["symbol"] in ("BTC", "ETH") for r in dash["spot"]), str(dash["spot"][:2]))
    check("成交记录行", isinstance(dash["trades"], list) and len(dash["trades"]) > 0 and "action" in dash["trades"][0])
    check("曲线数据点", isinstance(dash["curve"], list) and len(dash["curve"]) >= 1 and {"t", "v"} == set(dash["curve"][0].keys()))
    check("总资产恒等式", approx(ov["total"], ov["cash"] + ov["spot_value"] + ov["margin"] + ov["fut_upnl"], 0.02), str(ov))
    # webui.json 与 API 声明一致性
    wj = json.loads((ROOT / "webui.json").read_text("utf-8"))
    page = wj["pages"][0]
    bound = {b["api"] for b in page["queries"].values()}
    declared = {i["name"] for i in api_comps}
    check("webui.json 绑定的 API 均已注册", bound <= declared, f"{bound} vs {declared}")

    print("[决策日志]")
    check("journal 初始化", hasattr(p, "journal") and p.journal is not None)
    # Tool 调用带 reasoning → 应记入 llm 日志
    ok, msg, _ = await p.tool_get_price(symbol="BTC", stream_id="s1", reasoning="用户问BTC价格，我来查一下")
    check("tool 查价（带 reasoning）", ok, msg)
    await asyncio.sleep(0.3)
    j = await p.api_get_journal(limit=50, kind="llm")
    llm_entries = j["entries"]
    check("LLM 决策已记录", len(llm_entries) > 0 and "crypto_get_price" in llm_entries[0]["title"], str(llm_entries[:1]))
    check("reasoning 已捕获", any("用户问BTC" in (e.get("reasoning") or "") for e in llm_entries), str(llm_entries[0]))
    # 命令 → command 日志
    ok, msg, _ = await run_cmd("price BTC")
    check("price 命令", ok, msg)
    await asyncio.sleep(0.3)
    j2 = await p.api_get_journal(limit=50, kind="command")
    check("命令已记录", any("/coin price" in e["title"] for e in j2["entries"]), str(j2["entries"][:1]))
    # 全量与统计
    j3 = await p.api_get_journal(limit=200, kind="")
    check("全量日志与计数", j3["total"] >= 2 and j3["counts"]["llm"] >= 1 and j3["counts"]["command"] >= 1, str(j3["counts"]))
    # 日志持久化（journal.jsonl 落盘）
    await asyncio.sleep(0.3)
    jp = ROOT / "tests" / ".tmp_data" / "journal.jsonl"
    check("日志文件落盘", jp.exists(), str(jp))
    # kind 过滤参数容错
    j4 = await p.api_get_journal(limit=50, kind="bogus")
    check("非法 kind 当作全部", "entries" in j4)

    print("[历史回放]")
    rs = await p.api_replay_status()
    check("回放状态结构", "presets" in rs and "week" in rs["presets"], str(rs))
    # 非法参数：现在 bogus 会被当币种处理（normalize 通过），多余日期参数才报错
    r2 = await p.api_run_replay(preset="day", symbol="BTC", until="not-a-date")
    deadline0 = time.time() + 90
    while p._replay_task and not p._replay_task.done() and time.time() < deadline0:
        await asyncio.sleep(0.5)
    check("非法日期拒绝", not r2["success"] and "日期" in r2["message"], r2["message"])
    # 自由交易回放：mock LLM 脚本（买入 → 持有 → 卖出），day=1h×24，节流至 6 次决策
    p.config.task.replay_max_decisions = 6
    ctx.llm_script = ["buy 300u", "hold", "hold", "sell all", "hold", "hold"]
    ctx.llm_calls = 0
    r3 = await p.api_run_replay(preset="day", symbol="BTC")
    check("回放任务后台启动", r3["success"] and "已开始" in r3["message"], r3["message"])
    if r3["success"]:
        # 等回放任务完成（真实拉取 1 天的 1h K线 + 6 次 mock 决策）
        deadline = time.time() + 90
        while p._replay_task and not p._replay_task.done() and time.time() < deadline:
            await asyncio.sleep(0.5)
        check("回放任务完成", p._replay_task is not None and p._replay_task.done())
        replay_err = p._replay_task.exception() if p._replay_task.done() and not p._replay_task.cancelled() else None
        check("回放无异常", replay_err is None, str(replay_err))
        # 麦麦 LLM 被实际调用（节流后 ≥2 次：买入与卖出）
        check("麦麦决策被调用", ctx.llm_calls >= 2, f"calls={ctx.llm_calls}")
        # 决策日志有 LLM 记录（回放决策）
        await asyncio.sleep(0.3)
        jdec = await p.api_get_journal(limit=100, kind="llm")
        check("回放决策入 llm 日志", any("回放决策" in e["title"] for e in jdec["entries"]), str(jdec["entries"][:1]))
        jreason = [e for e in jdec["entries"] if "回放决策" in e["title"]]
        check("决策含 reasoning", any(e.get("reasoning") for e in jreason), str(jreason[:1]))
        # 回放结束日志
        j5 = await p.api_get_journal(limit=100, kind="system")
        check("回放结束入 system 日志", any("历史回放结束" in e["title"] for e in j5["entries"]), str(j5["entries"][:1]))
        # 钱包有回放痕迹（成交+曲线）
        dash = await p.api_get_dashboard()
        ov = dash["overview"]
        check("回放后资产恒等式", approx(ov["total"], ov["cash"] + ov["spot_value"] + ov["margin"] + ov["fut_upnl"], 0.02), str(ov))
        check("回放后有成交", len(dash["trades"]) > 0)
        check("回放后曲线多条", len(dash["curve"]) > 3, f"{len(dash['curve'])} 点")
        # 回放中再触发 → 拒绝并发
        ctx.llm_script = ["hold"]
        r5 = await p.api_run_replay(preset="day", symbol="BTC")
        if r5["success"]:
            deadline = time.time() + 90
            while p._replay_task and not p._replay_task.done() and time.time() < deadline:
                await asyncio.sleep(0.5)
        else:
            check("并发回放拒绝", "正在运行" in r5["message"], r5["message"])
    else:
        check("回放启动失败跳过后续", False, r3["message"])
    # 命令入口：立即返回「已开始」，回放后台执行，完成后主动发消息
    ctx.llm_script = ["hold"]
    ok, msg, _ = await run_cmd("replay")
    check("replay 命令立即返回已开始", ok and "回放开始" in msg, msg)
    check("replay 命令无重复报错", "多余参数" not in msg, msg)
    # 等命令触发的后台回放跑完
    deadline = time.time() + 90
    while p._replay_task and not p._replay_task.done() and time.time() < deadline:
        await asyncio.sleep(0.5)
    check("命令回放后台完成", p._replay_task is not None and p._replay_task.done())
    # 回放中再开一次 → 拒绝（并发保护）；此时已跑完则改为验证参数校验
    r_dup = await p.api_run_replay(preset="bogus")
    check("非法预设立即拒绝", not r_dup["success"] and "预设" in r_dup["message"], r_dup["message"])
    await asyncio.sleep(0.3)
    # 重置 API（清场，恢复初始资金）
    rr = await p.api_reset_wallet()
    check("页面重置钱包", rr["success"] and "重置" in rr["message"], str(rr))

    print("[生命周期收尾]")
    await p.on_unload()
    check("on_unload 保存", (ROOT / "tests" / ".tmp_data" / "wallet___global__.json").exists())
    await p2.on_unload()

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(main()))
