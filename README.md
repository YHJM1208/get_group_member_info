# 群成员同步管理插件（get_group_member_info）

基于 **NoneBot2 + OneBot v11** 的群成员快照、成员事件流水、事件归档与孤儿群库清理插件。

- 每个群一个独立 SQLite 文件：`{group_db_dir}/{group_id}.db`
- 主表保存当前成员快照，事件表保存入群/退群/改名/管理员变动流水
- 提供命令查询、定时同步、事件缓冲、归档和孤儿群库清理
- 所有命令仅 **SUPERUSER** 可用，并带按群/按用户的冷却

---

## 一、功能概览

| 功能 | 说明 |
|---|---|
| 成员快照 | `group_members` 保存 OneBot v11 成员信息全字段，同步时整群替换 |
| 实时事件 | 入群、退群、群名片变更、管理员变动写入 `member_events` |
| 事件缓冲 | 同步/归档占锁时事件先入内存缓冲，定时批量写入 |
| 事件归档 | 超过保留期的事件移入 `member_events_archive` |
| 孤儿清理 | Bot 不在的群先写 `.db.orphan` 标记，观察期满后删除群库 |
| 状态监控 | `/状态` 查看缓冲、同步失败、引擎缓存、归档和清理状态 |
| 关机保护 | 关闭前先 flush 事件缓冲，再释放全部数据库引擎 |

---

## 二、目录结构

```text
src/plugins/get_group_member_info/
├── __init__.py       # 插件入口：元数据、依赖声明、模块装配、关机钩子
├── config.py         # Config(BaseModel)，全部配置及默认值
├── models.py         # GroupMember / MemberEvent / MemberEventArchive
├── db.py             # 引擎、session、按群可重入锁、orphan 标记、删库
├── sync.py           # 主表全量替换、名片实时更新
├── event_buffer.py   # 事件内存缓冲 + 批量 flush
├── archive.py        # 过期事件归档
├── cleanup.py        # 孤儿群扫描与清理
├── state.py          # 运行状态（内存单例）
├── cooldown.py       # 命令冷却装饰器
├── handlers.py       # 5 个命令 + 5 个通知监听
├── scheduler.py      # 4 个定时任务
└── requirements.txt  # 依赖清单
```

入口文件只做装配，不写 SQL、不写 `await bot.xxx`；命令和事件在 `handlers.py`，定时任务在 `scheduler.py`。

---

## 三、安装与启动

运行环境要求 Python 3.10+（代码使用了 `int | None` 等新语法）。

### 1. 放置插件

把整个 `get_group_member_info` 目录放到 NoneBot 项目的插件目录，通常是：

```text
<项目根目录>/src/plugins/get_group_member_info/
```

`bot.py` 中需要能扫描到该目录，例如：

```python
nonebot.load_plugins("src/plugins")
```

### 2. 安装依赖

```bash
pip install -r src/plugins/get_group_member_info/requirements.txt
```

依赖：

```text
nonebot2>=2.2.0
nonebot-adapter-onebot>=2.4.0
nonebot-plugin-apscheduler>=0.5.0
pydantic>=1.10,<3
sqlalchemy>=2.0.0
aiosqlite>=0.19.0
```

项目里的 `run.sh` 会自动扫描插件目录下的 `requirements*.txt` 并合并安装。

### 3. 配置数据库目录

默认目录在 `config.py`：

```python
group_db_dir: str = "/home/admin/nbbot/data/groups"
```

如果部署路径不同，修改 `config.py` 中的 `group_db_dir` 即可。目录不存在时插件启动会自动创建，运行用户需要有写权限。

### 4. 重启并验证

重启 bot 后，启动日志中应出现：

```text
Succeeded to load plugin "get_group_member_info"
```

然后在群里由超管发送：

```text
/同步成员
```

成功后回复“群 xxxx 同步完成：当前 N 人”，同时生成 `{group_db_dir}/{group_id}.db`。

---

## 四、命令说明

命令前缀以 bot 配置为准，下文默认 `/`。

`limit=N`：`all` 模式默认 20 条，其他模式默认 50 条，最大 100 条。查询类命令共用 `query_cooldown`（默认 10 秒）冷却。

### 1. `/同步成员`

拉取当前群完整成员列表，整体替换主表快照。

```text
/同步成员
```

- 仅 SUPERUSER
- 冷却 `sync_cooldown` 秒（默认 30）
- 拉取失败或成员列表为空时不会清空旧数据

### 2. `/成员查询`

```text
/成员查询 [条件...] [count|all|help] [limit=N]
```

支持字段：

| 字段 | 匹配方式 | 说明 |
|---|---|---|
| `sex` | 精确 | `m`/`male`、`f`/`female`、`u`/`unknown` |
| `role` | 精确 | `owner` / `admin` / `member` |
| `level` | 精确/比较/区间 | 群等级，数据库存字符串，查询时转整数 |
| `age` | 精确/比较/区间 | 年龄 |
| `user_id` | 精确/比较/区间 | QQ 号 |
| `card` | 模糊 | 群名片；名片为空时自动按昵称匹配 |
| `nickname` | 模糊 | 昵称 |

示例：

```text
/成员查询 sex=m count
/成员查询 role=admin
/成员查询 level=>30
/成员查询 level=30-40
/成员查询 age=>20 all
/成员查询 user_id=>10000
/成员查询 card=技术
/成员查询 card="技术 部"
/成员查询 nickname=小 count limit=20
```

### 3. `/事件查询`

```text
/事件查询 [条件...] [count|all|help] [limit=N] [source=live|archive|all]
```

支持字段：

| 字段 | 匹配方式 | 说明 |
|---|---|---|
| `user_id` | 精确/比较/区间 | 成员 QQ |
| `event_type` | 精确 | `increase` / `decrease` / `card` / `admin` |
| `sub_type` | 精确 | 如 `approve` / `invite` / `leave` / `kick` / `set` / `unset` |
| `operator_id` | 精确/比较/区间 | 操作人 QQ |

`source`：

- `live`（默认）：只查 `member_events`
- `archive`：只查 `member_events_archive`
- `all`：两表合并，按事件时间倒序

示例：

```text
/事件查询 user_id=10001
/事件查询 user_id=>10000
/事件查询 user_id=10000-20000
/事件查询 event_type=decrease count
/事件查询 operator_id=>10000
/事件查询 event_type=decrease source=archive
/事件查询 event_type=decrease source=all limit=50
```

### 4. `/状态`

查看插件运行状态：

```text
/状态
```

输出包括：

- 事件缓冲：当前积压数、按群分布、累计推送/缓冲/刷出/丢弃
- 连续同步失败的群（达到 `sync_fail_warn`）
- 最近同步时间及状态
- 当前数据库引擎缓存数量
- 最近归档时间、移动条数
- 最近清理时间、删除群号
- 当前处于观察期的孤儿群数量

### 5. `/清理群库`

```text
/清理群库                     # 帮助
/清理群库 help                # 帮助
/清理群库 <群号>              # 手动删除（要求 Bot 已不在该群）
/清理群库 <群号> force        # 跳过“Bot 仍在群”检查，强制删除
/清理群库 scan                # 扫描孤儿群，仅预览
/清理群库 scan confirm        # 扫描并删除观察期已满的孤儿群
```

孤儿判定：

1. 群库存在，但 Bot 的群列表里没有这个群
2. 第一次发现时写入 `<群号>.db.orphan`，记录时间
3. 超过 `auto_cleanup_grace_days` 天才真正删除
4. 下次扫描发现 Bot 已回到该群时，自动清除标记

获取群列表失败时会保守放弃本次清理，避免误删。

---

## 五、范围查询语法

`level`、`age`、`user_id`、`operator_id` 支持比较和区间，统一写法：

| 写法 | 含义 |
|---|---|
| `field=30` | 等于 30 |
| `field=>30` | 大于 30 |
| `field=>=30` | 大于等于 30 |
| `field=<30` | 小于 30 |
| `field=<=30` | 小于等于 30 |
| `field=30-40` | 30 到 40（含边界） |
| `field=30~40` | 同上 |
| `field>30` | 简写，等价于 `field=>30` |

也支持全角符号：`＞`、`＜`、`≥`、`≤`。

注意：

- `level` 在数据库中是字符串，比较时使用 `CAST(level AS INTEGER)`；空等级成员不会参与范围匹配
- `-` 区间写反会自动排序，如 `40-30` 等价 `30-40`
- `count`、`all`、`limit=N` 可以与范围条件组合

---

## 六、card 模糊匹配说明

`card=` 按“群显示名”匹配：

```text
有群名片  -> 匹配 card
名片为空  -> 匹配 nickname
```

因此没设群名片的人也能用昵称搜到。查询会自动：

- `trim` 首尾空格
- 转义 `%`、`_`，按普通字符匹配
- 支持引号包住含空格的关键词：`card="技术 部"`

只按昵称精确搜索时使用 `nickname=`。

另外，群名片变更事件到达时会立即更新主表中该成员的 `card` 字段（前提是该成员已在快照里），不需要等下一次全量同步；但成员入群/退群仍要等 `/同步成员` 或定时同步后才会反映到主表快照。

---

## 七、配置项

全部在 `config.py` 的 `Config` 中，均有默认值，不依赖 `.env` 即可运行。

| 配置 | 默认值 | 说明 |
|---|---|---|
| `group_db_dir` | `/home/admin/nbbot/data/groups` | 群库目录 |
| `group_db_timeout` | `10` | SQLite 连接超时（秒） |
| `group_db_wal` | `True` | 是否启用 WAL |
| `group_db_max_engines` | `50` | 引擎缓存上限，超出按 LRU 释放 |
| `group_sync_timezone` | `Asia/Shanghai` | 时区 |
| `sync_cooldown` | `30` | `/同步成员` 冷却（秒） |
| `query_cooldown` | `10` | 查询类命令冷却（秒） |
| `auto_sync_hours` | `*/6` | 自动同步 cron 小时表达式 |
| `sync_lock_timeout` | `30` | 同步群锁超时（秒） |
| `event_lock_timeout` | `10` | 事件写入群锁超时（秒） |
| `archive_lock_timeout` | `60` | 归档群锁超时（秒） |
| `event_retention_days` | `180` | 事件保留天数 |
| `archive_cron_day` | `1` | 归档日（每月） |
| `archive_cron_hour` | `3` | 归档小时 |
| `event_buffer_max` | `100` | 单群事件缓冲上限 |
| `event_flush_interval` | `30` | 缓冲 flush 间隔（秒） |
| `sync_fail_warn` | `3` | 连续失败告警阈值 |
| `auto_cleanup_enabled` | `True` | 是否启用自动清理 |
| `auto_cleanup_cron_day` | `15` | 自动清理日（每月） |
| `auto_cleanup_cron_hour` | `4` | 自动清理小时 |
| `auto_cleanup_grace_days` | `7` | 孤儿群观察期（天） |

---

## 八、定时任务

| 任务 ID | 触发 | 作用 |
|---|---|---|
| `auto_sync_members` | cron `hour=*/6` | 遍历所有 bot 的群，拉取成员并同步主表 |
| `flush_event_buffer` | 每 30 秒 | 刷出内存中的事件缓冲 |
| `archive_events` | 每月 1 日 03:00 | 把超过 180 天的事件移入归档表 |
| `auto_cleanup_orphan_groups` | 每月 15 日 04:00 | 扫描并删除观察期满的孤儿群库 |

自动同步会在群之间 `sleep(2)`，降低协议端风控概率。

---

## 九、数据模型

### `group_members`（当前成员快照）

联合主键 `(group_id, user_id)`，包含 OneBot 成员信息：

`nickname`、`card`、`sex`、`age`、`area`、`join_time`、`last_sent_time`、`level`、`role`、`unfriendly`、`title`、`title_expire_time`、`card_changeable`、`shut_up_timestamp`、`is_robot`、`updated_at`。

主表是纯快照：退群成员在下一次同步时被删除，不保留 `in_group` 标记。

### `member_events`（实时事件流水）

字段：`id`、`group_id`、`user_id`、`event_type`、`sub_type`、`operator_id`、`card_old`、`card_new`、`raw_json`、`event_time`。

唯一索引 `(group_id, user_id, event_type, event_time)` + `ON CONFLICT DO NOTHING`，协议端重放同一事件不会写入第二条。

### `member_events_archive`（历史事件）

结构与 `member_events` 相同，`event_time` 保留原值。归档后可用 `/事件查询 source=archive` 查询。

---

## 十、运行机制

### 按群隔离

- 每个群一个 SQLite 文件，群之间互不影响
- 引擎按群懒创建，超过 `group_db_max_engines` 后按 LRU 释放
- 群锁是进程内 `asyncio.Lock`，通过 `contextvars` 支持同一协程重入
- 不支持多进程部署（多进程下锁不共享）

### 事件写入

```text
事件到达
  ├─ 无积压：拿群锁 -> 直接 INSERT -> 成功
  └─ 有积压/锁超时：进内存缓冲 -> 定时 flush
```

缓冲超过 `event_buffer_max` 时丢弃最旧事件，并计入 `/状态` 的丢弃数。

### Bot 被移出群

Bot 自己被移出群时：

- 不写入 `decrease` 事件
- 立即释放该群的数据库引擎缓存
- 群库文件保留，交给孤儿群清理流程处理

### 归档

1. 查询 `event_time < now - event_retention_days` 的事件
2. 复制到 `member_events_archive`
3. 从 `member_events` 删除
4. 更新 `/状态` 中的归档时间和条数

归档和同步使用同一把群锁，避免并发写冲突。

### 关机

`__init__.py` 注册 `on_shutdown`：

1. `flush_all()` 尝试写完缓冲
2. 输出剩余未写条数
3. `close_all()` 释放全部引擎

---

## 十一、常见问题

### 1. 插件加载失败：`Cannot import name 'GroupCardNoticeEvent'`

OneBot v11 适配器没有内置 `GroupCardNoticeEvent`，群名片通知会被解析为基类 `NoticeEvent`。当前实现已改为监听 `NoticeEvent` 并判断 `notice_type == "group_card"`，通过扩展字段读取群号、QQ、新旧名片。如果仍报这个错，说明服务器上的 `handlers.py` 不是最新版本。

### 2. `Module nonebot_plugin_apscheduler is not loaded as a plugin!`

通常是插件加载顺序导致的：其他插件先直接 `import nonebot_plugin_apscheduler`，NoneBot 之后再 `require` 就会报这个错。

当前入口已做保护：

```python
if "nonebot_plugin_apscheduler" not in sys.modules:
    require("nonebot_plugin_apscheduler")
```

同时建议其他直接 import apscheduler 的插件也先 `require` 再 import。

### 3. 查询报 `查询失败：FinishedException()`

`Matcher.finish()` 依靠抛 `FinishedException` 结束处理，不能放在会被 `except Exception` 捕获的 `try` 块里。当前 `/成员查询`、`/事件查询` 已显式放行该异常；如果仍出现，检查是否有旧版 `handlers.py`。

### 4. card 查不到某人

按顺序检查：

1. 是否执行过 `/同步成员`，主表是否有该群数据
2. 对方是否有群名片；没名片时插件会回退匹配昵称
3. 关键词是否含空格，含空格时用引号：`card="技术 部"`
4. 名片是否是最近才改的；当前版本会实时更新，旧版本需等同步
5. 是否含全角/半角差异或特殊字符，可把名片和完整命令发出来排查

### 5. `/同步成员` 提示群正在同步/归档

群锁被占用，等当前同步或归档结束后重试。锁超时时间由 `sync_lock_timeout` 控制。

### 6. 群库目录没有生成

检查 `group_db_dir` 是否存在写权限，以及 bot 运行用户是否与目录 owner 一致。

---

## 十二、部署注意事项

1. **单进程运行**：群锁和事件缓冲都是进程内状态，不要用多 worker 启动同一个 bot。
2. **磁盘空间**：每个群一个库，长期运行会增长；归档只搬表，不删库，必要时结合 `/清理群库`。
3. **备份**：直接备份 `{group_db_dir}/*.db` 即可；WAL 模式下建议先停 bot 或同时备份 `-wal`/`-shm` 文件。
4. **权限**：所有命令仅 SUPERUSER，普通群成员和管理员无法触发。
5. **协议端能力**：成员列表、群列表依赖 OneBot 实现；`get_group_member_list` 失败时不会清空旧快照。
6. **事件补录**：Bot 离线期间的事件无法补录，定时同步只能修正主表，不能补事件流水。

---

## 十三、命令速查

```text
/同步成员

/成员查询 [sex=m|f|u] [role=owner|admin|member] [level=N] [age=N]
          [user_id=N] [card=关键词] [nickname=关键词]
          [count|all|help] [limit=N]

/成员查询 level=>30
/成员查询 level=30-40
/成员查询 age=>20 count
/成员查询 user_id=10000-20000
/成员查询 card="技术 部"

/事件查询 [user_id=N] [event_type=increase|decrease|card|admin]
          [sub_type=approve|invite|leave|kick|set|unset]
          [operator_id=N] [count|all|help] [limit=N]
          [source=live|archive|all]

/事件查询 user_id=>10000
/事件查询 operator_id=10000-20000
/事件查询 event_type=decrease count
/事件查询 event_type=decrease source=archive

/状态

/清理群库
/清理群库 help
/清理群库 <群号> [force]
/清理群库 scan [confirm]
```

范围写法通用：`>N`、`>=N`、`<N`、`<=N`、`N-M`、`N~M`，也支持 `field>N` 简写和全角符号。

## LICEMSE
MIT
