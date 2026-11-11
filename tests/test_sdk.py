"""SDK 集成冒烟测试：用真实 maibot_sdk 加载插件，mock PluginContext。

跑法（SDK 已解压到 /tmp/opencode/sdk）：
    PYTHONPATH=/tmp/opencode/sdk python3 tests/test_sdk.py
覆盖：create_plugin、生命周期、组件收集（Command/Tool 元数据）、
Command 处理器全流程（含行情实网）、渲染降级。
"""

import asyncio
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

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

    ok, msg, _ = await run_cmd("close ETH")
    check("平空", ok and "平空" in msg, msg)

    ok, msg, _ = await run_cmd("dca add BTC 100u 0.5h")
    check("添加定投", ok and "定投" in msg, msg)

    ok, msg, _ = await run_cmd("dca list")
    check("定投列表", ok and "BTC" in msg)

    ok, msg, _ = await run_cmd("history 5")
    check("成交记录", ok and "买入" in msg)

    ok, msg, _ = await run_cmd("source")
    check("测速", ok and ("ms" in msg or "不可达" in msg))

    ok, msg, _ = await run_cmd("market")
    check("大盘概览", ok and "BTC" in msg)

    ok, msg, _ = await run_cmd("reset")
    check("reset 需确认", not ok and "确认" in msg)
    ok, msg, _ = await run_cmd("reset 确认")
    check("reset 确认执行", ok and "重置" in msg)

    print("[新功能命令]")
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
    tick_fast.cancel()
    try:
        await tick_fast
    except asyncio.CancelledError:
        pass
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
    ok, msg, _ = await p.tool_trade(action="close", symbol="ETH", amount="all", stream_id="s1")
    check("tool 平空", ok and "平空" in msg, msg)
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

    print("[生命周期收尾]")
    await p.on_unload()
    check("on_unload 保存", (ROOT / "tests" / ".tmp_data" / "wallet___global__.json").exists())
    await p2.on_unload()

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(main()))
