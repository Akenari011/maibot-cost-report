"""开销统计看板渲染层。

把 `dashboard.collect()` 采到的数据画成一张卡片，布局对齐宿主 WebUI 的统计概览页：

    统计概览（4 个大数字 + 迷你柱状图） │ Prompt 缓存 │ 模型花费分布
    Token 流向拆解                      │ 单位成本与效率（带环比）

纯 HTML + 内联 CSS，交给宿主的 `render.html2png` 出图，不依赖第三方库。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 配色取自 WebUI 统计页
ORANGE = "#E8730C"
GREEN = "#10B981"
BLUE = "#3B82F6"
YELLOW = "#F59E0B"
PINK = "#EC4899"
GRAY = "#6B7280"
LIGHT = "#9CA3AF"
UP_BAD = "#DC2626"
DOWN_GOOD = "#059669"

SERIES_COLORS = [ORANGE, GREEN, BLUE, YELLOW, PINK, "#8B5CF6", "#14B8A6", "#F43F5E"]


def esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def fmt_wan(value: float) -> str:
    """按中文习惯折算成 万 / 亿。"""
    if value >= 1e8:
        return f"{value / 1e8:.2f}亿"
    if value >= 1e4:
        return f"{value / 1e4:.2f}万"
    if value >= 1000:
        return f"{value:,.0f}"
    return f"{value:.0f}"


def fmt_money(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.2f}"
    return f"{value:.4f}"


def fmt_pct(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}%"


def fmt_delta(current: float, previous: float) -> tuple[str, str]:
    """返回 (文本, 颜色)。上涨标红、下跌标绿——花费类指标涨了不是好事。"""
    if previous <= 0:
        return "", LIGHT
    delta = (current - previous) / previous * 100
    if abs(delta) < 0.05:
        return "0.0%", LIGHT
    text = f"{delta:+.1f}%"
    return text, (UP_BAD if delta > 0 else DOWN_GOOD)


def _delta_badge(current: float, previous: float) -> str:
    text, color = fmt_delta(current, previous)
    if not text:
        return ""
    return (
        f"<span style='display:inline-block;padding:1px 5px;border:1px solid {color};"
        f"border-radius:4px;color:{color};font-size:11px;line-height:1.4'>{text}</span>"
    )


def _sparkline(points: list[dict[str, Any]], key: str, color: str) -> str:
    """迷你柱状图。点数不足时留空，避免出现一根孤零零的柱子。"""
    values = [max(0.0, float(p.get(key) or 0)) for p in points]
    if len(values) < 2 or max(values) <= 0:
        return "<div style='min-height:38px'></div>"
    peak = max(values)
    bars = []
    for v in values:
        # 按容器高度的百分比算，卡片被拉高时柱状图跟着长，不留大片空白
        height = max(6.0, v / peak * 100.0)
        bars.append(
            f"<i style='flex:1;height:{height:.1f}%;background:{color};border-radius:1px;opacity:.85'></i>"
        )
    return (
        "<div style='display:flex;align-items:flex-end;gap:2px;flex:1;min-height:38px;margin-top:8px'>"
        + "".join(bars)
        + "</div>"
    )


def _big_card(
    title: str,
    value: str,
    badge: str,
    line1: tuple[str, str],
    line2: tuple[str, str],
    spark: str,
) -> str:
    def row(label: str, val: str) -> str:
        return (
            f"<div style='display:flex;justify-content:space-between;font-size:11px;"
            f"color:{GRAY};margin-top:3px'><span>{esc(label)}</span>"
            f"<span style='color:#111827;font-variant-numeric:tabular-nums'>{esc(val)}</span></div>"
        )

    return f"""
    <div style="flex:1;min-width:0;display:flex;flex-direction:column">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
        <span style="font-size:12px;color:{GRAY};white-space:nowrap">{esc(title)}</span>{badge}
      </div>
      <div style="font-size:26px;font-weight:700;letter-spacing:-.5px;line-height:1.15;white-space:nowrap">{value}</div>
      {row(line1[0], line1[1])}
      {row(line2[0], line2[1])}
      <div style="flex:1;display:flex;flex-direction:column;justify-content:flex-end">{spark}</div>
    </div>
    """


def _progress_row(label: str, percent: float, color: str, right: str = "") -> str:
    width = max(0.0, min(100.0, percent))
    return f"""
    <div style="margin-bottom:11px">
      <div style="display:flex;align-items:baseline;gap:10px;font-size:12px;margin-bottom:6px">
        <span style="flex:1;min-width:0;color:#111827;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{esc(label)}</span>
        <span style="flex:none;color:{GRAY};font-variant-numeric:tabular-nums">{esc(right or fmt_pct(percent))}</span>
      </div>
      <div style="height:8px;background:#F1F5F9;border-radius:999px;overflow:hidden">
        <div style="width:{width:.1f}%;height:100%;background:{color};border-radius:999px"></div>
      </div>
    </div>
    """


def _cache_block(data: dict[str, Any], limit: int = 8) -> str:
    """Prompt 缓存区块。

    最多只画 4 行：这个区块和「统计概览」同一行，行数多了会把整行撑高，
    左侧统计概览的迷你柱状图被 stretch 拉得老长，反而难看。
    """
    current = data["current"]
    cache = current["cache_by_model"]
    if not data["has_cache"]:
        body = f"<div style='font-size:12px;color:{LIGHT};padding:12px 0'>读不到缓存明细，检查「数据库路径」配置</div>"
    else:
        rows = []
        hidden = 0
        for model in current["models"]:
            stats = cache.get(model["name"])
            if not stats:
                continue
            denom = stats["hit"] + stats["miss"]
            if denom <= 0:
                continue
            # 只画前 limit 个：30 天窗口可能有二十多个模型，全列出来会把整行撑爆，
            # 同行的「统计概览」被 stretch 拉高后大片留白，反而更难看。
            if len(rows) >= max(1, int(limit)):
                hidden += 1
                continue
            rate = stats["hit"] / denom * 100
            color = GREEN if rate >= 60 else (YELLOW if rate >= 30 else "#DC2626")
            rows.append(_progress_row(model["name"], rate, color))
        total_rate = current["cache_rate"]
        head = f"""
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px">
          <span style="font-size:12px;color:{GRAY}">全部</span>
          <span style="font-size:24px;font-weight:700">{fmt_pct(total_rate)}</span>
        </div>
        <div style="height:8px;background:#F1F5F9;border-radius:999px;overflow:hidden;margin-bottom:16px">
          <div style="width:{max(0.0, min(100.0, total_rate)):.1f}%;height:100%;background:{ORANGE};border-radius:999px"></div>
        </div>
        """
        body = head + "".join(rows)
        if hidden:
            body += (
                f"<div style='font-size:11px;color:{LIGHT};margin-top:-4px;margin-bottom:10px'>"
                f"另有 {hidden} 个模型未列出</div>"
            )
        if current["cache_no_info"] > 0:
            body += (
                f"<div style='font-size:11px;color:{LIGHT};margin-top:2px'>"
                f"另有 {fmt_wan(current['cache_no_info'])} 输入 Token 未上报缓存明细</div>"
            )
    return f"""
    <div class="card" style="flex:1">
      <div style="font-size:13px;font-weight:600;margin-bottom:12px">Prompt缓存</div>
      {body}
    </div>
    """


def _model_dist_block(data: dict[str, Any]) -> str:
    current = data["current"]
    models = current["models"]
    total = current["total_cost"]
    if not models or total <= 0:
        body = f"<div style='font-size:12px;color:{LIGHT};padding:12px 0'>这个窗口内没有模型花费</div>"
        legend = ""
    else:
        segments, legend_rows, zero_count = [], [], 0
        for index, model in enumerate(models):
            share = model["cost"] / total * 100
            color = SERIES_COLORS[index % len(SERIES_COLORS)]
            if share <= 0.02:
                zero_count += 1
                continue
            segments.append(
                f"<div style='width:{share:.2f}%;background:{color};height:100%' title=\"{esc(model['name'])}\"></div>"
            )
            legend_rows.append(
                f"""
                <div style="display:flex;align-items:center;gap:6px;font-size:12px;margin-top:7px">
                  <i style="width:9px;height:9px;background:{color};border-radius:2px;flex:none"></i>
                  <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{esc(model['name'])}</span>
                  <span style="color:{GRAY};font-variant-numeric:tabular-nums">{fmt_pct(share)}</span>
                </div>
                """
            )
        body = (
            "<div style='display:flex;height:12px;border-radius:3px;overflow:hidden;gap:1px'>"
            + "".join(segments)
            + "</div>"
        )
        legend = "".join(legend_rows)
        if zero_count:
            legend += (
                f"<div style='font-size:11px;color:{LIGHT};margin-top:10px'>"
                f"另有 {zero_count} 个模型花费为 ¥0.00</div>"
            )
    return f"""
    <div class="card" style="flex:1">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px">
        <span style="font-size:13px;font-weight:600">模型花费分布</span>
        <span style="font-size:11px;color:{LIGHT}">查看明细 —</span>
      </div>
      {body}
      {legend}
    </div>
    """


def _token_flow_block(data: dict[str, Any]) -> str:
    current = data["current"]
    hit = current["cache_hit"]
    miss = current["cache_miss"]
    no_info = current["cache_no_info"]
    output = current["output_tokens"]
    total = hit + miss + no_info + output
    if total <= 0:
        return """
        <div class="card" style="flex:1">
          <div style="font-size:13px;font-weight:600;margin-bottom:12px">Token流向拆解</div>
          <div style="font-size:12px;color:#9CA3AF;padding:12px 0">这个窗口内没有 Token 记录</div>
        </div>
        """

    parts = [
        ("缓存命中输入", hit, ORANGE),
        ("缓存未命中输入", miss, BLUE),
        ("无缓存信息输入", no_info, PINK),
        ("输出 Token", output, GREEN),
    ]
    segments, legend = [], []
    for name, value, color in parts:
        share = value / total * 100
        if share > 0:
            segments.append(f"<div style='width:{share:.2f}%;background:{color};height:100%'></div>")
        legend.append(
            f"""
            <div style="display:flex;align-items:center;gap:6px;font-size:12px;min-width:0">
              <i style="width:9px;height:9px;background:{color};border-radius:2px;flex:none"></i>
              <span style="color:{GRAY};white-space:nowrap">{esc(name)}</span>
              <span style="margin-left:auto;font-variant-numeric:tabular-nums;white-space:nowrap">{fmt_wan(value)}</span>
              <span style="color:{LIGHT};font-variant-numeric:tabular-nums;width:48px;text-align:right;flex:none">{fmt_pct(share)}</span>
            </div>
            """
        )
    input_total = hit + miss + no_info
    ratio = (input_total / output) if output else 0.0
    return f"""
    <div class="card" style="flex:1">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px">
        <span style="font-size:13px;font-weight:600">Token流向拆解</span>
        <span style="font-size:11px;color:{LIGHT}">总计 {fmt_wan(total)}</span>
      </div>
      <div style="display:flex;height:26px;border-radius:4px;overflow:hidden;gap:1px">
        {''.join(segments)}
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:9px 24px;margin-top:12px">{''.join(legend)}</div>
      <div style="display:flex;justify-content:space-between;font-size:12px;color:{GRAY};margin-top:14px">
        <span>输入 : 输出 比</span>
        <span style="color:#111827;font-variant-numeric:tabular-nums">{ratio:.1f} : 1</span>
      </div>
    </div>
    """


def _efficiency_block(data: dict[str, Any]) -> str:
    current = data["current"]
    # previous 可能缺失（首轮采集或数据库刚建），缺失时环比一律按 0 处理
    previous = data.get("previous") or {}
    rows = [
        ("花费/消息数量", f"¥{fmt_money(current['cost_per_message'])}", current["cost_per_message"], previous.get("cost_per_message", 0.0)),
        ("花费/回复数量", f"¥{fmt_money(current['cost_per_reply'])}", current["cost_per_reply"], previous.get("cost_per_reply", 0.0)),
        ("花费/时间", f"¥{fmt_money(current['cost_per_hour'])}", current["cost_per_hour"], previous.get("cost_per_hour", 0.0)),
        ("Token/时间", fmt_wan(current["tokens_per_hour"]), current["tokens_per_hour"], previous.get("tokens_per_hour", 0.0)),
        ("单回复调用", f"{current['calls_per_reply']:.1f}", current["calls_per_reply"], previous.get("calls_per_reply", 0.0)),
        ("回复率", fmt_pct(current["reply_rate"]), current["reply_rate"], previous.get("reply_rate", 0.0)),
    ]
    body = []
    for label, value, cur, prev in rows:
        body.append(
            f"""
            <div style="display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid #F1F5F9">
              <span style="flex:1;min-width:0;font-size:12px;color:{GRAY};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{esc(label)}</span>
              <span style="flex:none;width:104px;text-align:right;font-size:14px;font-weight:600;font-variant-numeric:tabular-nums">{esc(value)}</span>
              <span style="flex:none;width:64px;text-align:right">{_delta_badge(cur, prev)}</span>
            </div>
            """
        )
    return f"""
    <div class="card" style="grid-column:2 / 4">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
        <span style="font-size:13px;font-weight:600">单位成本与效率</span>
        <span style="font-size:11px;color:{LIGHT}">相比上一等长周期</span>
      </div>
      {''.join(body)}
    </div>
    """


def _online_text(minutes: int) -> str:
    if minutes <= 0:
        return "本次启动后暂无在线记录"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if mins or not parts:
        parts.append(f"{mins}分钟")
    return " ".join(parts)



def _reset_hint(iso: Any) -> str:
    """把配额的 ISO 重置时间转成「X 天后重置」这种相对说法。"""
    text = str(iso or "").strip()
    if not text:
        return ""
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    seconds = (when - datetime.now().astimezone()).total_seconds()
    if seconds <= 0:
        return "即将重置"
    hours = seconds / 3600
    if hours >= 24:
        return f"{int(hours // 24)} 天后重置"
    if hours >= 1:
        return f"{int(hours)} 小时后重置"
    return f"{max(1, int(seconds // 60))} 分钟后重置"


def _magpie_block(magpie: dict[str, Any]) -> str:
    """magpie 网关的订阅用量。只展示 MaiBot 实际调过的模型对应的配额池。"""
    # 优先用和卡片窗口一致的统计；拿不到才退回自然日
    stats = magpie.get("window_stats") or magpie.get("today") or {}
    yesterday = magpie.get("yesterday") or {}
    matched = magpie.get("matched") or []
    window = magpie.get("window") or {}

    calls = int(stats.get("calls") or 0)
    tokens_in = int(stats.get("input") or 0)
    tokens_out = int(stats.get("output") or 0)
    # window_stats 的 cost 已经是人民币（按单价算的）；today 的 cost 是美元，要换算
    if magpie.get("window_stats"):
        cost = float(stats.get("cost") or 0)
    else:
        usd_rate = float(magpie.get("usd_rate") or 7.2)
        cost = float(stats.get("cost") or 0) * usd_rate

    def quota_bar(label: str, used: float | None, color: str, reset: Any = None) -> str:
        hint = _reset_hint(reset)
        hint_html = f"<span style='color:{LIGHT};margin-left:6px'>· {hint}</span>" if hint else ""
        if used is None:
            return f"""
            <div style="flex:1;min-width:0">
              <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:5px">
                <span style="color:{GRAY}">{esc(label)}</span>
                <span style="color:{LIGHT}">—</span>
              </div>
              <div style="height:8px;background:#F1F5F9;border-radius:999px"></div>
            </div>
            """
        used = max(0.0, min(100.0, float(used)))
        return f"""
        <div style="flex:1;min-width:0">
          <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:5px">
            <span style="color:{GRAY}">{esc(label)}</span>
            <span style="color:#111827;font-variant-numeric:tabular-nums">
              已用 {used:.2f}% · 剩 {100 - used:.2f}%{hint_html}
            </span>
          </div>
          <div style="height:8px;background:#F1F5F9;border-radius:999px;overflow:hidden">
            <div style="width:{used:.2f}%;height:100%;background:{color};border-radius:999px"></div>
          </div>
        </div>
        """

    # 标题：优先用实际模型名，没有就退回池子名
    plan = next((str(m.get("plan") or "") for m in matched if m.get("plan")), "")
    model_names = []
    for item in matched:
        for name in item.get("models") or []:
            short = str(name).split("/")[-1]
            if short and short not in model_names:
                model_names.append(short)
    heading = "、".join(model_names) if model_names else "订阅用量"
    heading = f"{heading} · {plan}" if plan else heading

    # 配额条：每个命中的池子一行（7 天 + 5 小时并排）
    quota_rows = []
    for item in matched:
        pool = str(item.get("pool") or "")
        quota_rows.append(f"""
        <div style="display:flex;gap:20px;margin-top:{'0' if not quota_rows else '10px'}">
          {quota_bar(f'{pool} · 7 天配额', item.get('quota_7d'), GREEN, item.get('reset_7d'))}
          {quota_bar(f'{pool} · 5 小时配额', item.get('quota_5h'), BLUE, item.get('reset_5h'))}
        </div>
        """)
    if not quota_rows:
        quota_rows.append(f"""
        <div style="font-size:12px;color:{LIGHT};padding:6px 0">
          本窗口内没有走 magpie 的模型调用，或配额接口没返回对应池子
        </div>
        """)

    delta = ""
    if yesterday.get("calls"):
        ratio = calls / yesterday["calls"]
        delta = f"<span style='color:{LIGHT};font-size:11px;margin-left:8px'>昨日 {yesterday['calls']} 次（{ratio:.2f}×）</span>"

    counted = magpie.get("count_cost", True)
    note = "来自 magpie 网关 · 账面已计入总花费" if counted else "来自 magpie 网关 · 账面未计入总花费"

    # 不再提示「明细超过 500 条上限」：总花费改用单价 × token 量算，不受明细条数限制，
    # 留着这行只会让人以为整张卡片都不准。
    return f"""
    <div class="card" style="grid-column:1 / -1">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px">
        <span style="font-size:13px;font-weight:600">{esc(heading)}</span>
        <span style="font-size:11px;color:{LIGHT}">{note}</span>
      </div>
      <div style="display:flex;gap:26px;align-items:flex-start">
        <div style="min-width:180px">
          <div style="font-size:11px;color:{GRAY};margin-bottom:4px">本窗口调用</div>
          <div style="font-size:24px;font-weight:700;line-height:1.1">{calls}<span style="font-size:13px;font-weight:500;color:{GRAY};margin-left:3px">次</span>{delta}</div>
          <div style="display:flex;gap:16px;margin-top:8px;font-size:11px;color:{GRAY}">
            <span>输入 <b style="color:#111827">{fmt_wan(tokens_in)}</b></span>
            <span>输出 <b style="color:#111827">{fmt_wan(tokens_out)}</b></span>
            <span>账面 <b style="color:#111827">¥{fmt_money(cost)}</b></span>
          </div>
        </div>
        <div style="flex:1;min-width:0">{''.join(quota_rows)}</div>
      </div>
    </div>
    """


def build(data: dict[str, Any], cfg: Any) -> str:
    """生成完整卡片 HTML。"""
    current = data["current"]
    previous = data.get("previous") or {}
    series = data.get("series") or []
    unit = esc(getattr(cfg, "currency", "元") or "")
    days = data["days"]
    label = "最近 24 小时" if days == 1 else f"最近 {days} 天"
    title = str(getattr(cfg, "title", "") or "").strip()
    heading = f"{title} · {label}" if title else label

    cards = [
        _big_card(
            "总花费",
            f"¥{fmt_money(current['total_cost'])}",
            _delta_badge(current["total_cost"], previous.get("total_cost", 0.0)),
            ("花费/时间", f"¥{fmt_money(current['cost_per_hour'])}"),
            ("单回复调用", f"{current['calls_per_reply']:.1f}"),
            _sparkline(series, "cost", ORANGE),
        ),
        _big_card(
            "总请求数",
            fmt_wan(current["total_requests"]),
            _delta_badge(current["total_requests"], previous.get("total_requests", 0.0)),
            ("任务失败率", fmt_pct(current["tool_fail_rate"])),
            ("缓存命中", fmt_pct(current["cache_rate"])),
            _sparkline(series, "requests", ORANGE),
        ),
        _big_card(
            "总Token数",
            fmt_wan(current["total_tokens"]),
            _delta_badge(current["total_tokens"], previous.get("total_tokens", 0.0)),
            ("总输入Token", fmt_wan(current["input_tokens"])),
            ("总输出Token", fmt_wan(current["output_tokens"])),
            _sparkline(series, "tokens", ORANGE),
        ),
        _big_card(
            "总消息数",
            fmt_wan(current["total_messages"]),
            _delta_badge(current["total_messages"], previous.get("total_messages", 0.0)),
            ("总回复数", f"{current['total_replies']:,}"),
            ("回复率", fmt_pct(current["reply_rate"])),
            _sparkline(series, "messages", ORANGE),
        ),
    ]

    magpie = data.get("magpie")
    magpie_row = _magpie_block(magpie) if magpie else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 18px;
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", system-ui, sans-serif;
    background: #F1F5F9; color: #0F172A;
  }}
  .card {{
    background: #fff; border-radius: 14px; padding: 16px 18px;
    box-shadow: 0 1px 3px rgba(15,23,42,.06), 0 1px 2px rgba(15,23,42,.04);
  }}
</style></head>
<body>
  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px;padding:0 4px">
    <span style="font-size:15px;font-weight:700">{esc(heading)}</span>
    <span style="font-size:11px;color:{LIGHT}">总在线时间 {esc(_online_text(current['online_minutes']))}</span>
  </div>

  <div style="display:grid;grid-template-columns:2fr 1fr 1fr;gap:14px;align-items:stretch">
    <div class="card" style="display:flex;flex-direction:column">
      <div style="font-size:13px;font-weight:600;margin-bottom:14px">统计概览</div>
      <div style="display:flex;gap:22px;flex:1;min-height:0">{''.join(cards)}</div>
    </div>
    {_cache_block(data, min(getattr(cfg, 'top_models', 8), 4))}
    {_model_dist_block(data)}
    {_token_flow_block(data)}
    {_efficiency_block(data)}
    {magpie_row}
  </div>
</body></html>"""
