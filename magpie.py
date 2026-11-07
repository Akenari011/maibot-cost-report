"""magpie 网关的 Gemini 用量查询（可选功能）。

magpie 是一个 OpenAI 兼容的本地反代网关，把请求转给上游订阅账号。
它额外提供几个用量接口，能拿到「今天用了多少 token、吃掉多少订阅配额」。

这个模块只在配置里开启 `report.magpie_enabled` 时才被调用：

- 没装 magpie 的用户完全不受影响（不发请求、卡片上也不会多出区块）
- 地址可配，指向自己的 magpie 实例即可
- 任何一步失败都返回 None，由调用方静默跳过，绝不影响主卡片

接口说明（magpie 0.1.x）：

| 路径 | 内容 |
|---|---|
| `/v1/magpie/usage` | 30 天汇总 + `series` 按天分桶，累加正好等于汇总字段 |
| `/v1/magpie/quotas` | 订阅配额，Gemini 有 7 天 / 5 小时两个窗口，单位是已用百分比 |
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

#: 单次请求超时（秒）。magpie 在本机，正常毫秒级返回。
DEFAULT_TIMEOUT = 5.0


def _fetch_json(url: str, timeout: float) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        # ValueError：地址填错时 urlopen 直接抛「unknown url type」，不能让它冒到上层
        return None


def _get(base_url: str, path: str, timeout: float) -> dict[str, Any] | None:
    return _fetch_json(f"{base_url.rstrip('/')}{path}", timeout)


def _pick_day(usage: dict[str, Any], label: str) -> dict[str, Any]:
    for item in usage.get("series") or []:
        if not isinstance(item, dict):
            continue
        if item.get("label") == label:
            return {
                "calls": int(item.get("calls") or 0),
                "input": int(item.get("input") or 0),
                "output": int(item.get("output") or 0),
                "cache_read": int(item.get("cache_read") or 0),
                "cost": float(item.get("cost") or 0),
            }
    return {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cost": 0.0}


def _parse_quota(quotas: dict[str, Any]) -> dict[str, Any]:
    """解析所有 provider 的所有配额池。

    一个 provider（比如 antigravity）下面可能挂着多个池子：Gemini、Claude & GPT 各自
    有 7 天 / 5 小时两个窗口。这里全部保留，由调用方按「MaiBot 实际调了哪些模型」
    去挑该显示哪几个，不要写死只认 Gemini。
    """
    providers: list[dict[str, Any]] = []
    for item in quotas.get("data") or []:
        if not isinstance(item, dict):
            continue
        pools: dict[str, dict[str, Any]] = {}
        for window in item.get("windows") or []:
            if not isinstance(window, dict):
                continue
            name = str(window.get("name") or "")
            pool = str(window.get("pool") or "").strip()
            if not pool:
                # 老版本没 pool 字段时从 name 里推：`Gemini · 7 days` -> Gemini
                pool = name.split("·")[0].strip() if "·" in name else name
            if not pool:
                continue
            slot = pools.setdefault(
                pool,
                {"quota_7d": None, "quota_5h": None, "reset_7d": None, "reset_5h": None},
            )
            if "7 days" in name or "7 天" in name:
                slot["quota_7d"] = window.get("used")
                slot["reset_7d"] = window.get("resetsAt")
            elif "5 hours" in name or "5 小时" in name:
                slot["quota_5h"] = window.get("used")
                slot["reset_5h"] = window.get("resetsAt")
        if pools:
            providers.append({
                "provider": str(item.get("provider") or ""),
                "plan": str(item.get("plan") or ""),
                "account": str(item.get("user") or ""),
                "pools": pools,
            })
    return {"providers": providers}


def _day_label(now: datetime) -> str:
    """magpie 的 series 用 `Nov 8` 这种英文短日期做标签（不补零）。"""
    return f"{now.strftime('%b')} {now.day}"


def collect(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    """采集一次用量与配额。失败返回 None。

    返回结构：
        {
          "today": {"calls", "input", "output", "cache_read", "cost"},
          "yesterday": 同上或 None,
          "total_30d": {"calls", "input", "output", "cost"},
          "quota_7d": 已用百分比或 None,
          "quota_5h": 已用百分比或 None,
          "plan": "Google AI Pro",
        }
    """
    if not str(base_url or "").strip():
        return None

    usage = _get(base_url, "/v1/magpie/usage", timeout)
    if usage is None:
        return None

    now = datetime.now()
    series = [s for s in (usage.get("series") or []) if isinstance(s, dict)]
    today = _pick_day(usage, _day_label(now))
    yesterday = _pick_day(usage, str(series[-2].get("label"))) if len(series) >= 2 else None

    result: dict[str, Any] = {
        "today": today,
        "yesterday": yesterday,
        "total_30d": {
            "calls": int(usage.get("calls") or 0),
            "input": int(usage.get("input") or 0),
            "output": int(usage.get("output") or 0),
            "cost": float(usage.get("cost") or 0),
        },
        "providers": [],
    }

    quotas = _get(base_url, "/v1/magpie/quotas", timeout)
    if quotas is not None:
        result.update(_parse_quota(quotas))
    return result


def collect_window(
    base_url: str,
    since: datetime,
    timeout: float = DEFAULT_TIMEOUT,
    limit: int = 500,
) -> dict[str, Any] | None:
    """按时间窗口统计 gemini 的账面成本。

    卡片的时间窗口是「最近 N 天」的滚动窗口，而 `/v1/magpie/usage` 的 series 按
    自然日分桶，两者对不上。明细接口 `/v1/magpie/usage/requests` 带精确时间戳，
    按 `since` 过滤才能和卡片的窗口口径一致。

    接口的 limit 上限实测是 500 条（约覆盖 2.6 天），够 24 小时窗口用；
    真超出时会少算，所以返回里带 `truncated` 标记。
    """
    if not str(base_url or "").strip():
        return None

    data = _get(base_url, f"/v1/magpie/usage/requests?limit={int(limit)}", timeout)
    if data is None:
        return None
    rows = [r for r in (data.get("rows") or []) if isinstance(r, dict)]
    if not rows:
        return {"calls": 0, "input": 0, "output": 0, "cost": 0.0,
                "truncated": False, "scanned": 0, "models": [],
                "cost_by_model": {}, "tokens_by_model": {}}

    since_naive = since.replace(tzinfo=None)
    calls = tokens_in = tokens_out = 0
    cost = 0.0
    oldest = None
    seen_models: dict[str, int] = {}
    cost_by_model: dict[str, float] = {}
    tokens_by_model: dict[str, int] = {}
    for row in rows:
        stamp = str(row.get("t") or "")
        try:
            when = datetime.fromisoformat(stamp).replace(tzinfo=None)
        except ValueError:
            continue
        oldest = when if oldest is None or when < oldest else oldest
        if when < since_naive:
            continue
        calls += 1
        tokens_in += int(row.get("in") or 0)
        tokens_out += int(row.get("out") or 0)
        cost += float(row.get("cost") or 0)
        # req 是请求里写的模型名（`antigravity/gemini-3.8-flash`），和 MaiBot 配置对得上
        name = str(row.get("req") or row.get("model") or "").strip()
        if name:
            seen_models[name] = seen_models.get(name, 0) + 1
            # 按模型累计账面成本，供卡片把成本分摊回各个模型
            cost_by_model[name] = round(cost_by_model.get(name, 0.0) + float(row.get("cost") or 0), 6)
            tokens_by_model[name] = tokens_by_model.get(name, 0) + int(row.get("in") or 0) + int(row.get("out") or 0)

    # 扫到的最旧一条仍晚于窗口起点 → 说明 500 条没覆盖满窗口，统计会偏小
    truncated = bool(oldest is not None and oldest > since_naive and len(rows) >= limit)
    return {
        "calls": calls,
        "input": tokens_in,
        "output": tokens_out,
        "cost": round(cost, 6),
        "truncated": truncated,
        "scanned": len(rows),
        "models": sorted(seen_models, key=lambda k: -seen_models[k]),
        "cost_by_model": cost_by_model,
        "tokens_by_model": tokens_by_model,
    }


def _pool_keywords(pool: str) -> list[str]:
    """把池子名拆成可匹配的关键词：`Claude & GPT` -> ['claude', 'gpt']。"""
    text = str(pool or "").lower().replace("&", " ").replace("·", " ").replace("/", " ")
    return [word for word in text.split() if word]


def match_pools(
    models: list[str],
    providers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 MaiBot 实际调用的模型映射到 magpie 的配额池。

    一个池子可能对应多个模型（比如 `Gemini` 池下有 gemini-3.8-flash 等），
    一个模型只归一个池子。匹配靠池子名的关键词：模型名里出现池子名的任一词就算命中。

    返回按「命中模型数」降序排列，每项：
        {provider, plan, pool, quota_7d, quota_5h, models: [命中的模型名]}
    """
    wanted = [str(m).strip() for m in models if str(m).strip()]
    if not wanted:
        return []

    result: list[dict[str, Any]] = []
    for entry in providers or []:
        if not isinstance(entry, dict):
            continue
        pools = entry.get("pools") or {}
        for pool_name, quota in pools.items():
            keywords = _pool_keywords(pool_name)
            if not keywords:
                continue
            hits = [
                model for model in wanted
                if any(word in model.lower() for word in keywords)
            ]
            if not hits:
                continue
            result.append({
                "provider": entry.get("provider", ""),
                "plan": entry.get("plan", ""),
                "pool": str(pool_name),
                "quota_7d": (quota or {}).get("quota_7d"),
                "quota_5h": (quota or {}).get("quota_5h"),
                "reset_7d": (quota or {}).get("reset_7d"),
                "reset_5h": (quota or {}).get("reset_5h"),
                "models": hits,
            })

    result.sort(key=lambda item: (-len(item["models"]), item["pool"]))
    return result


def fetch_pricing(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, dict[str, float]]:
    """拉模型定价表：{model_id: {prompt, input_cache_read, completion}}。

    单位是**美元 / token**（magpie 的 `pricing` 字段就是按 token 给的）。
    有了单价就能拿 MaiBot 自己的完整 token 量算成本，不受明细接口 500 条的限制。
    """
    data = _get(base_url, "/v1/models", timeout)
    if data is None:
        return {}
    out: dict[str, dict[str, float]] = {}
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        pricing = item.get("pricing")
        if not model_id or not isinstance(pricing, dict):
            continue
        try:
            out[model_id] = {
                "prompt": float(pricing.get("prompt") or 0),
                "input_cache_read": float(pricing.get("input_cache_read") or 0),
                "completion": float(pricing.get("completion") or 0),
            }
        except (TypeError, ValueError):
            continue
    return out


#: 美元兑人民币的免费汇率源，按顺序尝试。都失败时由调用方回退到配置值。
FX_SOURCES = (
    "https://open.er-api.com/v6/latest/USD",
    "https://api.exchangerate-api.com/v4/latest/USD",
)


def fetch_usd_cny(timeout: float = DEFAULT_TIMEOUT) -> float | None:
    """抓美元兑人民币汇率。全部源都失败时返回 None。

    不做缓存——调用方负责按自己的节奏缓存，避免这个模块藏状态。
    """
    for url in FX_SOURCES:
        data = _fetch_json(url, timeout)
        if not data:
            continue
        rates = data.get("rates")
        if not isinstance(rates, dict):
            continue
        try:
            rate = float(rates.get("CNY") or 0)
        except (TypeError, ValueError):
            continue
        # 汇率不可能低于 1，明显异常的值直接跳过，免得算出离谱的成本
        if 1.0 < rate < 20.0:
            return round(rate, 6)
    return None
