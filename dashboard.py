"""开销统计看板数据层。

直接读宿主的 SQLite 聚合表，拼出 WebUI 统计页那套指标。

## 为什么要直连数据库

宿主有 7 个 `statistics.local.*` 能力，但都拿不到这张卡片需要的东西：

- `token_distribution` 只给 `total_tokens`，没有输入/输出、缓存命中/未命中的拆分
- `message_trend` 只按聊天流分组，分不出哪些是机器人自己的发言（算不了回复率）
- 没有任何能力暴露"任务失败率"和"环比"

而 `statistics_model_hourly` / `statistics_message_hourly` / `llm_usage` /
`online_time` / `tool_records` 这几张表里数据是全的，所以只能直读。

所有查询都容错：表不存在、字段缺失、值为 None 一律按 0 处理，
不让一行脏数据把整张卡片打崩。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

#: 缓存命中相关字段只存在于 llm_usage 明细表
_CACHE_SQL = """
    SELECT COALESCE(NULLIF(model_assign_name, ''), NULLIF(model_name, ''), 'Unknown') AS model,
           SUM(COALESCE(prompt_cache_hit_tokens, 0)) AS hit,
           SUM(COALESCE(prompt_cache_miss_tokens, 0)) AS miss,
           SUM(COALESCE(prompt_tokens, 0)) AS prompt_total
    FROM llm_usage
    WHERE timestamp >= ? AND timestamp < ?
    GROUP BY model
"""


def _num(value: Any) -> float:
    """把数据库里的值转成 float，坏值按 0 算。"""
    try:
        if value is None:
            return 0.0
        result = float(value)
        return result if result == result and result not in (float("inf"), float("-inf")) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    return int(_num(value))


def align_window(now: datetime, days: int) -> tuple[datetime, datetime]:
    """把统计窗口对齐到整点。

    `statistics_model_hourly` 按整点分桶，`bucket_time >= since` 取到的第一个桶是
    `ceil(since)`。明细表（llm_usage / mai_messages）也按同一个起点算，两边才是
    同一个时间窗口，不然卡片上的花费和缓存命中会差最多 59 分钟。
    """
    start = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return start, now


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5)


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _bot_accounts(con: sqlite3.Connection) -> list[str]:
    """从 bot_platform_accounts 读机器人自己的账号 ID。读不到返回空。"""
    if not _table_exists(con, "bot_platform_accounts"):
        return []
    try:
        rows = con.execute(
            "SELECT account_id FROM bot_platform_accounts WHERE COALESCE(disabled, 0) = 0"
        ).fetchall()
        return [str(r[0]).strip() for r in rows if str(r[0] or "").strip()]
    except Exception:
        return []


def _model_rows(con: sqlite3.Connection, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """模型维度：请求数、输入/输出 token、花费。"""
    if not _table_exists(con, "statistics_model_hourly"):
        return []
    try:
        rows = con.execute(
            """
            SELECT COALESCE(NULLIF(model_name, ''), 'Unknown') AS model,
                   SUM(COALESCE(request_count, 0)),
                   SUM(COALESCE(prompt_tokens, 0)),
                   SUM(COALESCE(completion_tokens, 0)),
                   SUM(COALESCE(total_tokens, 0)),
                   SUM(COALESCE(cost, 0))
            FROM statistics_model_hourly
            WHERE bucket_time >= ? AND bucket_time < ?
            GROUP BY model
            ORDER BY 6 DESC
            """,
            (_fmt(start), _fmt(end)),
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "name": str(r[0]),
            "requests": _int(r[1]),
            "prompt_tokens": _int(r[2]),
            "completion_tokens": _int(r[3]),
            "tokens": _int(r[4]),
            "cost": _num(r[5]),
        }
        for r in rows
    ]


def _cache_rows(con: sqlite3.Connection, start: datetime, end: datetime) -> dict[str, dict[str, int]]:
    """缓存命中/未命中，按模型。"""
    if not _table_exists(con, "llm_usage"):
        return {}
    try:
        rows = con.execute(_CACHE_SQL, (_fmt(start), _fmt(end))).fetchall()
    except Exception:
        return {}
    out: dict[str, dict[str, int]] = {}
    for model, hit, miss, prompt_total in rows:
        hit_i, miss_i = _int(hit), _int(miss)
        out[str(model)] = {
            "hit": hit_i,
            "miss": miss_i,
            # 输入总量里没被缓存字段覆盖的部分（服务端没上报缓存明细）
            "no_cache": max(0, _int(prompt_total) - hit_i - miss_i),
        }
    return out


def _message_count(con: sqlite3.Connection, start: datetime, end: datetime) -> int:
    if not _table_exists(con, "statistics_message_hourly"):
        return 0
    try:
        row = con.execute(
            "SELECT SUM(COALESCE(message_count, 0)) FROM statistics_message_hourly "
            "WHERE bucket_time >= ? AND bucket_time < ?",
            (_fmt(start), _fmt(end)),
        ).fetchone()
        return _int(row[0]) if row else 0
    except Exception:
        return 0


def _reply_count(con: sqlite3.Connection, start: datetime, end: datetime, bots: list[str]) -> int:
    """机器人自己发出的消息条数。"""
    if not bots or not _table_exists(con, "mai_messages"):
        return 0
    placeholders = ",".join("?" for _ in bots)
    try:
        row = con.execute(
            f"SELECT COUNT(*) FROM mai_messages WHERE timestamp >= ? AND timestamp < ? "
            f"AND user_id IN ({placeholders})",
            (_fmt(start), _fmt(end), *bots),
        ).fetchone()
        return _int(row[0]) if row else 0
    except Exception:
        return 0


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        # 老数据可能只有日期部分
        try:
            return datetime.fromisoformat(text[:19]).replace(tzinfo=None)
        except ValueError:
            return None


def _online_minutes(con: sqlite3.Connection, start: datetime, end: datetime) -> int:
    """统计窗口内的在线时长（分钟）。

    ⚠ 不能用 `duration_minutes` 字段：那张表里它恒为 5（字段名写 minutes、
    注释写「单位秒」，实际写进去的值不可信）。宿主自己算在线时长也不用它，
    而是拿 `start_timestamp` / `end_timestamp` 和窗口求交集
    （`statistics_service._apply_summary_activity`）。这里照同样的口径算。
    """
    if not _table_exists(con, "online_time"):
        return 0
    try:
        rows = con.execute(
            "SELECT start_timestamp, end_timestamp FROM online_time "
            "WHERE end_timestamp >= ? OR start_timestamp >= ?",
            (_fmt(start), _fmt(start)),
        ).fetchall()
    except Exception:
        return 0

    total = 0.0
    for raw_start, raw_end in rows:
        seg_start = _parse_dt(raw_start)
        seg_end = _parse_dt(raw_end)
        if seg_start is None:
            continue
        # 还没下线的记录没有 end，按窗口末尾算
        if seg_end is None:
            seg_end = end
        lo = max(seg_start, start)
        hi = min(seg_end, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return int(total / 60)


def _tool_fail_rate(con: sqlite3.Connection, start: datetime, end: datetime) -> tuple[int, int]:
    """(失败数, 总数)。tool_data 是 JSON，只能逐条解析。"""
    if not _table_exists(con, "tool_records"):
        return 0, 0
    try:
        rows = con.execute(
            "SELECT tool_data FROM tool_records WHERE timestamp >= ? AND timestamp < ?",
            (_fmt(start), _fmt(end)),
        ).fetchall()
    except Exception:
        return 0, 0
    total = failed = 0
    for (raw,) in rows:
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        total += 1
        if data.get("success") is False:
            failed += 1
    return failed, total


def _series_prefix(days: int) -> int:
    """时间序列的聚合粒度：短窗口按小时，长窗口按天。

    迷你柱状图只有 ~200px 宽，点太多会挤成一片看不出趋势。
    1 天给 24 个点刚好；7 天按小时是 165 个点、30 天是 456 个点，必须降到按天。
    """
    return 13 if days <= 2 else 10


def _hourly_series(con: sqlite3.Connection, start: datetime, days: int = 1) -> list[dict[str, Any]]:
    """时间序列，给迷你柱状图用。粒度随窗口长度变化。"""
    if not _table_exists(con, "statistics_model_hourly"):
        return []
    prefix = _series_prefix(days)
    try:
        rows = con.execute(
            f"""
            SELECT substr(bucket_time, 1, {prefix}) AS bucket,
                   SUM(COALESCE(cost, 0)),
                   SUM(COALESCE(request_count, 0)),
                   SUM(COALESCE(total_tokens, 0))
            FROM statistics_model_hourly
            WHERE bucket_time >= ?
            GROUP BY bucket
            ORDER BY bucket
            """,
            (_fmt(start),),
        ).fetchall()
    except Exception:
        return []
    return [
        {"bucket": str(r[0]), "cost": _num(r[1]), "requests": _int(r[2]), "tokens": _int(r[3])}
        for r in rows
    ]


def _message_hourly(con: sqlite3.Connection, start: datetime, days: int = 1) -> dict[str, int]:
    if not _table_exists(con, "statistics_message_hourly"):
        return {}
    prefix = _series_prefix(days)
    try:
        rows = con.execute(
            f"SELECT substr(bucket_time, 1, {prefix}) AS bucket, SUM(COALESCE(message_count, 0)) "
            "FROM statistics_message_hourly WHERE bucket_time >= ? GROUP BY bucket",
            (_fmt(start),),
        ).fetchall()
    except Exception:
        return {}
    return {str(r[0]): _int(r[1]) for r in rows}


def _period_metrics(
    con: sqlite3.Connection,
    start: datetime,
    end: datetime,
    bots: list[str],
    days: int,
) -> dict[str, Any]:
    """一个统计周期的全部汇总指标。"""
    models = _model_rows(con, start, end)
    cache = _cache_rows(con, start, end)

    total_cost = sum(m["cost"] for m in models)
    total_requests = sum(m["requests"] for m in models)
    total_tokens = sum(m["tokens"] for m in models)
    input_tokens = sum(m["prompt_tokens"] for m in models)
    output_tokens = sum(m["completion_tokens"] for m in models)

    hit = sum(c["hit"] for c in cache.values())
    miss = sum(c["miss"] for c in cache.values())
    no_cache = sum(c["no_cache"] for c in cache.values())

    messages = _message_count(con, start, end)
    replies = _reply_count(con, start, end, bots)
    online = _online_minutes(con, start, end)
    failed, tool_total = _tool_fail_rate(con, start, end)

    # 宿主算「花费/时间」用的是真实在线小时数，不是窗口长度；在线时长为 0 时退回窗口长度
    span_hours = max(1.0, days * 24.0)
    online_hours = online / 60.0 if online > 0 else span_hours
    return {
        "total_cost": total_cost,
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit": hit,
        "cache_miss": miss,
        "cache_no_info": no_cache,
        "cache_rate": (hit / (hit + miss) * 100) if (hit + miss) else 0.0,
        "total_messages": messages,
        "total_replies": replies,
        "reply_rate": (replies / messages * 100) if messages else 0.0,
        "online_minutes": online,
        "cost_per_hour": total_cost / online_hours,
        "tokens_per_hour": total_tokens / online_hours,
        "cost_per_message": (total_cost / messages) if messages else 0.0,
        "cost_per_reply": (total_cost / replies) if replies else 0.0,
        "calls_per_reply": (total_requests / replies) if replies else 0.0,
        "tool_fail_rate": (failed / tool_total * 100) if tool_total else 0.0,
        "models": models,
        "cache_by_model": cache,
    }




def _apply_extra_by_model(metrics: dict[str, Any], extra_by_model: dict[str, float]) -> float:
    """把外部折算成本按模型分摊，返回总增量。

    宿主表里 gemini 这类走订阅的模型 cost 记 0（价目表没配价），成本只在网关侧有。
    如果只把总额加上去、不分摊到模型，卡片上就会出现「总额含它、模型分布却是 0%」
    的对不上。这里按模型名精确落位，名字对不上时补一条新记录，保证两边一致。
    """
    if not extra_by_model:
        return 0.0
    by_name = {m["name"]: m for m in metrics.get("models") or []}
    added = 0.0
    for name, amount in extra_by_model.items():
        amount = float(amount or 0)
        if amount <= 0:
            continue
        model = by_name.get(name)
        if model is None:
            model = {
                "name": name, "requests": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "tokens": 0, "cost": 0.0,
            }
            metrics.setdefault("models", []).append(model)
            by_name[name] = model
        model["cost"] = round(model["cost"] + amount, 6)
        model["extra_cost"] = round(model.get("extra_cost", 0.0) + amount, 6)
        added += amount
    metrics["models"].sort(key=lambda m: -m["cost"])
    return round(added, 6)


def _recompute_derived(metrics: dict[str, Any], days: int) -> None:
    """往 total_cost 里加了额外成本后，把派生指标一起重算，避免口径打架。"""
    cost = metrics["total_cost"]
    span_hours = max(1.0, days * 24.0)
    online = metrics.get("online_minutes") or 0
    online_hours = online / 60.0 if online > 0 else span_hours
    messages = metrics["total_messages"]
    replies = metrics["total_replies"]
    requests = metrics["total_requests"]
    metrics["cost_per_hour"] = cost / online_hours
    metrics["cost_per_message"] = (cost / messages) if messages else 0.0
    metrics["cost_per_reply"] = (cost / replies) if replies else 0.0
    metrics["calls_per_reply"] = (requests / replies) if replies else 0.0


def collect(
    db_path: Path,
    days: int,
    now: datetime | None = None,
    extra_by_model: dict[str, float] | None = None,
    prev_extra_by_model: dict[str, float] | None = None,
    pricing: dict[str, dict[str, float]] | None = None,
    usd_rate: float = 7.2,
    apply_pricing: bool = True,
) -> dict[str, Any] | None:
    """采集一个完整周期的看板数据。数据库打不开返回 None。

    两种方式把「不按 token 计费、但按公开价折算」的开销并进花费：

    - `pricing` + `usd_rate`：网关给了单价（美元/token）时用这个，插件拿 MaiBot 自己的
      完整 token 量去算，不受网关明细接口条数限制，长窗口也准。这是推荐路径。
    - `extra_by_model` / `prev_extra_by_model`：直接给现成的按模型成本，测试或特殊场景用。

    都是按模型分摊而不是只加总额，这样「模型花费分布」和总花费才对得上。
    """
    if not db_path.is_file():
        return None
    now = now or datetime.now()
    start, end = align_window(now, days)
    prev_start, prev_end = align_window(now - timedelta(days=days), days)

    try:
        con = _connect(db_path)
    except Exception:
        return None
    try:
        bots = _bot_accounts(con)
        current = _period_metrics(con, start, end, bots, days)
        previous = _period_metrics(con, prev_start, prev_end, bots, days)
        series = _hourly_series(con, start, days)
        msg_series = _message_hourly(con, start, days)
        # 单价计算必须在 con 关闭之前：conn.close() 之后再查会抛异常，
        # 异常被外层吞掉就表现为「传了 pricing 但成本没变」。
        # 实际调用过的模型：不管计不计入成本都要有，用来决定卡片展示哪些配额池
        current["used_models"] = used_models(con, start, end)
        if pricing:
            usage_detail = priced_usage(con, start, end, pricing, usd_rate)
            # 供卡片底部展示「本窗口调用 / token / 成本」
            current["priced_usage"] = usage_detail
            # apply_pricing 关掉时只展示不并入总花费
            if apply_pricing:
                extra_by_model = {
                    **(extra_by_model or {}),
                    **{name: item["cost"] for name, item in usage_detail.items()},
                }
            prev_extra_by_model = {
                **(prev_extra_by_model or {}),
                **priced_cost(con, prev_start, prev_end, pricing, usd_rate),
            }
    finally:
        con.close()

    # 消息序列补进时间序列，缺失的桶按 0
    for point in series:
        point["messages"] = msg_series.get(point["bucket"], 0)

    # 把外部折算成本按模型分摊，再重算派生指标；两个周期都加，环比才有可比性
    if extra_by_model:
        added = _apply_extra_by_model(current, extra_by_model)
        if added:
            current["total_cost"] = round(current["total_cost"] + added, 6)
            current["extra_cost"] = added
            _recompute_derived(current, days)
    if prev_extra_by_model:
        added_prev = _apply_extra_by_model(previous, prev_extra_by_model)
        if added_prev:
            previous["total_cost"] = round(previous["total_cost"] + added_prev, 6)
            previous["extra_cost"] = added_prev
            _recompute_derived(previous, days)

    return {
        "days": days,
        "start": start,
        "end": end,
        "current": current,
        "previous": previous,
        "series": series,
        "has_cache": bool(current["cache_hit"] or current["cache_miss"]),
    }


def priced_cost(
    con: sqlite3.Connection,
    start: datetime,
    end: datetime,
    pricing: dict[str, dict[str, float]],
    usd_rate: float = 7.2,
) -> dict[str, float]:
    """用网关给的单价 × MaiBot 自己的 token 量算成本，返回 {模型: 人民币}。

    为什么不直接用网关的明细成本：`/v1/magpie/usage/requests` 只返回最近 500 条
    （约 2.6 天），窗口一长就截断。而 `llm_usage` 里有完整历史，token 量准确。

    `pricing` 来自 `/v1/models`，单位是**美元 / token**；乘 `usd_rate` 换成人民币，
    才能和宿主记的其它模型花费（人民币）相加。
    """
    if not pricing or not _table_exists(con, "llm_usage"):
        return {}
    try:
        rows = con.execute(
            """
            SELECT COALESCE(NULLIF(model_assign_name, ''), NULLIF(model_name, ''), 'Unknown') AS model,
                   SUM(COALESCE(prompt_tokens, 0)),
                   SUM(COALESCE(prompt_cache_hit_tokens, 0)),
                   SUM(COALESCE(completion_tokens, 0))
            FROM llm_usage
            WHERE timestamp >= ? AND timestamp < ?
            GROUP BY model
            """,
            (_fmt(start), _fmt(end)),
        ).fetchall()
    except Exception:
        return {}

    out: dict[str, float] = {}
    for name, prompt_tokens, hit, completion in rows:
        price = pricing.get(str(name))
        if not price:
            continue
        prompt_tokens, hit, completion = _int(prompt_tokens), _int(hit), _int(completion)
        # 未命中 = 输入总量 - 命中量：比直接用 prompt_cache_miss_tokens 稳，
        # 部分请求服务端没上报缓存字段，那些 token 实际按全价计
        miss = max(0, prompt_tokens - hit)
        usd = (
            hit * float(price.get("input_cache_read") or 0)
            + miss * float(price.get("prompt") or 0)
            + completion * float(price.get("completion") or 0)
        )
        if usd > 0:
            out[str(name)] = round(usd * float(usd_rate), 6)
    return out


def priced_usage(
    con: sqlite3.Connection,
    start: datetime,
    end: datetime,
    pricing: dict[str, dict[str, float]],
    usd_rate: float = 7.2,
) -> dict[str, dict[str, Any]]:
    """按模型统计窗口内的调用次数、token 量和折算成本（人民币）。

    和 `priced_cost` 的区别：这里除了成本还给出次数和 token，供卡片底部展示。
    数据同样来自 `llm_usage`（完整历史），所以不受网关明细 500 条的限制。
    """
    if not pricing or not _table_exists(con, "llm_usage"):
        return {}
    try:
        rows = con.execute(
            """
            SELECT COALESCE(NULLIF(model_assign_name, ''), NULLIF(model_name, ''), 'Unknown') AS model,
                   COUNT(*),
                   SUM(COALESCE(prompt_tokens, 0)),
                   SUM(COALESCE(prompt_cache_hit_tokens, 0)),
                   SUM(COALESCE(completion_tokens, 0))
            FROM llm_usage
            WHERE timestamp >= ? AND timestamp < ?
            GROUP BY model
            """,
            (_fmt(start), _fmt(end)),
        ).fetchall()
    except Exception:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for name, calls, prompt_tokens, hit, completion in rows:
        price = pricing.get(str(name))
        if not price:
            continue
        prompt_tokens, hit, completion = _int(prompt_tokens), _int(hit), _int(completion)
        miss = max(0, prompt_tokens - hit)
        usd = (
            hit * float(price.get("input_cache_read") or 0)
            + miss * float(price.get("prompt") or 0)
            + completion * float(price.get("completion") or 0)
        )
        out[str(name)] = {
            "calls": _int(calls),
            "input": prompt_tokens,
            "output": completion,
            "cost": round(usd * float(usd_rate), 6),
        }
    return out


def used_models(con: sqlite3.Connection, start: datetime, end: datetime) -> list[str]:
    """窗口内 MaiBot 实际调用过的模型名（取 model_identifier，和网关的模型 id 对齐）。

    和 `priced_cost` 不同，这个不依赖定价表——即使不计入账面成本，
    也要能判断该在卡片上展示哪些模型，不能退回网关的请求明细
    （那里混着别的 agent 和 magpie 自己探测 provider 的记录）。
    """
    if not _table_exists(con, "llm_usage"):
        return []
    try:
        rows = con.execute(
            """
            SELECT DISTINCT COALESCE(NULLIF(model_name, ''), NULLIF(model_assign_name, ''), 'Unknown')
            FROM llm_usage
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (_fmt(start), _fmt(end)),
        ).fetchall()
    except Exception:
        return []
    return sorted(str(r[0]) for r in rows if r and r[0])
