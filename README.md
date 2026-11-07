# 开销统计

麦麦（MaiBot）插件。查麦麦自己烧了多少 token、花了多少钱、缓存命中率怎么样。

## 功能

| 功能 | 触发方式 |
| --- | --- |
| 查开销 | `/开销 [天数]` 命令，渲染成一张卡片图 |
| LLM 自查 | `check_cost` 工具，用户问「今天花了多少钱」时由模型自己调 |

数据来自宿主 `statistics.local.models` 能力，底层是 `statistics_model_hourly` 聚合表。

## 安装

1. 把仓库内容放进 `MaiBot/plugins/maibot-cost-report/`，目录里必须有 `_manifest.json` 和 `plugin.py`
2. 在 WebUI 插件页启用，或重启麦麦
3. 首次加载会自动生成 `config.toml`

## 要求

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| MaiBot | 1.3.x / 1.4.x | manifest 里声明的区间是 `1.3.0` ～ `1.4.99` |
| `maibot-plugin-sdk` | 2.4.0 起 | 插件用了 2.4.0 才引入的 `PluginConfigBase`，再低的版本会在 `import` 阶段就失败。2.21.0 已验证 |

SDK 版本超出 manifest 区间时宿主只记 warning、仍会加载，所以下界不要写低——写在 2.0.0 的话，2.0～2.3 的环境里表现是「加载成功然后 ImportError」。

## 配置

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `report.enabled` | `true` | 是否允许查询 |
| `report.currency` | `元` | 金额单位 |
| `report.top_models` | `8` | 按花费列出前几个模型 |
| `report.max_days` | `30` | 允许查询的最大天数，防止 `/开销 9999` 扫表 |
| `report.show_cache` | `true` | 是否显示缓存命中率 |
| `report.output_mode` | `image` | `text` / `image` / `both`，渲染失败会自动退回文本 |
| `report.title` | `麦麦开销` | 卡片图标题，留空则只显示时间范围 |
| `report.tool_enabled` | `true` | 是否给 LLM 提供查询工具 |
| `report.tool_send_image` | `true` | 工具调用时是否同时发图 |
| `report.db_path` | 空 | 宿主数据库路径，仅缓存命中率用。留空按「插件数据目录往上两级」推算；支持 `~` 和相对路径，内部会转成绝对路径 |

## 命令

| 命令 | 说明 |
| --- | --- |
| `/开销` | 最近 24 小时 |
| `/开销 7` | 最近 7 天，上限由 `max_days` 控制 |
| `/开销7` `/开销 7天` | 同上，空格和「天」字都可省 |
| `/成本` `/花费` `/cost` | 别名 |

## 关于「最近 24 小时」

宿主的统计窗口按 `now - N 天` 计算，没有自然日边界，所以 `days=1` 得到的是**滚动 24 小时**而不是今天 0 点至今。凌晨查会带上昨天的一部分，白天基本等于今天。

要精确到自然日只能自己扫 `llm_usage` 明细，代价大得多，这里没做。

另外，聚合表按**整点**分桶（`bucket_time >= since` 的第一个桶是 `ceil(since)`），所以缓存命中率那条明细查询也统一从 `ceil(since)` 起算，保证卡片上的「花费」和「缓存命中」是同一个时间窗口。

宿主按**调用次数**降序返回模型，最多 50 个。插件会取满 50 个再按花费排序，尽量避免漏掉「调用少但单价贵」的模型；真的超过 50 个时，卡片页脚会注明「按调用次数取前 50 个模型汇总」。

## 关于缓存命中率

聚合表里没有缓存字段，命中率只能从 `llm_usage` 明细算。插件会用**只读模式**（`mode=ro`）打开宿主数据库，只执行一条 `SELECT ... GROUP BY`，不写入、不改结构。

需要说明的是，这条路径**在宿主的能力体系之外**——宿主没有「读数据库文件」这种能力，`database.query` 只支持按模型类做等值查询，没有聚合也没有 SQL 直通，所以只能自己开文件。它依赖三个宿主没有承诺过的内部细节：

1. 数据库文件名是 `MaiBot.db`
2. 它位于「插件数据目录往上两级」（`data/plugins/<id>` → `data/MaiBot.db`）
3. `llm_usage` 表里有 `model_assign_name`、`prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`、`timestamp` 这四列

宿主一旦调整目录布局或表结构，这个功能会失效。失效时的表现是卡片上「缓存命中」显示 `—` 并在下面提示检查「数据库路径」，日志里也会有一条 `warning`，不会静默。如果路径推算不对，把 `report.db_path` 填成实际的 `MaiBot.db` 绝对路径即可。

如果不想让插件碰数据库，把 `report.show_cache` 关掉即可，此时只走官方能力，完全不读文件。

## 常见问题

**图里的数字和模型配置页对不上？**
卡片上的模型名来自 `statistics_model_hourly.model_name`，就是配置里的模型名（如 `wb-proxy/deepseek-v4.1-flash`），不是 `llm_usage.model_name` 里那个 `model_identifier`（`deepseek-v4.1-flash`）。缓存命中率另外按 `llm_usage.model_assign_name` 分组匹配，两者实测同名；真出现对不上的情况，命中率那一列会显示 `—`，不影响花费和 token。

**缓存命中率显示「—」？**
卡片上会同时提示「读不到 llm_usage，检查数据库路径」。先看日志里那条 `数据库文件不存在` 的 warning，路径不对就把 `report.db_path` 填成 `MaiBot.db` 的绝对路径；路径对但还是「—」，可能是该窗口内没有缓存字段数据，或者数据库被别的进程长时间锁住。

**渲染失败？**
会自动退回纯文本，日志里会有一条「统计图渲染失败」。常见原因是 Playwright 浏览器没装好。

**能查某个具体模型吗？**
目前不支持，只按花费降序列出前 N 个。

## 许可

本插件代码采用 MIT，全文见 [LICENSE](./LICENSE)。

运行时依赖与边界：

| 组件 | 许可证 | 关系 |
| --- | --- | --- |
| `maibot-plugin-sdk` | LGPL-3.0-only | 仅 `import` 使用，未修改、未内嵌其源码 |
| MaiBot 主程序 | GPL-3.0 | 通过 SDK 提供的 IPC 接口通信，插件不含主程序代码，也不 `import src.*` |

插件运行在独立的 Runner 子进程里，与宿主是进程间通信关系，不构成对主程序的衍生作品；LGPL 的 SDK 属于动态链接使用，因此插件本身可以采用 MIT。

需要单独说明的是缓存命中率那条路径：它用 `sqlite3` 只读打开宿主生成的 `MaiBot.db` 读取统计数值，读取的是运行时数据文件，不是主程序代码，也不修改库结构。介意这一点可以把 `report.show_cache` 关掉，插件就不再碰任何数据库文件。

仓库内的图标为自制，没有使用第三方素材。
