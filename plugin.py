"""查询麦麦的 token 消耗和花费。

数据来自宿主的 `statistics.local.models` 能力，底层读 `statistics_model_hourly`
聚合表（按小时预聚合，不需要扫 `llm_usage` 明细）。

## 为什么默认是「最近 24 小时」而不是「今天 0 点」

宿主的统计窗口按 `now - N 天` 计算（`data.py:926`），没有自然日边界，
所以 `days=1` 得到的是滚动 24 小时。凌晨查的时候会带上昨天的一部分，
但白天基本等于今天。要精确到自然日只能自己扫 `llm_usage`，代价大得多，
这里不值得。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from maibot_sdk import (
    Command,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType


def _load_local(name: str) -> Any:
    """按文件路径加载同目录的辅助模块。

    不直接 `import dashboard`：Runner 把所有插件目录都放进 sys.path，
    别的插件若也有同名模块就会串。按路径加载并加插件前缀命名，互不干扰。
    """
    module_name = f"_cost_report_{name}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"加载 {path} 失败")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_dashboard = _load_local("dashboard")
_render = _load_local("render")
_magpie = _load_local("magpie")

#: 宿主硬性要求：config.toml 必须有 [plugin].config_version，缺失即加载失败。
SUPPORTED_CONFIG_VERSION = "1.0.0"


class PluginSectionConfig(PluginConfigBase):
    """`[plugin]` 配置节。config_version 由插件维护，勿手改。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用",
        json_schema_extra={"label": "启用", "order": 0},
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置结构版本，由插件维护",
        json_schema_extra={"hidden": True, "disabled": True, "order": 99},
    )


class ReportConfig(PluginConfigBase):
    __ui_label__ = "统计"
    __ui_icon__ = "bar_chart"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="是否允许查询开销",
        json_schema_extra={"label": "启用查询", "order": 0},
    )

    currency: str = Field(
        default="元",
        description="金额单位",
        json_schema_extra={"label": "货币单位", "order": 1},
    )

    top_models: int = Field(
        default=8,
        ge=1,
        le=20,
        description="按花费列出前几个模型",
        json_schema_extra={
            "label": "展示模型数",
            "hint": "按花费从高到低取前 N 个，避免一次刷屏。",
            "step": 1,
            "order": 2,
        },
    )

    max_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="允许查询的最大天数",
        json_schema_extra={
            "label": "最大查询天数",
            "hint": "防止有人发 `/开销 9999` 把聚合表扫一遍。",
            "step": 1,
            "order": 3,
        },
    )

    show_cache: bool = Field(
        default=True,
        description="是否显示缓存命中率",
        json_schema_extra={
            "label": "显示缓存命中率",
            "hint": (
                "开启后额外读一次 `llm_usage` 明细表算命中率（聚合表里没有缓存字段）。"
                "命中率直接影响实际花费：命中部分按缓存价计费，通常只有正常价的百分之几。"
                "关掉可以省一次查询。"
            ),
            "order": 4,
        },
    )

    output_mode: str = Field(
        default="image",
        description="输出形式：text / image / both",
        json_schema_extra={
            "label": "输出形式",
            "hint": (
                "image 用 Playwright 把统计渲染成一张卡片图，比纯文本好看但要多花 1~2 秒；"
                "text 直接发文本；both 先发图再补一份文字（方便复制数字）。"
                "渲染失败会自动退回文本。"
            ),
            "order": 5,
        },
    )

    title: str = Field(
        default="麦麦开销",
        description="统计图卡片标题",
        json_schema_extra={
            "label": "卡片标题",
            "hint": (
                "统计图左上角的大标题，后面会自动接上时间范围（如「最近 24 小时」）。"
                "留空则只显示时间范围。"
            ),
            "placeholder": "麦麦开销",
            "order": 6,
        },
    )

    tool_enabled: bool = Field(
        default=True,
        description="是否给 LLM 提供查询开销的工具",
        json_schema_extra={
            "label": "允许 LLM 自查开销",
            "hint": (
                "开启后注册一个工具，用户问「今天花了多少钱」「哪个模型最贵」时"
                "LLM 会自己调它拿数字，再用自己的话回答。关掉则只有 `/开销` 命令能用。"
            ),
            "order": 7,
        },
    )

    tool_send_image: bool = Field(
        default=True,
        description="工具调用时是否同时发统计图",
        json_schema_extra={
            "label": "工具调用也发图",
            "hint": (
                "开启后 LLM 查开销时会顺手发一张卡片图，比纯文字直观。"
                "关掉则只把数字交给 LLM 由它转述，回复更短，但看不到图和分模型明细。"
            ),
            "depends_on": "tool_enabled",
            "order": 8,
        },
    )

    db_path: str = Field(
        default="",
        description="宿主数据库文件路径，仅缓存命中率用",
        json_schema_extra={
            "label": "数据库路径（可选）",
            "hint": (
                "留空则按「插件数据目录往上两级」去猜宿主根目录下的 MaiBot.db。"
                "这个推算依赖宿主的目录结构，宿主换布局就会失效；"
                "猜不到或结构变了，把完整路径填在这里。"
                "只在「显示缓存命中率」开启时才会读这个文件。"
            ),
            "placeholder": "/path/to/MaiBot/data/MaiBot.db",
            "depends_on": "show_cache",
            "order": 9,
        },
    )

    magpie_enabled: bool = Field(
        default=False,
        description="是否在卡片上展示 magpie 网关的 Gemini 用量",
        json_schema_extra={
            "label": "显示 magpie 用量（可选）",
            "hint": (
                "只有你自己跑了 magpie 这类本地反代网关时才需要打开。"
                "打开后卡片会多一个区块，显示今天用了多少 token、吃掉多少订阅配额。"
                "没有 magpie 的话保持关闭即可，插件不会发任何额外请求。"
            ),
            "order": 10,
        },
    )

    magpie_url: str = Field(
        default="http://127.0.0.1:3425",
        description="magpie 网关地址",
        json_schema_extra={
            "label": "magpie 地址",
            "hint": (
                "magpie 的 HTTP 地址，通常跑在本机。"
                "插件会请求它的 /v1/magpie/usage 和 /v1/magpie/quotas 两个接口。"
                "拉不到数据时这个区块会整块消失，不影响卡片其它部分。"
            ),
            "placeholder": "http://127.0.0.1:3425",
            "depends_on": "magpie_enabled",
            "order": 11,
        },
    )

    magpie_count_cost: bool = Field(
        default=True,
        description="是否把 magpie 的账面成本计入总花费",
        json_schema_extra={
            "label": "账面成本计入总花费",
            "hint": (
                "开启后，magpie 按公开价折算的成本会按模型并进「总花费」和「模型花费分布」，"
                "看到的是「如果按量付费要花多少」。"
                "关掉则只展示用量和配额，总花费仍按宿主记的 token 计费算，"
                "适合上游是包月订阅、只想看配额消耗的情况。"
            ),
            "depends_on": "magpie_enabled",
            "order": 12,
        },
    )

    magpie_usd_rate: float = Field(
        default=7.2,
        ge=0.0,
        description="兜底汇率：自动抓取失败时用这个值",
        json_schema_extra={
            "label": "兜底汇率",
            "hint": (
                "magpie 返回的 cost 和单价都是**美元**，而宿主记的模型花费是人民币，"
                "不换算直接相加会把美元当人民币。\n"
                "插件每 6 小时自动从公开汇率源抓一次，正常不用管这里；"
                "只有抓取失败（断网/被墙）时才用这个兜底值。"
            ),
            "depends_on": "magpie_enabled",
            "step": 0.1,
            "order": 13,
        },
    )


class RootConfig(PluginConfigBase):
    """配置根模型。⚠ `plugin` 节必须显式声明，否则加载失败。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)


class CostReportPlugin(MaiBotPlugin):
    config_model = RootConfig

    async def on_load(self) -> None:
        self.ctx.logger.info("开销统计插件已加载")

    async def on_unload(self) -> None:
        self.ctx.logger.info("开销统计插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("开销统计配置已热更新 version=%s", version)

    # ---------------------------------------------------------------- 数据

    def _clamp_days(self, days: int) -> int:
        """把天数夹到 [1, max_days]。命令和工具都走这里，避免各写一份。"""
        if days < 1:
            return 1
        return min(days, self.config.report.max_days)

    @staticmethod
    def _num(value: Any) -> float:
        """把宿主返回的数值字段转成 float，坏值按 0 算。

        宿主保证给数字，但数据库里被手工改过的行、或者宿主将来换字段类型时，
        一个坏值会让整张统计图崩掉，不值得。
        """
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _totals(self, models: list[dict[str, Any]]) -> tuple[float, int, int]:
        """(总花费, 总 token, 总调用次数)。

        三处格式化都从这里取，避免哪天改了一处、另外两处的口径悄悄漂开。
        """
        cost = sum(self._num(m.get("total_cost")) for m in models)
        tokens = sum(int(self._num(m.get("total_tokens"))) for m in models)
        req = sum(int(self._num(m.get("request_count"))) for m in models)
        return cost, tokens, req

    @staticmethod
    def _hit_miss(cache_stats: dict[str, dict[str, int]]) -> tuple[int, int]:
        """(缓存命中 token, 未命中 token)。"""
        hit = sum(int(v.get("hit") or 0) for v in cache_stats.values())
        miss = sum(int(v.get("miss") or 0) for v in cache_stats.values())
        return hit, miss

    @staticmethod
    def _pad(text: str, width: int) -> str:
        """按显示宽度补空格：中文占两格，直接用 format 的 :<N 会对不齐。"""
        shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
        return text + " " * max(0, width - shown)

    async def _gather(
        self, days: int
    ) -> tuple[list[dict[str, Any]] | None, dict[str, dict[str, int]]]:
        """取聚合统计 + 缓存命中。

        返回 `None` 表示查询本身失败（区别于「查询成功但没数据」的空列表），
        调用方据此决定是报错还是说「还没有记录」。
        """
        try:
            # 宿主按 request_count 降序取前 N 个（上限 50），不是按花费。
            # 取满 50 是为了尽量别漏掉「调用少但单价贵」的模型，
            # 否则卡片上的总花费会偏低。真超过 50 个时 footer 会注明。
            #
            # ⚠ 这里走 call_capability 而不是 ctx.statistics.local.models()：
            # 后者的封装会把「查询失败」也归一成空列表（SDK statistics.py:20-29），
            # 于是数据库被锁时用户看到的是「还没有模型调用记录」，查不出真原因。
            raw = await self.ctx.call_capability(
                "statistics.local.models", days=days, limit=50
            )
        except Exception as exc:
            self.ctx.logger.error("查询模型统计失败: %s", exc)
            return None, {}

        # 宿主查询失败时返回 {"success": False, "error": ...}，不会抛异常
        if isinstance(raw, dict):
            if not raw.get("success", True):
                # 只打前 200 字：软失败里可能带 SQL 和表名，整段进日志没必要
                self.ctx.logger.warning(
                    "统计查询失败: %s", str(raw.get("error") or raw)[:200]
                )
                return None, {}
            inner = raw.get("models")
            if not isinstance(inner, list):
                self.ctx.logger.warning("统计查询没有返回 models 列表，按失败处理")
                return None, {}
            raw = inner
        if not isinstance(raw, list):
            self.ctx.logger.warning(
                "统计查询返回了意外类型 %s，按失败处理", type(raw).__name__
            )
            return None, {}

        models = [m for m in raw if isinstance(m, dict)]

        cache_stats: dict[str, dict[str, int]] = {}
        if self.config.report.show_cache:
            cache_stats = await asyncio.to_thread(self._query_cache_stats, days)
        return models, cache_stats

    def _fallback_models(
        self, days: int
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]] | None:
        """宿主统计能力不可用时，用直连库的数据兜底。

        不兜底会出现「图能出、命令却报查询失败」的割裂：两条路径读的是同一份数据，
        一条挂了另一条还能用，用户看到的行为就自相矛盾。
        """
        data = self._collect_dashboard(days)
        if data is None:
            return None
        current = data["current"]
        models = [
            {
                "model_name": item["name"],
                "request_count": item["requests"],
                "total_cost": item["cost"],
                "total_tokens": item["tokens"],
            }
            for item in current.get("models") or []
        ]
        cache = {
            str(name): {"hit": int(v.get("hit") or 0), "miss": int(v.get("miss") or 0)}
            for name, v in (current.get("cache_by_model") or {}).items()
        }
        return models, cache

    @staticmethod
    def _resolve_stream(kwargs: dict) -> str:
        """工具路径的当前会话流 ID。

        `component_query._build_tool_context_payload`（宿主 747-750 行）把
        `stream_id` 和 `chat_id` 都设成 `context.stream_id or context.session_id`，
        两者同源，就是当前会话的 session_id，可以直接拿去发消息。

        ⚠ 绝不能拿 `chat_id` 去 `get_stream_by_group_id` 反查：群聊里它是一串
        32 位 session_id 而不是群号，反查必然失败；失败后再退回
        `get_stream_by_user_id`（那里面是发言人）就会把图发到发言人的私聊去。
        真实群号在 `group_id` 里，但那个键模型也能塞，所以干脆不做任何反查。
        """
        for key in ("stream_id", "chat_id"):
            value = str(kwargs.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _command_stream(kwargs: dict) -> str:
        """命令路径的当前会话 ID。

        命令的 kwargs 由宿主构造、模型不参与，所以 `message.session_id` 可信；
        而 `stream_id` 在私聊里可能是机器人账号号，拿它发会报「未找到聊天流」。
        先取 session_id，取不到再退回 stream_id。
        """
        message = kwargs.get("message")
        if isinstance(message, dict):
            sid = str(message.get("session_id") or "").strip()
            if sid:
                return sid
        return str(kwargs.get("stream_id") or "").strip()

    # ---------------------------------------------------------------- 工具

    @Tool(
        "check_cost",
        brief_description="查麦麦自己的 token 花费：有人问花了多少、成本多少时调用",
        detailed_description=(
            "返回总花费、token、调用次数、缓存命中率和几个主要模型。"
            "拿到数字用自己的话一两句说完，别念表格，也别主动提这个话题。"
        ),
        parameters=[
            ToolParameterInfo(
                name="days",
                param_type=ToolParamType.INTEGER,
                description="最近几天，不填默认 1（即 24 小时）",
                required=False,
            ),
        ],
    )
    async def handle_check_cost(self, days: int = 0, **kwargs):
        cfg = self.config.report
        if not self.config.plugin.enabled or not cfg.enabled or not cfg.tool_enabled:
            return {"success": False, "content": "开销统计没开，查不了。"}

        # 模型可能把天数传成字符串或 0，别直接 int() 崩掉。
        try:
            want = int(days or 0)
        except (TypeError, ValueError):
            want = 1
        span = self._clamp_days(want if want >= 1 else 1)
        # 留个痕迹，方便回头数 LLM 到底多爱查账。
        self.ctx.logger.debug("LLM 调用 check_cost days=%s", span)

        models, cache_stats = await self._gather(span)
        if models is None:
            fallback = self._fallback_models(span)
            if fallback is None:
                return {"success": False, "content": "统计表查不到，可能是数据库被占用或者路径不对。"}
            models, cache_stats = fallback
            self.ctx.logger.info("统计能力不可用，已用直连库数据兜底")

        content = self._summarize(models, span, cache_stats)

        if cfg.tool_send_image and models:
            stream_id = self._resolve_stream(kwargs)
            if stream_id:
                await self._send_image(models, span, cache_stats, stream_id)
            else:
                self.ctx.logger.warning("拿不到当前聊天流，本次只回文字不配图")

        return {"success": True, "content": content}

    # ---------------------------------------------------------------- 命令

    @Command("cost", pattern=r"^/(开销|成本|花费|cost)\s*(?P<days>\d{1,3})?\s*(?:天)?$")
    async def handle_cost(self, **kwargs):
        cfg = self.config.report
        if not self.config.plugin.enabled or not cfg.enabled:
            return False, "开销统计已关闭。", 1

        matched = kwargs.get("matched_groups") or {}
        raw_days = str(matched.get("days") or "").strip()
        days = self._clamp_days(int(raw_days) if raw_days else 1)

        models, cache_stats = await self._gather(days)
        if models is None:
            fallback = self._fallback_models(days)
            if fallback is None:
                return False, "查询失败，看看日志。", 1
            models, cache_stats = fallback
            self.ctx.logger.info("统计能力不可用，已用直连库数据兜底")

        text = self._format(models, days, cache_stats)
        stream_id = self._command_stream(kwargs)
        mode = str(cfg.output_mode or "image").strip().lower()

        # ⚠ 命令的返回值不会自动发到聊天流（bot.py:665 只存库记日志），必须自己发。
        sent_image = False
        if mode in ("image", "both") and stream_id:
            sent_image = await self._send_image(models, days, cache_stats, stream_id)

        if stream_id:
            if mode == "both" or not sent_image:
                try:
                    await self.ctx.send.text(text, stream_id)
                except Exception as exc:
                    self.ctx.logger.error("发送开销统计失败: %s", exc)

        return True, text, 1

    # ---------------------------------------------------------------- 图片输出

    #: 汇率缓存时长（秒）。公开汇率源一天才更新一次，6 小时足够。
    FX_TTL = 6 * 3600
    #: 抓取失败后的重试间隔，别每次都去撞墙
    FX_RETRY_TTL = 600

    async def _resolve_usd_rate(self) -> float:
        """取美元兑人民币汇率：缓存命中直接用，过期就重抓，抓不到退回配置值。"""
        now = time.monotonic()
        cache = getattr(self, "_fx_cache", None)
        if cache:
            rate, stamp, ttl = cache
            if now - stamp < ttl:
                return rate

        fetched = await asyncio.to_thread(_magpie.fetch_usd_cny)
        if fetched:
            self._fx_cache = (fetched, now, self.FX_TTL)
            self.ctx.logger.debug("美元汇率已更新: %.4f", fetched)
            return fetched

        fallback = float(getattr(self.config.report, "magpie_usd_rate", 7.2) or 7.2)
        # 缓存兜底值但缩短有效期，网络恢复后能尽快换回实时汇率
        self._fx_cache = (fallback, now, self.FX_RETRY_TTL)
        self.ctx.logger.debug("汇率抓取失败，暂时用配置值 %.4f", fallback)
        return fallback

    def _collect_dashboard(
        self,
        days: int,
        extra_by_model: dict[str, float] | None = None,
        prev_extra_by_model: dict[str, float] | None = None,
        pricing: dict[str, dict[str, float]] | None = None,
        usd_rate: float | None = None,
    ) -> dict[str, Any] | None:
        """采集 WebUI 统计页那套指标。数据库不可用返回 None。

        `extra_by_model` / `prev_extra_by_model` 是把「不按 token 计费、但按公开价折算」
        的开销（走订阅额度的 gemini，成本只记在 magpie 网关侧）按模型并进花费，
        这样「模型花费分布」和总花费才对得上。两个周期都要传，环比才有可比性。
        """
        db_path = self._db_path()
        if db_path is None:
            return None
        try:
            return _dashboard.collect(
                db_path, days,
                extra_by_model=extra_by_model,
                prev_extra_by_model=prev_extra_by_model,
                pricing=pricing,
                usd_rate=usd_rate if usd_rate is not None else float(
                    getattr(self.config.report, "magpie_usd_rate", 7.2) or 7.2
                ),
            )
        except Exception as exc:
            self.ctx.logger.warning("看板数据采集失败，退回简易卡片: %s", exc)
            return None

    async def _send_image(
        self,
        models: list[dict[str, Any]],
        days: int,
        cache_stats: dict[str, dict[str, int]],
        stream_id: str,
    ) -> bool:
        """把统计渲染成卡片图发出去。失败返回 False，由调用方退回文本。"""
        # Playwright 单实例不吃并发渲染：两次调用同时进来会互相排队甚至超时。
        # 用实例级锁把渲染串起来，锁只护渲染这一段，发图不受影响。
        lock = getattr(self, "_render_lock", None)
        if lock is None:
            lock = self._render_lock = asyncio.Lock()

        # 优先用看板渲染（指标全、带环比）；数据库读不到时退回旧的简易卡片，
        # 至少还能出一张图，不至于什么都不发。
        # magpie 用量是可选功能：没开开关就完全不发请求，卡片上也不会多区块。
        # 开启时顺手把两个周期的账面成本算出来，并进总花费。
        extra_by_model: dict[str, float] = {}
        prev_extra_by_model: dict[str, float] = {}
        magpie_data = None
        # 在 if 块外初始化：magpie 关闭时后面也要读它们，否则 NameError
        pricing: dict[str, dict[str, float]] = {}
        usd_rate = float(getattr(self.config.report, "magpie_usd_rate", 7.2) or 7.2)
        if self.config.report.magpie_enabled:
            url = str(self.config.report.magpie_url or "")
            now = datetime.now()
            cur_since = _dashboard.align_window(now, days)[0]
            prev_since = _dashboard.align_window(now - timedelta(days=days), days)[0]
            cur_window = await asyncio.to_thread(_magpie.collect_window, url, cur_since)
            prev_window = await asyncio.to_thread(_magpie.collect_window, url, prev_since)
            usd_rate = await self._resolve_usd_rate()
            count_cost = bool(getattr(self.config.report, "magpie_count_cost", True))
            if count_cost:
                # 用网关单价 × MaiBot 自己的 token 量算成本：网关明细只有最近 500 条，
                # 长窗口会截断，而 llm_usage 里有完整历史。
                pricing = await asyncio.to_thread(_magpie.fetch_pricing, url)
                if not pricing:
                    # 拿不到单价就不计入：网关明细里的 cost 是美元，直接加进人民币总额会差 7 倍
                    self.ctx.logger.debug("magpie 没返回单价表，本次不计入账面成本")
            magpie_data = await asyncio.to_thread(_magpie.collect, url)
            if magpie_data:
                magpie_data["window"] = cur_window or {}
                # 网关返回的 cost 是美元，渲染层要按同一汇率换算成人民币
                magpie_data["usd_rate"] = usd_rate
                magpie_data["count_cost"] = bool(
                    getattr(self.config.report, "magpie_count_cost", True)
                )
                # 只展示 MaiBot 这个窗口里真正调过的模型对应的配额池，
                # 没调过的池子（比如 Claude & GPT）不占版面
                magpie_data["matched"] = _magpie.match_pools(
                    (cur_window or {}).get("models") or [],
                    magpie_data.get("providers") or [],
                )
            elif extra_by_model:
                self.ctx.logger.debug("magpie 详情拉取失败，但窗口成本已并入模型花费")

        data = self._collect_dashboard(
            days, extra_by_model, prev_extra_by_model, pricing or None, usd_rate
        )
        if data is not None:
            if magpie_data:
                # 底部区块用和卡片一致的窗口统计，而不是网关的自然日统计
                # 用 llm_usage 的完整统计（网关明细只有最近 500 条，长窗口会截断）
                detail = data["current"].get("priced_usage") or {}
                if detail:
                    magpie_data["window_stats"] = {
                        "calls": sum(int(v.get("calls") or 0) for v in detail.values()),
                        "input": sum(int(v.get("input") or 0) for v in detail.values()),
                        "output": sum(int(v.get("output") or 0) for v in detail.values()),
                        "cost": round(sum(float(v.get("cost") or 0) for v in detail.values()), 6),
                    }
                elif cur_window:
                    magpie_data["window_stats"] = {
                        "calls": int(cur_window.get("calls") or 0),
                        "input": int(cur_window.get("input") or 0),
                        "output": int(cur_window.get("output") or 0),
                        "cost": float(data["current"].get("extra_cost") or 0),
                    }
                data["magpie"] = magpie_data
            html = _render.build(data, self.config.report)
            selector = "body"
            viewport = {"width": 1400, "height": 700}
        else:
            html = self._build_html(models, days, cache_stats)
            selector = ".card"
            viewport = {"width": 760, "height": 400}

        try:
            async with lock:
                result = await self.ctx.render.html2png(
                    html,
                    selector=selector,
                    viewport=viewport,
                    device_scale_factor=2.0,
                    full_page=False,
                    allow_network=False,
                )
        except Exception as exc:
            self.ctx.logger.warning("统计图渲染失败: %s", exc)
            return False

        b64 = ""
        if isinstance(result, dict):
            b64 = str(result.get("image_base64") or "")
        if not b64:
            self.ctx.logger.warning("统计图渲染没有返回 image_base64")
            return False

        # ⚠ send.image 失败时**不抛异常**，而是返回 False（SDK 把它列进
        # _BOOLEAN_SUCCESS_CAPABILITIES）。不接返回值就会把「没发出去」当成功，
        # 命令路径据此跳过补发文本，用户最后一条回复都收不到。
        try:
            ok = await self.ctx.send.image(b64, stream_id)
        except Exception as exc:
            self.ctx.logger.warning("发送统计图失败: %s", exc)
            return False
        if not ok:
            self.ctx.logger.warning("统计图没发出去，宿主返回失败")
            return False
        return True

    def _build_html(
        self,
        models: list[dict[str, Any]],
        days: int,
        cache_stats: dict[str, dict[str, int]],
    ) -> str:
        cfg = self.config.report
        unit = cfg.currency
        label = "最近 24 小时" if days == 1 else f"最近 {days} 天"
        # 标题可自定义；留空就只显示时间范围，不留一个孤零零的「·」。
        custom_title = str(cfg.title or "").strip()
        heading = f"{custom_title} · {label}" if custom_title else label

        total_cost, total_tokens, total_req = self._totals(models)
        avg_per_k = (total_cost / total_req * 1000) if total_req else 0.0

        hit_sum, miss_sum = self._hit_miss(cache_stats)
        denom = hit_sum + miss_sum
        hit_rate = (hit_sum / denom * 100) if denom else 0.0

        def esc(value: Any) -> str:
            # 引号也要转义：模型名会被塞进 title="{name}" 属性里
            return (
                str(value)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;")
            )

        ranked = sorted(models, key=lambda m: -self._num(m.get("total_cost")))[: cfg.top_models]
        rows: list[str] = []
        max_cost = self._num(ranked[0].get("total_cost")) if ranked else 0.0
        for m in ranked:
            name = esc(str(m.get("model_name") or "Unknown"))
            cost = self._num(m.get("total_cost"))
            tokens = int(self._num(m.get("total_tokens")))
            req = int(self._num(m.get("request_count")))
            share = (cost / total_cost * 100) if total_cost else 0.0
            bar = (cost / max_cost * 100) if max_cost else 0.0

            cache_cell = "<span class='muted'>—</span>"
            stats = cache_stats.get(str(m.get("model_name") or ""))
            if stats:
                d = stats["hit"] + stats["miss"]
                if d > 0:
                    rate = stats["hit"] / d * 100
                    tone = "#16a34a" if rate >= 60 else "#f59e0b" if rate >= 30 else "#dc2626"
                    cache_cell = f"<span style='color:{tone};font-weight:600'>{rate:.0f}%</span>"

            rows.append(
                f"""
                <tr>
                  <td class="name" title="{name}">{name}</td>
                  <td class="num">{cost:.2f}{esc(unit)}</td>
                  <td class="num">{tokens:,}</td>
                  <td class="num">{req:,}</td>
                  <td class="num">{cache_cell}</td>
                  <td class="bar-cell"><div class="bar"><i style="width:{bar:.1f}%"></i></div></td>
                  <td class="num muted">{share:.1f}%</td>
                </tr>
                """
            )

        # 宿主按调用次数取前 50 个模型，真撞到上限时说明一句，
        # 免得用户以为卡片上的总额就是全部花费。
        capped_note = "，按调用次数取前 50 个模型汇总" if len(models) >= 50 else ""

        cache_block = ""
        if denom > 0:
            cache_block = f"""
            <div class="stat">
              <div class="k">缓存命中</div>
              <div class="v">{hit_rate:.1f}<small>%</small></div>
              <div class="d">{hit_sum:,} 命中 / {miss_sum:,} 未命中</div>
            </div>
            """
        elif cfg.show_cache:
            # 开了命中率却拿不到数据时，明说一句，别让用户对着空白猜。
            cache_block = """
            <div class="stat">
              <div class="k">缓存命中</div>
              <div class="v muted">—</div>
              <div class="d">窗口内没有缓存数据，或读不到 llm_usage</div>
            </div>
            """

        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
    background: linear-gradient(160deg, #f8fafc 0%, #eef2f7 100%);
    color: #0f172a;
  }}
  .card {{
    background: #fff; border-radius: 18px; padding: 26px 28px;
    box-shadow: 0 10px 30px rgba(15,23,42,.08), 0 2px 6px rgba(15,23,42,.04);
  }}
  h1 {{ margin: 0 0 4px; font-size: 20px; font-weight: 700; letter-spacing: .5px; }}
  .sub {{ margin: 0 0 20px; font-size: 12px; color: #64748b; }}
  .stats {{ display: flex; gap: 14px; margin-bottom: 22px; }}
  .stat {{
    flex: 1; background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 12px; padding: 14px 16px;
  }}
  .stat .k {{ font-size: 11px; color: #64748b; margin-bottom: 6px; letter-spacing: .5px; }}
  .stat .v {{ font-size: 24px; font-weight: 700; color: #0f172a; line-height: 1.1; }}
  .stat .v small {{ font-size: 13px; font-weight: 500; margin-left: 2px; color: #64748b; }}
  .stat .d {{ margin-top: 5px; font-size: 11px; color: #94a3b8; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
  th {{
    text-align: left; font-weight: 600; font-size: 11px; color: #64748b;
    padding: 0 8px 8px; border-bottom: 1px solid #e2e8f0; white-space: nowrap;
  }}
  th.num, td.num {{ text-align: right; }}
  td {{ padding: 9px 8px; border-bottom: 1px solid #f1f5f9; }}
  tr:last-child td {{ border-bottom: none; }}
  td.name {{
    max-width: 210px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; font-weight: 500;
  }}
  td.num {{ font-variant-numeric: tabular-nums; }}
  .muted {{ color: #94a3b8; }}
  .bar-cell {{ width: 110px; }}
  .bar {{ height: 6px; background: #f1f5f9; border-radius: 999px; overflow: hidden; }}
  .bar i {{ display: block; height: 100%; background: linear-gradient(90deg,#60a5fa,#3b82f6); border-radius: 999px; }}
  .foot {{ margin-top: 16px; font-size: 11px; color: #94a3b8; }}
</style></head>
<body>
  <div class="card">
    <h1>{esc(heading)}</h1>
    <p class="sub">共 {total_req:,} 次模型调用</p>

    <div class="stats">
      <div class="stat">
        <div class="k">花费</div>
        <div class="v">{total_cost:.2f}<small>{esc(unit)}</small></div>
        <div class="d">平均每千次 {avg_per_k:.2f}{esc(unit)}</div>
      </div>
      <div class="stat">
        <div class="k">Token</div>
        <div class="v">{total_tokens:,}</div>
        <div class="d">prompt + completion</div>
      </div>
      {cache_block}
    </div>

    <table>
      <thead><tr>
        <th>模型</th><th class="num">花费</th><th class="num">Token</th>
        <th class="num">调用</th><th class="num">缓存</th><th>占比</th><th class="num"></th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <div class="foot">数据来自 statistics_model_hourly 聚合表{capped_note}{cache_stats and '，缓存命中率取自 llm_usage 明细' or ''}</div>
  </div>
</body></html>"""

    # ---------------------------------------------------------------- 缓存统计

    def _db_path(self) -> Path | None:
        """宿主数据库文件路径。

        优先用配置里的 `report.db_path`；留空才按「插件数据目录往上两级」去猜。
        猜路径押在宿主的目录布局上（`data/plugins/<id>` → `data/MaiBot.db`），
        宿主一改布局就会失效，所以留了手填的口子。
        """
        override = str(self.config.report.db_path or "").strip()
        if override:
            # resolve() 是为了 as_uri()：相对路径表达不成 file URI
            try:
                return Path(override).expanduser().resolve()
            except Exception:
                return None
        try:
            return (Path(self.ctx.paths.data_dir).parent.parent / "MaiBot.db").resolve()
        except Exception:
            return None

    def _query_cache_stats(self, days: int) -> dict[str, dict[str, int]]:
        """从 `llm_usage` 明细表算缓存命中。聚合表没有缓存字段，只能查明细。"""
        db_path = self._db_path()
        if db_path is None or not db_path.is_file():
            # 用 warning 而不是 debug：这会让卡片上的命中率变成「—」，得能在日志里看到原因
            self.ctx.logger.warning(
                "数据库文件不存在，缓存命中率不可用: %s（可在「数据库路径」里手填）", db_path
            )
            return {}

        # 对齐到下一个整点：聚合表按整点分桶，`bucket_time >= since` 取到的第一个
        # 桶是 ceil(since)，所以明细查询也必须从 ceil(since) 起，两边才是同一个窗口。
        # 不然卡片上的「花费」和「缓存命中」会差最多 59 分钟的数据。
        start = (datetime.now() - timedelta(days=days)).replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(hours=1)
        since = start.strftime("%Y-%m-%d %H:%M:%S")
        # 用 model_assign_name 分组：它才是配置里的模型名（`wb-proxy/deepseek-v4.1-flash`），
        # 和 statistics_model_hourly.model_name 对得上。llm_usage.model_name 存的是
        # model_identifier（`deepseek-v4.1-flash`），直接拿它对不上。
        sql = """
            SELECT COALESCE(NULLIF(model_assign_name, ''), NULLIF(model_name, ''), 'Unknown') AS model,
                   SUM(prompt_cache_hit_tokens) AS hit,
                   SUM(prompt_cache_miss_tokens) AS miss
            FROM llm_usage
            WHERE timestamp >= ?
            GROUP BY model
        """
        try:
            # 只读连接 + busy_timeout，避免和正在跑的 MaiBot 抢锁。
            # 用 as_uri() 而不是拼 f"file:{path}"：路径里带空格或 # 时后者会解析错。
            con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5)
            try:
                rows = con.execute(sql, (since,)).fetchall()
            finally:
                con.close()
        except Exception as exc:
            self.ctx.logger.warning("读取缓存统计失败: %s", exc)
            return {}

        stats: dict[str, dict[str, int]] = {}
        for model, hit, miss in rows:
            stats[str(model)] = {
                "hit": int(hit or 0),
                "miss": int(miss or 0),
            }
        return stats

    # ---------------------------------------------------------------- 格式化

    def _summarize(
        self,
        models: list[dict[str, Any]],
        days: int,
        cache_stats: dict[str, dict[str, int]],
    ) -> str:
        """给 LLM 读的紧凑摘要：两行、没有列对齐、没有缩进，方便它直接转述。"""
        cfg = self.config.report
        unit = cfg.currency
        label = "最近 24 小时" if days == 1 else f"最近 {days} 天"

        if not models:
            return f"{label}还没有模型调用记录。"

        total_cost, total_tokens, total_req = self._totals(models)

        head = (
            f"{label}：花费 {total_cost:.2f}{unit}，{total_tokens:,} token，"
            f"{total_req} 次调用"
        )
        hit, miss = self._hit_miss(cache_stats)
        if hit + miss:
            head += f"，缓存命中 {hit / (hit + miss) * 100:.1f}%"

        ranked = sorted(models, key=lambda m: -self._num(m.get("total_cost")))[:3]
        items = [
            f"{str(m.get('model_name') or 'Unknown')} "
            f"{self._num(m.get('total_cost')):.2f}{unit}"
            for m in ranked
        ]
        return f"{head}。\n花钱最多：{'、'.join(items)}。"

    def _format(
        self,
        models: list[dict[str, Any]],
        days: int,
        cache_stats: dict[str, dict[str, int]] | None = None,
    ) -> str:
        cfg = self.config.report
        unit = cfg.currency
        cache_stats = cache_stats or {}

        if not models:
            label = "最近 24 小时" if days == 1 else f"最近 {days} 天"
            return f"{label}还没有模型调用记录。"

        total_cost, total_tokens, total_req = self._totals(models)
        avg_cost = total_cost / total_req if total_req else 0.0

        label = "最近 24 小时" if days == 1 else f"最近 {days} 天"
        lines = [
            f"{label}",
            f"  花费 {total_cost:.2f}{unit}",
            f"  token {total_tokens:,}",
            f"  请求 {total_req} 次，平均每千次 {avg_cost * 1000:.2f}{unit}",
        ]

        # 缓存命中：命中部分按缓存价计费，直接影响实际花费
        if cache_stats:
            hit_sum, miss_sum = self._hit_miss(cache_stats)
            denom = hit_sum + miss_sum
            if denom > 0:
                rate = hit_sum / denom * 100
                lines.append(
                    f"  缓存命中 {rate:.1f}%（{hit_sum:,} 命中 / {miss_sum:,} 未命中）"
                )

        lines.extend(["", "按模型（花费降序）："])

        ranked = sorted(models, key=lambda m: -self._num(m.get("total_cost")))
        for m in ranked[: cfg.top_models]:
            name = str(m.get("model_name") or "Unknown")
            cost = self._num(m.get("total_cost"))
            tokens = int(self._num(m.get("total_tokens")))
            req = int(self._num(m.get("request_count")))
            share = (cost / total_cost * 100) if total_cost else 0.0
            row = (
                f"  {self._pad(name[:24], 26)} {cost:>7.2f}{unit} "
                f"{tokens:>11,} tok {req:>5}次 {share:>5.1f}%"
            )
            stats = cache_stats.get(name)
            if stats:
                denom = stats["hit"] + stats["miss"]
                if denom > 0:
                    row += f" 缓存{stats['hit'] / denom * 100:.0f}%"
            lines.append(row)

        if len(ranked) > cfg.top_models:
            rest = ranked[cfg.top_models :]
            rest_cost = sum(self._num(m.get("total_cost")) for m in rest)
            lines.append(f"  另外 {len(rest)} 个模型合计 {rest_cost:.2f}{unit}")

        return "\n".join(lines)


def create_plugin() -> CostReportPlugin:
    return CostReportPlugin()
