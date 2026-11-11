"""K线图与资产曲线：生成纯内联 SVG 的 HTML（无外部资源，适配 render.html2png 的
allow_network=False），渲染失败时降级为文字迷你图。配色遵循国内习惯：红涨绿跌。
"""

from __future__ import annotations

import html
from typing import List, Optional, Sequence, Tuple

from .market import Candle

# 防止 float 精度问题：SVG 坐标计算用小数
UP_COLOR = "#e64545"       # 涨：红
DOWN_COLOR = "#1fb374"     # 跌：绿
GRID_COLOR = "#2a2f3a"
TEXT_COLOR = "#c8ccd4"
BG_COLOR = "#171a21"
ACCENT = "#f0b90b"         # 主色调（币安金）


def _fmt_price(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def _fmt_ts(ts: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _page(inner_svg: str, title: str, sub: str, w: int, h: int) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0e1016; font-family:'Noto Sans CJK SC','WenQuanYi Micro Hei','Microsoft YaHei',sans-serif; }}
.page {{ width:{w}px; background:{BG_COLOR}; padding:20px 24px 16px; }}
h1 {{ color:#fff; font-size:20px; font-weight:600; }}
.sub {{ color:{TEXT_COLOR}; font-size:12px; margin:4px 0 10px; }}
svg {{ display:block; }}
</style></head><body><div class="page">
<h1>{html.escape(title)}</h1>
<div class="sub">{html.escape(sub)}</div>
{inner_svg}
</div></body></html>"""


# ─────────────────────────── K线图 ───────────────────────────

def kline_html(candles: Sequence[Candle], symbol: str, interval: str, source: str = "") -> str:
    W, H = 1060, 420
    L, R, T, B = 10, 74, 14, 26          # 边距：右留给价格轴，下留给时间轴
    cw, ch = W - L - R, H - T - B
    n = len(candles)
    if n == 0:
        return _page("", f"{symbol} K线", "无数据", W, 120)

    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    if hi == lo:
        hi, lo = hi * 1.001, lo * 0.999
    pad = (hi - lo) * 0.06
    hi, lo = hi + pad, lo - pad
    vmax = max(c.volume for c in candles) or 1.0

    def y(p: float) -> float:
        return T + ch * (1 - (p - lo) / (hi - lo))

    def x(i: int) -> float:
        step = cw / n
        return L + i * step + step / 2

    parts: List[str] = []
    # 网格 + 价格轴（5 档）
    for k in range(5):
        gy = T + ch * k / 4
        p = hi - (hi - lo) * k / 4
        parts.append(f'<line x1="{L}" y1="{gy:.1f}" x2="{L+cw}" y2="{gy:.1f}" stroke="{GRID_COLOR}" stroke-width="1"/>')
        parts.append(f'<text x="{L+cw+6}" y="{gy+4:.1f}" fill="{TEXT_COLOR}" font-size="11">{_fmt_price(p)}</text>')
    # 时间轴（每 ~6 根一个标签）
    step_lab = max(1, n // 6)
    for i in range(0, n, step_lab):
        parts.append(f'<text x="{x(i):.1f}" y="{H-8}" fill="{TEXT_COLOR}" font-size="10" text-anchor="middle">{_fmt_ts(candles[i].ts)}</text>')
    # 蜡烛 + 成交量
    bw = max(2.0, cw / n * 0.62)
    for i, c in enumerate(candles):
        color = UP_COLOR if c.close >= c.open else DOWN_COLOR
        cx = x(i)
        parts.append(f'<line x1="{cx:.1f}" y1="{y(c.high):.1f}" x2="{cx:.1f}" y2="{y(c.low):.1f}" stroke="{color}" stroke-width="1"/>')
        top, bot = y(max(c.open, c.close)), y(min(c.open, c.close))
        if bot - top < 1:
            bot = top + 1
        parts.append(f'<rect x="{cx-bw/2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{bot-top:.1f}" fill="{color}"/>')
        # 成交量（底部 15% 区域）
        vh = ch * 0.15 * (c.volume / vmax)
        parts.append(
            f'<rect x="{cx-bw/2:.1f}" y="{T+ch-vh:.1f}" width="{bw:.1f}" height="{vh:.1f}" fill="{color}" opacity="0.35"/>'
        )
    # 最新价虚线
    last = candles[-1]
    ly = y(last.close)
    parts.append(f'<line x1="{L}" y1="{ly:.1f}" x2="{L+cw}" y2="{ly:.1f}" stroke="{ACCENT}" stroke-width="1" stroke-dasharray="4,4" opacity="0.8"/>')
    parts.append(f'<rect x="{L+cw+2}" y="{ly-9:.1f}" width="68" height="18" rx="3" fill="{ACCENT}"/>')
    parts.append(f'<text x="{L+cw+8}" y="{ly+4:.1f}" fill="#111" font-size="11" font-weight="bold">{_fmt_price(last.close)}</text>')

    first = candles[0].close
    chg = (last.close - first) / first * 100 if first else 0
    sub = f"{interval} · {n}根 · 期间{chg:+.2f}% · 首 {_fmt_price(first)} → 末 {_fmt_price(last.close)}" + (f" · {source}" if source else "")
    svg = f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">' + "".join(parts) + "</svg>"
    return _page(svg, f"{symbol}/USDT K线", sub, W + 48, H + 80)


# ─────────────────────────── 资产曲线 ───────────────────────────

def curve_html(snapshots: Sequence[Sequence[float]], title: str = "资产曲线") -> str:
    W, H = 1060, 380
    L, R, T, B = 12, 90, 16, 26
    cw, chh = W - L - R, H - T - B
    pts = [(float(a), float(b)) for a, b in snapshots if b > 0]
    if len(pts) < 2:
        return _page("", title, "快照不足（等后台任务多跑一会儿）", W, 140)

    ts = [p[0] for p in pts]
    vs = [p[1] for p in pts]
    t0, t1 = min(ts), max(ts)
    v0, v1 = min(vs), max(vs)
    if v1 == v0:
        v1 = v0 * 1.001
    if t1 == t0:
        t1 = t0 + 1
    pad = (v1 - v0) * 0.08
    v0, v1 = v0 - pad, v1 + pad

    def X(t: float) -> float:
        return L + cw * (t - t0) / (t1 - t0)

    def Y(v: float) -> float:
        return T + chh * (1 - (v - v0) / (v1 - v0))

    up_trend = vs[-1] >= vs[0]
    line_color = UP_COLOR if up_trend else DOWN_COLOR

    # 曲线下方渐变填充
    path_pts = [f"{X(t):.1f},{Y(v):.1f}" for t, v in pts]
    area = f"M{L},{T+chh} L" + " L".join(path_pts) + f" L{X(ts[-1]):.1f},{T+chh} Z"
    line = "M" + " L".join(path_pts)

    parts: List[str] = [f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
                        f'<stop offset="0%" stop-color="{line_color}" stop-opacity="0.35"/>'
                        f'<stop offset="100%" stop-color="{line_color}" stop-opacity="0.02"/>'
                        f'</linearGradient></defs>']
    # 网格与数值轴
    for k in range(5):
        gy = T + chh * k / 4
        val = v1 - (v1 - v0) * k / 4
        parts.append(f'<line x1="{L}" y1="{gy:.1f}" x2="{L+cw}" y2="{gy:.1f}" stroke="{GRID_COLOR}" stroke-width="1"/>')
        parts.append(f'<text x="{L+cw+6}" y="{gy+4:.1f}" fill="{TEXT_COLOR}" font-size="11">{val:,.0f}</text>')
    for k in range(0, len(pts), max(1, len(pts) // 5)):
        parts.append(f'<text x="{X(ts[k]):.1f}" y="{H-8}" fill="{TEXT_COLOR}" font-size="10" text-anchor="middle">{_fmt_ts(int(ts[k]))}</text>')
    # 基准线（初始资金）
    base_v = pts[0][1]
    by = Y(base_v)
    parts.append(f'<line x1="{L}" y1="{by:.1f}" x2="{L+cw}" y2="{by:.1f}" stroke="{TEXT_COLOR}" stroke-width="1" stroke-dasharray="2,5" opacity="0.5"/>')
    parts.append(f'<path d="{area}" fill="url(#g)"/>')
    parts.append(f'<path d="{line}" fill="none" stroke="{line_color}" stroke-width="2.2" stroke-linejoin="round"/>')
    # 末点标记
    lx, ly2 = X(ts[-1]), Y(vs[-1])
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly2:.1f}" r="4" fill="{line_color}"/>')
    parts.append(f'<rect x="{L+cw+2}" y="{ly2-9:.1f}" width="82" height="18" rx="3" fill="{line_color}"/>')
    parts.append(f'<text x="{L+cw+6}" y="{ly2+4:.1f}" fill="#111" font-size="11" font-weight="bold">{vs[-1]:,.0f}</text>')

    chg = (vs[-1] - vs[0]) / vs[0] * 100 if vs[0] else 0
    sub = f"{len(pts)}个快照 · 期间{chg:+.2f}% · {_fmt_ts(int(t0))} → {_fmt_ts(int(t1))}"
    svg = f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">' + "".join(parts) + "</svg>"
    return _page(svg, title, sub, W + 48, H + 80)


# ─────────────────────────── 文字迷你图（降级） ───────────────────────────

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1.0
    return "".join(_SPARK_CHARS[int((v - lo) / rng * (len(_SPARK_CHARS) - 1))] for v in values)


def kline_text(candles: Sequence[Candle], symbol: str, interval: str) -> str:
    closes = [c.close for c in candles]
    first, last = closes[0], closes[-1]
    chg = (last - first) / first * 100 if first else 0
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    return (
        f"{symbol} {interval} 走势（{len(candles)}根）\n"
        f"{sparkline(closes)}\n"
        f"首 {first:.4g} → 末 {last:.4g}（{chg:+.2f}%）· 高 {hi:.4g} 低 {lo:.4g}"
    )
