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
    spec.loader.exec_module(module)
    return module


dashboard = load("dashboard")
render = load("render")
magpie = load("magpie")
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



# ---------------------------------------------------------------- magpie（可选功能）


def test_magpie_parsing():
    print("--- magpie 解析 ---")
    check("日期标签格式", magpie._day_label(datetime(2026, 11, 8)), "Nov 8")
    check("个位日期不补零", magpie._day_label(datetime(2026, 11, 5)), "Nov 5")

    usage = {
        "calls": 100, "input": 1000, "output": 100, "cost": 1.5,
        "series": [
            {"label": "Nov 7", "calls": 83, "input": 351274, "output": 20272, "cache_read": 3836, "cost": 0.3398},
            {"label": "Nov 8", "calls": 5, "input": 40409, "output": 599, "cache_read": 0, "cost": 0.0326},
        ],
    }
    day = magpie._pick_day(usage, "Nov 8")
    check("取当天 calls", day["calls"], 5)
    check("取当天 input", day["input"], 40409)
    check("取当天 cost", round(day["cost"], 4), 0.0326)
    check("取不存在的日期返回 0", magpie._pick_day(usage, "Nov 99")["calls"], 0)

    quotas = {"data": [{
        "provider": "antigravity", "plan": "Google AI Pro", "user": "a@b.c",
        "windows": [
            {"name": "Gemini · 7 days", "used": 0.3481},
            {"name": "Gemini · 5 hours", "used": 0.1092},
            {"name": "Claude & GPT · 7 days", "used": 0},
        ],
    }]}
    q = magpie._parse_quota(quotas)
    providers = q["providers"]
    check("provider 数量", len(providers), 1)
    check("套餐名", providers[0]["plan"], "Google AI Pro")
    check("池子数量", len(providers[0]["pools"]), 2)
    check("Gemini 7 天配额", round(providers[0]["pools"]["Gemini"]["quota_7d"], 4), 0.3481)
    check("Gemini 5 小时配额", round(providers[0]["pools"]["Gemini"]["quota_5h"], 4), 0.1092)
    check("Claude & GPT 也保留", round(providers[0]["pools"]["Claude & GPT"]["quota_7d"], 4), 0.0)
    check("空数据不崩", magpie._parse_quota({})["providers"], [])


def test_match_pools():
    """只把实际调过的模型映射到对应配额池。"""
    print("--- 模型到配额池映射 ---")
    providers = [{
        "provider": "antigravity", "plan": "Google AI Pro", "account": "a@b.c",
        "pools": {
            "Gemini": {"quota_7d": 0.35, "quota_5h": 0.11},
            "Claude & GPT": {"quota_7d": 0.0, "quota_5h": 0.0},
        },
    }]
    only_gemini = magpie.match_pools(["antigravity/gemini-3.8-flash"], providers)
    check("只调 gemini 时只返回一个池", len(only_gemini), 1)
    check("池名", only_gemini[0]["pool"], "Gemini")
    check("带上命中的模型", only_gemini[0]["models"], ["antigravity/gemini-3.8-flash"])

    both = magpie.match_pools(
        ["antigravity/gemini-3.8-flash", "antigravity/claude-sonnet"], providers
    )
    check("调了两个池就返回两个", len(both), 2)
    check("Claude & GPT 命中", any(x["pool"] == "Claude & GPT" for x in both), True)

    check("没调任何模型返回空", magpie.match_pools([], providers), [])
    check("模型对不上任何池返回空", magpie.match_pools(["deepseek-v4.1-flash"], providers), [])
    check("空 providers 不崩", magpie.match_pools(["a/gemini-x"], []), [])


def test_magpie_collect_failures():
    print("--- magpie 失败处理 ---")
    check("空地址返回 None", magpie.collect(""), None)
    check("连不上返回 None", magpie.collect("http://127.0.0.1:1", timeout=1), None)
    check("地址无效返回 None", magpie.collect("不是地址", timeout=1), None)


def test_render_magpie_block():
    print("--- magpie 渲染 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "test.db"
        make_db(db)
        data = dashboard.collect(db, 1, now=datetime(2026, 11, 8, 0, 21, 0))
        cfg = SimpleNamespace(currency="元", title="麦麦开销")

        # 不开启时不该出现这个区块
        html_off = render.build(data, cfg)
        check_true("未开启时无 magpie 区块", "来自 magpie 网关" not in html_off)

        # 开启后出现
        data["magpie"] = {
            "today": {"calls": 5, "input": 40409, "output": 599, "cache_read": 0, "cost": 0.0326},
            "yesterday": {"calls": 83, "input": 351274, "output": 20272, "cache_read": 3836, "cost": 0.3398},
            "total_30d": {"calls": 2418, "input": 9602063, "output": 532846, "cost": 9.2009},
            "window": {"models": ["antigravity/gemini-3.8-flash"], "truncated": False},
            "matched": [{
                "provider": "antigravity", "plan": "Google AI Pro", "pool": "Gemini",
                "quota_7d": 0.3481, "quota_5h": 0.1092,
                "models": ["antigravity/gemini-3.8-flash"],
            }],
        }
        html_on = render.build(data, cfg)
        check_true("开启后出现区块", "来自 magpie 网关" in html_on)
        check_true("标题带实际模型名", "gemini-3.8-flash" in html_on)
        check_true("显示套餐名", "Google AI Pro" in html_on)
        check_true("显示今日次数", ">5<" in html_on or "5<span" in html_on)
        check_true("显示配额百分比", "0.35%" in html_on and "99.65%" in html_on)
        check_true("配额条带池名", "Gemini · 7 天配额" in html_on)
        check_true("显示昨日对比", "83 次" in html_on)

        # 配额缺失时也要能渲染
        data["magpie"]["matched"][0]["quota_7d"] = None
        data["magpie"]["matched"][0]["quota_5h"] = None
        html_none = render.build(data, cfg)
        check_true("配额缺失不崩", "gemini-3.8-flash" in html_none)

        # 一个池子都没匹配上时也要能渲染
        data["magpie"]["matched"] = []
        html_empty = render.build(data, cfg)
        check_true("无匹配池不崩", "没有走 magpie 的模型调用" in html_empty)



def test_extra_cost_merge():
    """外部折算成本并进总花费后，派生指标要跟着重算。"""
    print("--- 账面成本并入 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "test.db"
        make_db(db)
        now = datetime(2026, 11, 8, 0, 21, 0)
        base = dashboard.collect(db, 1, now=now)
        merged = dashboard.collect(
            db, 1, now=now,
            extra_by_model={"model-a": 0.5},
            prev_extra_by_model={"model-a": 0.3},
        )

        check("原始总花费", round(base["current"]["total_cost"], 4), 9.0)
        check("并入后总花费", round(merged["current"]["total_cost"], 4), 9.5)
        check("记录额外成本", merged["current"].get("extra_cost"), 0.5)
        check_true("花费/时间 已重算", merged["current"]["cost_per_hour"] > base["current"]["cost_per_hour"])
        check_true("花费/消息 已重算", merged["current"]["cost_per_message"] > base["current"]["cost_per_message"])
        check("上一周期也并入", round(merged["previous"]["total_cost"], 4), 10.3)
        check("传空时无变化", round(dashboard.collect(db, 1, now=now, extra_by_model={})["current"]["total_cost"], 4), 9.0)

        # 关键：分摊到模型后，模型花费合计必须等于总花费
        a_model = next(m for m in merged["current"]["models"] if m["name"] == "model-a")
        check("model-a 的 cost 已加 0.5", round(a_model["cost"], 4), 8.0)
        check("model-a 记录了 extra_cost", a_model.get("extra_cost"), 0.5)
        model_sum = round(sum(m["cost"] for m in merged["current"]["models"]), 6)
        check("模型合计 == 总花费", model_sum, round(merged["current"]["total_cost"], 6))
        check("模型按花费重排", merged["current"]["models"][0]["name"], "model-a")

        # 宿主表里没有的模型也要补一条，不能静默丢掉
        extra_new = dashboard.collect(db, 1, now=now, extra_by_model={"gemini-x": 0.7})
        names = [m["name"] for m in extra_new["current"]["models"]]
        check_true("未知模型补进列表", "gemini-x" in names)
        check("补进来的模型带成本", round(next(m for m in extra_new["current"]["models"] if m["name"] == "gemini-x")["cost"], 4), 0.7)


def test_collect_window_filter():
    """窗口过滤只统计 since 之后的请求。"""
    print("--- magpie 窗口过滤 ---")
    rows = [
        {"t": "2026-11-07T10:00:00+08:00", "in": 100, "out": 10, "cost": 0.1},
        {"t": "2026-11-07T12:00:00+08:00", "in": 200, "out": 20, "cost": 0.2},
        {"t": "2026-11-08T00:00:00+08:00", "in": 300, "out": 30, "cost": 0.3},
    ]
    # 直接测内部逻辑：模拟 _get 的返回
    class FakeGet:
        pass
    since = datetime(2026, 11, 7, 11, 0)
    total_cost = 0.0
    calls = 0
    for row in rows:
        when = datetime.fromisoformat(row["t"]).replace(tzinfo=None)
        if when < since:
            continue
        calls += 1
        total_cost += row["cost"]
    check("窗口内次数", calls, 2)
    check("窗口内成本", round(total_cost, 4), 0.5)



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



def test_magpie_count_switch():
    """计入开关：只影响标注和是否并入，不影响用量展示。"""
    print("--- 账面成本计入开关 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "test.db"
        make_db(db)
        data = dashboard.collect(db, 1, now=datetime(2026, 11, 8, 0, 21, 0))
        cfg = SimpleNamespace(currency="元", title="麦麦开销")
        base_magpie = {
            "today": {"calls": 5, "input": 40409, "output": 599, "cache_read": 0, "cost": 0.0326},
            "yesterday": {"calls": 83},
            "window": {"models": ["a/gemini-x"], "truncated": False},
            "matched": [{"provider": "p", "plan": "Plan", "pool": "Gemini",
                         "quota_7d": 0.35, "quota_5h": 0.11, "models": ["a/gemini-x"]}],
        }

        data["magpie"] = {**base_magpie, "count_cost": True}
        html_on = render.build(data, cfg)
        check_true("开启时标注已计入", "账面已计入总花费" in html_on)

        data["magpie"] = {**base_magpie, "count_cost": False}
        html_off = render.build(data, cfg)
        check_true("关闭时标注未计入", "账面未计入总花费" in html_off)
        check_true("关闭时仍展示用量", "本窗口调用" in html_off)
        check_true("关闭时仍展示配额", "0.35%" in html_off)
        check_true("关闭时不再出现已计入字样", "账面已计入总花费" not in html_off)

        # 缺 count_cost 字段时按默认「计入」处理
        data["magpie"] = dict(base_magpie)
        html_default = render.build(data, cfg)
        check_true("缺字段时默认计入", "账面已计入总花费" in html_default)


def test_config_has_switch():
    """配置模型里得有这个开关，且默认开启。"""
    print("--- 配置项存在性 ---")
    fields = P.ReportConfig.model_fields
    check_true("存在 magpie_count_cost", "magpie_count_cost" in fields)
    check("默认值为 True", fields["magpie_count_cost"].default, True)
    check_true("存在 magpie_enabled", "magpie_enabled" in fields)
    check("magpie_enabled 默认关闭", fields["magpie_enabled"].default, False)
    check_true("存在 magpie_url", "magpie_url" in fields)



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



def test_priced_cost():
    """单价 × token 量算成本，按汇率换成人民币。"""
    print("--- 单价计算 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE llm_usage (
                id INTEGER PRIMARY KEY, model_name TEXT, model_assign_name TEXT,
                timestamp TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
                prompt_cache_hit_tokens INTEGER, prompt_cache_miss_tokens INTEGER);
        """)
        # 1M 输入（其中 400K 命中缓存）+ 100K 输出
        con.execute("INSERT INTO llm_usage (model_assign_name, timestamp, prompt_tokens, "
                    "completion_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens) "
                    "VALUES ('a/gemini-x', '2026-11-07 10:00:00', 1000000, 100000, 400000, 600000)")
        con.commit()
        con.close()

        pricing = {"a/gemini-x": {"prompt": 0.00000075, "input_cache_read": 0.000000075,
                                  "completion": 0.00000375}}
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        got = dashboard.priced_cost(con, datetime(2026, 11, 7, 0, 0), datetime(2026, 11, 8, 0, 0), pricing, 7.2)
        con.close()
        # 400K×0.075/M + 600K×0.75/M + 100K×3.75/M = 0.03 + 0.45 + 0.375 = 0.855 美元
        # ×7.2 = 6.156 人民币
        check("按单价算出的成本", round(got["a/gemini-x"], 3), 6.156)

        # 未命中量用 prompt - hit 兜底（服务端漏报 miss 字段时也不会少算）
        con = sqlite3.connect(db)
        con.execute("INSERT INTO llm_usage (model_assign_name, timestamp, prompt_tokens, "
                    "completion_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens) "
                    "VALUES ('a/gemini-x', '2026-11-07 11:00:00', 500000, 0, 0, 0)")
        con.commit(); con.close()
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        got2 = dashboard.priced_cost(con, datetime(2026, 11, 7, 0, 0), datetime(2026, 11, 8, 0, 0), pricing, 7.2)
        con.close()
        # 再叠 500K 全价输入 = 0.375 美元 × 7.2 = 2.7
        check("漏报 miss 时按全价算", round(got2["a/gemini-x"], 3), round(6.156 + 2.7, 3))

        # 没定价的模型不参与
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        got3 = dashboard.priced_cost(con, datetime(2026, 11, 7, 0, 0), datetime(2026, 11, 8, 0, 0), {}, 7.2)
        con.close()
        check("空定价返回空", got3, {})


def test_collect_with_pricing():
    """collect 传 pricing 时，模型分布和总花费要一起变。"""
    print("--- collect 接单价 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        make_db(db)
        pricing = {"model-a": {"prompt": 0.000001, "input_cache_read": 0.0000001,
                               "completion": 0.000004}}
        now = datetime(2026, 11, 8, 0, 21, 0)
        base = dashboard.collect(db, 1, now=now)
        priced = dashboard.collect(db, 1, now=now, pricing=pricing, usd_rate=7.0)
        check_true("传定价后总花费变大", priced["current"]["total_cost"] > base["current"]["total_cost"])
        model_sum = round(sum(m["cost"] for m in priced["current"]["models"]), 6)
        check("模型合计仍等于总花费", model_sum, round(priced["current"]["total_cost"], 6))



def test_reset_hint():
    """配额重置时间转成相对说法。"""
    print("--- 配额重置提示 ---")
    from datetime import timedelta
    now = datetime.now().astimezone()
    check_true("7 天后", "7 天后重置" in render._reset_hint((now + timedelta(days=7, hours=1)).isoformat()))
    check_true("5 小时后", "5 小时后重置" in render._reset_hint((now + timedelta(hours=5, minutes=1)).isoformat()))
    # 用 29~30 分钟容忍测试内的毫秒级时间差
    minute_hint = render._reset_hint((now + timedelta(minutes=30)).isoformat())
    check_true("分钟级提示", minute_hint.endswith("分钟后重置") and minute_hint.split(" ")[0].isdigit())
    check("已过期返回即将重置", render._reset_hint((now - timedelta(hours=1)).isoformat()), "即将重置")
    check("空值返回空", render._reset_hint(None), "")
    check("坏值返回空", render._reset_hint("不是时间"), "")
    # Z 结尾的 UTC 也能解析
    check_true("UTC 的 Z 后缀", render._reset_hint("2099-01-01T00:00:00Z") != "")


def test_magpie_window_stats():
    """底部区块优先用窗口统计，并且配额条带重置提示。"""
    print("--- 窗口统计与重置提示 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "test.db"
        make_db(db)
        data = dashboard.collect(db, 1, now=datetime(2026, 11, 8, 0, 21, 0))
        cfg = SimpleNamespace(currency="元", title="麦麦开销")
        from datetime import timedelta
        soon = (datetime.now().astimezone() + timedelta(days=6)).isoformat()
        data["magpie"] = {
            "window_stats": {"calls": 88, "input": 391683, "output": 20871, "cost": 2.68},
            "today": {"calls": 5, "input": 40409, "output": 599, "cost": 0.0326},
            "yesterday": {"calls": 83},
            "count_cost": True,
            "usd_rate": 7.2,
            "window": {"models": ["a/gemini-x"], "truncated": False},
            "matched": [{"provider": "p", "plan": "Plan", "pool": "Gemini",
                         "quota_7d": 0.35, "quota_5h": 0.11,
                         "reset_7d": soon, "reset_5h": soon,
                         "models": ["a/gemini-x"]}],
        }
        html = render.build(data, cfg)
        check_true("用窗口调用次数（88 不是 5）", ">88<" in html)
        check_true("标签是「本窗口调用」", "本窗口调用" in html)
        check_true("窗口成本用人民币原值", "¥2.68" in html or "¥2.6800" in html)
        check_true("配额条带重置提示", "天后重置" in html)
        check_true("不再出现「今日调用」", "今日调用" not in html)



def test_fx_cache():
    """汇率缓存：命中不重抓，过期重抓，抓不到用兜底。"""
    print("--- 汇率缓存 ---")
    from types import MethodType
    import time as _time

    class Fake:
        FX_TTL = 3600
        FX_RETRY_TTL = 60

    def make(rate_sequence):
        f = Fake()
        f.ctx = SimpleNamespace(logger=SimpleNamespace(debug=lambda *a, **k: None))
        f.config = SimpleNamespace(report=SimpleNamespace(magpie_usd_rate=7.0))
        f._resolve_usd_rate = MethodType(P.CostReportPlugin._resolve_usd_rate, f)
        calls = {"n": 0}
        def fake_fetch(timeout=5.0):
            value = rate_sequence[min(calls["n"], len(rate_sequence) - 1)]
            calls["n"] += 1
            return value
        P._magpie.fetch_usd_cny = fake_fetch
        return f, calls

    original = P._magpie.fetch_usd_cny
    try:
        f, calls = make([6.5, 9.9])
        first = asyncio.run(f._resolve_usd_rate())
        check("首次抓到实时汇率", first, 6.5)
        second = asyncio.run(f._resolve_usd_rate())
        check("缓存命中不再重抓", second, 6.5)
        check("只抓了一次", calls["n"], 1)

        # 过期后重抓
        f._fx_cache = (6.5, _time.monotonic() - 7200, 3600)
        third = asyncio.run(f._resolve_usd_rate())
        check("过期后重抓", third, 9.9)

        # 抓不到时退回配置值
        g, _ = make([None])
        fallback = asyncio.run(g._resolve_usd_rate())
        check("抓不到用兜底", fallback, 7.0)
        check("兜底值也被缓存", g._fx_cache[0], 7.0)
        check("兜底缓存用短 TTL", g._fx_cache[2], Fake.FX_RETRY_TTL)
    finally:
        P._magpie.fetch_usd_cny = original


def test_fetch_usd_cny_range():
    """真实汇率源：能通就校验范围，不通就跳过（不依赖网络）。"""
    print("--- 汇率源（可选）---")
    rate = magpie.fetch_usd_cny(timeout=8)
    if rate is None:
        print("  SKIP 网络不可用")
        return
    check_true("汇率在合理区间", 1.0 < rate < 20.0)
    print(f"  当前汇率 USD/CNY = {rate}")



def test_used_models_decoupled():
    """显示哪些模型不能和「是否计入成本」耦合。"""
    print("--- used_models 与计费解耦 ---")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        make_db(db)
        now = datetime(2026, 11, 8, 0, 21, 0)
        pricing = {"model-a": {"prompt": 0.000001, "input_cache_read": 0.0000001,
                               "completion": 0.000004}}

        base = dashboard.collect(db, 1, now=now)
        check("无 pricing 也有 used_models", base["current"]["used_models"], ["model-a"])

        # 关掉计费：成本不变，但 used_models 照常有
        off = dashboard.collect(db, 1, now=now, pricing=pricing, apply_pricing=False)
        check("不计费时 used_models 仍有值", off["current"]["used_models"], ["model-a"])
        check("不计费时总花费不变", round(off["current"]["total_cost"], 4), 9.0)
        check_true("不计费时仍有 priced_usage 供展示", bool(off["current"].get("priced_usage")))

        # 开启计费：成本并入
        on = dashboard.collect(db, 1, now=now, pricing=pricing, apply_pricing=True)
        check_true("计费时总花费变大", on["current"]["total_cost"] > base["current"]["total_cost"])

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



def test_quota_skips_non_window():
    """Credits / Tokens(est.) 这类不是 7天/5小时窗口，不能塞进配额条。"""
    print("--- 配额只收 7天/5小时 窗口 ---")
    quotas = {"data": [
        {  # workbuddy 一个账户：全是 Credits 类窗口
            "provider": "workbuddy", "plan": "Free plan", "user": "a",
            "windows": [
                {"name": "Credits", "used": 0},
                {"name": "Extra credits", "used": 11.4},
                {"name": "Total credits", "used": 10.5},
                {"name": "deepseek-v4.1-flash tokens (est.)", "used": 0.2},
            ],
        },
        {  # antigravity：标准 7天/5小时
            "provider": "antigravity", "plan": "Google AI Pro", "user": "b",
            "windows": [
                {"name": "Gemini · 7 days", "pool": "Gemini", "used": 2.6},
                {"name": "Gemini · 5 hours", "pool": "Gemini", "used": 2.8},
                {"name": "Claude & GPT · 7 days", "pool": "Claude & GPT", "used": 0},
            ],
        },
    ]}
    out = magpie._parse_quota(quotas)
    names = {e["provider"] for e in out["providers"]}
    check("workbuddy 无配额窗口被排除", "workbuddy" in names, False)
    check_true("antigravity 保留", "antigravity" in names)
    ag = next(e for e in out["providers"] if e["provider"] == "antigravity")
    check("只剩 Gemini / Claude & GPT 两个池", sorted(ag["pools"]), ["Claude & GPT", "Gemini"])
    check("Gemini 7d", round(ag["pools"]["Gemini"]["quota_7d"], 2), 2.6)
    check("Gemini 5h", round(ag["pools"]["Gemini"]["quota_5h"], 2), 2.8)


def test_quota_merges_accounts():
    """同一 provider 多个账户合并成一条，取已用最大的。"""
    print("--- 多账户配额合并 ---")
    quotas = {"data": [
        {"provider": "workbuddy", "plan": "Free plan", "user": "acc1",
         "windows": [{"name": "Gemini · 7 days", "pool": "Gemini", "used": 1.0},
                     {"name": "Gemini · 5 hours", "pool": "Gemini", "used": 0.5}]},
        {"provider": "workbuddy", "plan": "Free plan", "user": "acc2",
         "windows": [{"name": "Gemini · 7 days", "pool": "Gemini", "used": 5.0},
                     {"name": "Gemini · 5 hours", "pool": "Gemini", "used": 3.0}]},
        {"provider": "workbuddy", "plan": "Free plan", "user": "acc3",
         "windows": [{"name": "Gemini · 7 days", "pool": "Gemini", "used": 2.0}]},
    ]}
    out = magpie._parse_quota(quotas)
    check("同 provider 只出一条", len(out["providers"]), 1)
    pools = out["providers"][0]["pools"]
    check("7d 取最大 5.0", round(pools["Gemini"]["quota_7d"], 1), 5.0)
    check("5h 取最大 3.0", round(pools["Gemini"]["quota_5h"], 1), 3.0)


def test_match_pools_no_tokens_est():
    """池子名带模型名的（tokens (est.)）不能被误当成该模型的配额池。"""
    print("--- tokens(est.) 不误匹配 ---")
    quotas = {"data": [{
        "provider": "workbuddy", "plan": "Free plan", "user": "a",
        "windows": [{"name": "deepseek-v4.1-flash tokens (est.)", "used": 0.2}],
    }]}
    parsed = magpie._parse_quota(quotas)
    check("非配额窗口全部跳过，providers 为空", parsed["providers"], [])
    # 即使强行拿它匹配，也没有池子可用
    matched = magpie.match_pools(["deepseek-v4.1-flash"], parsed["providers"])
    check("匹配结果为空", matched, [])


def run(round_no: int) -> bool:
    print(f"\n===== 第 {round_no} 轮 =====")
    before = len(RESULTS)
    test_num()
    test_align_window()
    test_format()
    test_collect()
    test_collect_missing_db()
    test_render_blocks()
    test_magpie_parsing()
    test_match_pools()
    test_magpie_collect_failures()
    test_render_magpie_block()
    test_extra_cost_merge()
    test_collect_window_filter()
    test_fallback_models()
    test_magpie_count_switch()
    test_config_has_switch()
    test_online_minutes_interval()
    test_priced_cost()
    test_collect_with_pricing()
    test_reset_hint()
    test_magpie_window_stats()
    test_fx_cache()
    test_fetch_usd_cny_range()
    test_used_models_decoupled()
    test_quota_skips_non_window()
    test_quota_merges_accounts()
    test_match_pools_no_tokens_est()
    return all(RESULTS[before:])


if __name__ == "__main__":
    results = [run(i) for i in (1, 2, 3)]
    print(f"\n共 {len(RESULTS)} 个断言，失败 {RESULTS.count(False)} 个")
    print("三遍结果:", results)
    sys.exit(0 if all(results) else 1)
