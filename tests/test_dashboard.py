"""看板数据层与渲染层的回归测试。

不依赖生产数据库：用临时 SQLite 造一份最小数据，覆盖正常值、缺表、脏值三种情况。
运行: python tests/test_dashboard.py
"""

import asyncio
import importlib.util
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name):
    spec = importlib.util.spec_from_file_location(f"_test_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # 插件用了 `from __future__ import annotations`，pydantic 解析 RootConfig 的前向引用时
    # 要能按 __module__ 在 sys.modules 里找回这个模块，否则报 PluginSectionConfig 未定义。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dashboard = load("dashboard")
render = load("render")
P = load("plugin")

RESULTS = []


def check(label, got, expect):
    ok = got == expect
    RESULTS.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'} {label}: {got!r}")
    if not ok:
        print(f"        expect={expect!r}")


def check_true(label, cond):
    RESULTS.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'} {label}")


# ---------------------------------------------------------------- 数值容错


def test_num():
    print("--- 数值容错 ---")
    check("None", dashboard._num(None), 0.0)
    check("字符串数字", dashboard._num("3.5"), 3.5)
    check("坏值", dashboard._num("坏值"), 0.0)
    check("nan", dashboard._num(float("nan")), 0.0)
    check("inf", dashboard._num(float("inf")), 0.0)
    check("整数", dashboard._int(7), 7)
    check("负数保留", dashboard._num(-2.5), -2.5)


def test_align_window():
    print("--- 窗口对齐 ---")
    now = datetime(2026, 11, 8, 0, 21, 33)
    start, end = dashboard.align_window(now, 1)
    check("起点对齐整点", start, datetime(2026, 11, 7, 1, 0, 0))
    check("终点是现在", end, now)
    start7, _ = dashboard.align_window(now, 7)
    check("7 天窗口", start7, datetime(2026, 11, 1, 1, 0, 0))


# ---------------------------------------------------------------- 渲染格式


def test_format():
    print("--- 渲染格式 ---")
    check("万", render.fmt_wan(25600), "2.56万")
    check("亿", render.fmt_wan(123456789), "1.23亿")
    check("千", render.fmt_wan(1500), "1,500")
    check("小数字", render.fmt_wan(42), "42")
    check("金额大", render.fmt_money(92.48), "92.48")
    check("金额小", render.fmt_money(0.0054), "0.0054")
    check("百分比", render.fmt_pct(51.63), "51.6%")

    # 上涨标红（花费涨了不是好事），下跌标绿
    text, color = render.fmt_delta(110, 100)
    check("上涨文本", text, "+10.0%")
    check("上涨标红", color, render.UP_BAD)
    text2, color2 = render.fmt_delta(90, 100)
    check("下跌文本", text2, "-10.0%")
    check("下跌标绿", color2, render.DOWN_GOOD)
    check("上期为 0 不显示", render.fmt_delta(10, 0)[0], "")

    check("转义尖括号", render.esc("<b>"), "&lt;b&gt;")
    check("转义引号", render.esc('a"b'), "a&quot;b")


# ---------------------------------------------------------------- 数据层


def make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE statistics_model_hourly (
            id INTEGER PRIMARY KEY, bucket_time TEXT, request_type TEXT, module_name TEXT,
            provider_name TEXT, model_name TEXT, request_count INTEGER,
            prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
            cost REAL, time_cost_sum REAL, time_cost_sq_sum REAL);
        CREATE TABLE statistics_message_hourly (
            id INTEGER PRIMARY KEY, bucket_time TEXT, chat_id TEXT, chat_name TEXT,
            chat_type TEXT, message_count INTEGER, latest_timestamp TEXT);
        CREATE TABLE llm_usage (
            id INTEGER PRIMARY KEY, model_name TEXT, model_assign_name TEXT,
            timestamp TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
            prompt_cache_hit_tokens INTEGER, prompt_cache_miss_tokens INTEGER);
        CREATE TABLE online_time (
            id INTEGER PRIMARY KEY, timestamp TEXT, duration_minutes INTEGER,
            start_timestamp TEXT, end_timestamp TEXT);
        CREATE TABLE tool_records (
            id INTEGER PRIMARY KEY, tool_id TEXT, timestamp TEXT, session_id TEXT,
            tool_name TEXT, tool_reasoning TEXT, tool_data TEXT);
        CREATE TABLE mai_messages (
            id INTEGER PRIMARY KEY, timestamp TEXT, user_id TEXT, processed_plain_text TEXT);
        CREATE TABLE bot_platform_accounts (
            id INTEGER PRIMARY KEY, platform TEXT, account_id TEXT, disabled INTEGER);
        """
    )
    # 当前周期 2026-11-07 01:00 ~ 现在
    con.executemany(
        "INSERT INTO statistics_model_hourly (bucket_time, model_name, request_count, "
        "prompt_tokens, completion_tokens, total_tokens, cost) VALUES (?,?,?,?,?,?,?)",
        [
            ("2026-11-07 10:00:00", "model-a", 100, 10000, 2000, 12000, 5.0),
            ("2026-11-07 11:00:00", "model-a", 50, 5000, 1000, 6000, 2.5),
            ("2026-11-07 10:00:00", "model-b", 30, 3000, 600, 3600, 1.5),
            # 上一周期（11-06），用于环比
            ("2026-11-06 10:00:00", "model-a", 200, 20000, 4000, 24000, 10.0),
        ],
    )
    con.executemany(
        "INSERT INTO statistics_message_hourly (bucket_time, chat_id, message_count) VALUES (?,?,?)",
        [("2026-11-07 10:00:00", "g1", 400), ("2026-11-06 10:00:00", "g1", 800)],
    )
    con.executemany(
        "INSERT INTO llm_usage (model_name, model_assign_name, timestamp, prompt_tokens, "
        "completion_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens) VALUES (?,?,?,?,?,?,?)",
        [
            ("model-a", "model-a", "2026-11-07 10:30:00", 10000, 1000, 6000, 4000),
            ("model-a", "model-a", "2026-11-07 11:30:00", 5000, 500, 0, 5000),
        ],
    )
    # duration_minutes 故意写成 5：宿主那张表里这字段恒为 5，不能拿它算在线时长
    con.execute(
        "INSERT INTO online_time (timestamp, duration_minutes, start_timestamp, end_timestamp) "
        "VALUES ('2026-11-07 10:00:00', 5, '2026-11-07 10:00:00', '2026-11-07 12:00:00')"
    )
    con.executemany(
        "INSERT INTO tool_records (timestamp, tool_name, tool_data) VALUES (?,?,?)",
        [
            ("2026-11-07 10:10:00", "t1", '{"success": true}'),
            ("2026-11-07 10:11:00", "t2", '{"success": false}'),
            ("2026-11-07 10:12:00", "t3", '{"success": true}'),
            ("2026-11-07 10:13:00", "t4", "不是 JSON"),
        ],
    )
    con.executemany(
        "INSERT INTO mai_messages (timestamp, user_id, processed_plain_text) VALUES (?,?,?)",
        [
            ("2026-11-07 10:20:00", "999", "群友说话"),
            ("2026-11-07 10:21:00", "999", "群友又说"),
            ("2026-11-07 10:22:00", "BOT1", "机器人回复"),
        ],
    )
    con.execute("INSERT INTO bot_platform_accounts (platform, account_id, disabled) VALUES ('qq','BOT1',0)")
    con.commit()
    con.close()


def test_collect():
    print("--- 数据采集 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "test.db"
        make_db(db)
        now = datetime(2026, 11, 8, 0, 21, 0)
        data = dashboard.collect(db, 1, now=now)
        check_true("采集成功", data is not None)
        c, p = data["current"], data["previous"]

        check("总花费", round(c["total_cost"], 2), 9.0)
        check("总请求数", c["total_requests"], 180)
        check("总 Token", c["total_tokens"], 21600)
        check("输入 Token", c["input_tokens"], 18000)
        check("输出 Token", c["output_tokens"], 3600)
        check("总消息数", c["total_messages"], 400)
        check("总回复数", c["total_replies"], 1)
        check("回复率", round(c["reply_rate"], 2), 0.25)
        check("在线分钟（按起止区间算，忽略 duration_minutes）", c["online_minutes"], 120)

        # 缓存：命中 6000，未命中 9000，无信息 = 15000-6000-9000 = 0
        check("缓存命中", c["cache_hit"], 6000)
        check("缓存未命中", c["cache_miss"], 9000)
        check("缓存命中率", round(c["cache_rate"], 2), 40.0)
        check_true("有缓存数据", data["has_cache"])

        # 工具失败率：坏 JSON 无法判断成败，跳过不计入分母；有效 3 条里 1 条失败
        check("任务失败率", round(c["tool_fail_rate"], 2), 33.33)

        # 环比：上期花费 10.0，本期 9.0
        check("上期花费", round(p["total_cost"], 2), 10.0)
        text, _ = render.fmt_delta(c["total_cost"], p["total_cost"])
        check("花费环比", text, "-10.0%")

        check("模型数", len(c["models"]), 2)
        check("模型排序(花费降序)", c["models"][0]["name"], "model-a")
        check_true("时间序列非空", len(data["series"]) >= 2)


def test_collect_missing_db():
    print("--- 异常输入 ---")
    check("数据库不存在返回 None", dashboard.collect(Path("/nonexistent/x.db"), 1), None)

    with tempfile.TemporaryDirectory() as d:
        empty = Path(d) / "empty.db"
        sqlite3.connect(empty).close()
        data = dashboard.collect(empty, 1)
        check_true("空库不崩", data is not None)
        check("空库花费为 0", data["current"]["total_cost"], 0.0)
        check("空库无缓存", data["has_cache"], False)


# ---------------------------------------------------------------- 渲染输出


def test_fallback_models():
    """宿主能力不可用时，直连库能读就该兜底成功。"""
    print("--- 能力失败兜底 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "test.db"
        make_db(db)

        class Fake:
            pass
        f = Fake()
        f._db_path = lambda: db
        f._collect_dashboard = lambda days: dashboard.collect(db, days, now=datetime(2026, 11, 8, 0, 21, 0))
        f.ctx = SimpleNamespace(logger=SimpleNamespace(warning=lambda *a, **k: None,
                                                      info=lambda *a, **k: None,
                                                      debug=lambda *a, **k: None,
                                                      error=lambda *a, **k: None))
        f.config = SimpleNamespace(report=SimpleNamespace(max_days=30, db_path="", show_cache=True,
                                                          currency="元", top_models=8))
        from types import MethodType
        f._fallback_models = MethodType(P.CostReportPlugin._fallback_models, f)

        got = f._fallback_models(1)
        check_true("兜底返回数据", got is not None)
        if got:
            models, cache = got
            check("兜底模型数", len(models), 2)
            check("兜底模型名", models[0]["model_name"], "model-a")
            check("兜底花费", round(models[0]["total_cost"], 2), 7.5)
            check_true("兜底带缓存数据", len(cache) >= 1)

        # 数据库也不可用时返回 None，由调用方报错
        f2 = Fake()
        f2._db_path = lambda: Path("/nonexistent/x.db")
        f2._collect_dashboard = lambda days: None
        f2._fallback_models = MethodType(P.CostReportPlugin._fallback_models, f2)
        check("库也没有返回 None", f2._fallback_models(1), None)



def test_online_minutes_interval():
    """在线时长按起止区间与窗口求交集，不采信 duration_minutes。"""
    print("--- 在线时长区间计算 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE online_time (
                id INTEGER PRIMARY KEY, timestamp TEXT, duration_minutes INTEGER,
                start_timestamp TEXT, end_timestamp TEXT);
        """)
        # 窗口：2026-11-07 01:00 ~ 2026-11-08 00:21
        # 1) 完全落在窗口内，2 小时
        con.execute("INSERT INTO online_time (duration_minutes, start_timestamp, end_timestamp) "
                    "VALUES (5, '2026-11-07 10:00:00', '2026-11-07 12:00:00')")
        # 2) 跨窗口起点，只算窗口内那部分：01:00 ~ 03:00 = 2 小时
        con.execute("INSERT INTO online_time (duration_minutes, start_timestamp, end_timestamp) "
                    "VALUES (5, '2026-11-06 23:00:00', '2026-11-07 03:00:00')")
        # 3) 跨窗口终点，只算 22:00 ~ 00:21 = 141 分钟
        con.execute("INSERT INTO online_time (duration_minutes, start_timestamp, end_timestamp) "
                    "VALUES (5, '2026-11-07 22:00:00', '2026-11-08 02:00:00')")
        # 4) 完全在窗口外，不该计入
        con.execute("INSERT INTO online_time (duration_minutes, start_timestamp, end_timestamp) "
                    "VALUES (5, '2026-11-01 00:00:00', '2026-11-01 05:00:00')")
        con.commit()
        con.close()

        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        got = dashboard._online_minutes(con, datetime(2026, 11, 7, 1, 0), datetime(2026, 11, 8, 0, 21))
        con.close()
        # 120 + 120 + 141 = 381
        check("三段交集合计", got, 381)



def test_render_blocks():
    print("--- 渲染区块 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "test.db"
        make_db(db)
        data = dashboard.collect(db, 1, now=datetime(2026, 11, 8, 0, 21, 0))
        cfg = SimpleNamespace(currency="元", title="麦麦开销")
        html = render.build(data, cfg)
        for block in ("统计概览", "Prompt缓存", "模型花费分布", "Token流向拆解", "单位成本与效率"):
            check_true(f"含「{block}」", block in html)
        check_true("含标题", "麦麦开销" in html)
        check_true("含总花费", "9.00" in html)
        check_true("含模型名", "model-a" in html)
        check_true("含环比标签", "%<" in html or "%</span>" in html)
        # 输入输出比 = 15000 / 3600 ≈ 4.2
        check_true("输入输出比正确", "4.2 : 1" in html)

    # 空数据也要能出图
    empty_data = {
        "days": 1, "start": datetime.now(), "end": datetime.now(),
        "current": {
            "total_cost": 0, "total_requests": 0, "total_tokens": 0,
            "input_tokens": 0, "output_tokens": 0, "cache_hit": 0, "cache_miss": 0,
            "cache_no_info": 0, "cache_rate": 0, "total_messages": 0, "total_replies": 0,
            "reply_rate": 0, "online_minutes": 0, "cost_per_hour": 0, "tokens_per_hour": 0,
            "cost_per_message": 0, "cost_per_reply": 0, "calls_per_reply": 0,
            "tool_fail_rate": 0, "models": [], "cache_by_model": {},
        },
        "previous": {},
        "series": [],
        "has_cache": False,
    }
    html2 = render.build(empty_data, SimpleNamespace(currency="元", title=""))
    check_true("空数据不崩", "统计概览" in html2)


def test_used_models_decoupled():
    """显示哪些模型只认宿主真实调用，不依赖任何外部定价表。"""
    print("--- used_models 取自真实调用 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        make_db(db)
        now = datetime(2026, 11, 8, 0, 21, 0)

        base = dashboard.collect(db, 1, now=now)
        check("有 used_models", base["current"]["used_models"], ["model-a"])
        check("总花费只来自宿主记录", round(base["current"]["total_cost"], 4), 9.0)

        # used_models 取 model_identifier，不是显示名
        con = sqlite3.connect(db)
        con.execute("INSERT INTO llm_usage (model_name, model_assign_name, timestamp, prompt_tokens, "
                    "completion_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens) "
                    "VALUES ('provider/gemini-x', 'gemini-x', '2026-11-07 12:00:00', 100, 10, 0, 100)")
        con.commit(); con.close()
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        names = dashboard.used_models(con, datetime(2026, 11, 7, 0, 0), datetime(2026, 11, 8, 0, 0))
        con.close()
        check_true("used_models 用带前缀的 identifier", "provider/gemini-x" in names)
        check_true("不含纯显示名", "gemini-x" not in names)




def test_send_image_default_config():
    """默认配置下出图路径不能抛异常（曾经因为漏初始化而 UnboundLocalError）。"""
    print("--- 出图默认配置 ---")

    class _Logger:
        def __init__(self):
            self.lines = []

        def _rec(self, level):
            return lambda msg, *a: self.lines.append(f"{level} {msg % a if a else msg}")

        def debug(self, msg, *a):
            self._rec("DEBUG")(msg, *a)

        def info(self, msg, *a):
            self._rec("INFO")(msg, *a)

        def warning(self, msg, *a):
            self._rec("WARN")(msg, *a)

        def error(self, msg, *a):
            self._rec("ERROR")(msg, *a)

    class _Render:
        def __init__(self):
            self.calls = []

        async def html2png(self, html, **kw):
            self.calls.append(html)
            return {"image_base64": "iVBORw0KGgo="}

    class _Send:
        def __init__(self):
            self.images = []

        async def image(self, *a, **kw):
            self.images.append(a)
            return True

        async def text(self, *a, **kw):
            return True

    async def _drive(db_path, data_dir):
        cfg = P.RootConfig()
        logger, render, send = _Logger(), _Render(), _Send()
        plugin = P.CostReportPlugin.__new__(P.CostReportPlugin)
        plugin._plugin_config_data = {}
        plugin._plugin_config_instance = cfg
        plugin._ctx = SimpleNamespace(
            logger=logger, render=render, send=send,
            paths=SimpleNamespace(data_dir=str(data_dir)),
        )
        try:
            ok = await plugin._send_image([], 7, {}, "stream-x")
        except Exception as exc:
            return ("EXC", f"{type(exc).__name__}: {exc}", render, send, logger)
        return (ok, None, render, send, logger)

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        db = base / "MaiBot.db"
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE llm_usage (id INTEGER PRIMARY KEY, timestamp REAL,"
            " model_name TEXT, model_assign_name TEXT, prompt_tokens INTEGER,"
            " completion_tokens INTEGER, total_tokens INTEGER,"
            " prompt_cache_hit_tokens INTEGER, prompt_cache_miss_tokens INTEGER, cost REAL);"
        )
        con.commit()
        con.close()

        ok, err, render, send, logger = asyncio.run(_drive(db, base / "data/plugins/x"))
        check("没有抛异常", err, None)
        check_true("返回成功", ok is True)
        check("渲染被调用一次", len(render.calls), 1)
        check("图片发出去了", len(send.images), 1)
        bad = [x for x in logger.lines if x.startswith("ERROR") or "UnboundLocal" in x]
        check("日志里没有错误", bad, [])


def run(round_no: int) -> bool:
    print(f"\n===== 第 {round_no} 轮 =====")
    before = len(RESULTS)
    test_num()
    test_align_window()
    test_format()
    test_collect()
    test_collect_missing_db()
    test_fallback_models()
    test_online_minutes_interval()
    test_render_blocks()
    test_used_models_decoupled()
    test_send_image_default_config()
    return all(RESULTS[before:])


if __name__ == "__main__":
    results = [run(i) for i in (1, 2, 3)]
    print(f"\n共 {len(RESULTS)} 个断言，失败 {RESULTS.count(False)} 个")
    print("三遍结果:", results)
    sys.exit(0 if all(results) else 1)
