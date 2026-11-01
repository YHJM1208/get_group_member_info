"""命令与事件处理器。

包含 5 个管理命令（同步成员 / 成员查询 / 事件查询 / 状态 / 清理群库），
以及入群、退群、改名、管理员变动和 Bot 被移出群的通知监听。
"""

import json
import re
import shlex
from datetime import datetime
from zoneinfo import ZoneInfo

from nonebot import get_bots, get_plugin_config, on_command, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupAdminNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    Message,
    NoticeEvent,
)
from nonebot.exception import FinishedException
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from sqlalchemy import Integer, cast, func, select

from .cleanup import execute_cleanup, scan_orphan_groups
from .config import Config
from .cooldown import cooldown
from .db import (
    delete_group_db,
    dispose_group,
    engine_count,
    get_known_groups,
    get_orphan_marker,
    get_session,
    init_group_db,
)
from .event_buffer import pending_by_group, pending_count, push_event
from .models import GroupMember, MemberEvent, MemberEventArchive
from .state import failing_groups, get_state
from .sync import save_group_members, update_member_card

plugin_config = get_plugin_config(Config)
_state = get_state()
_TZ = ZoneInfo(plugin_config.group_sync_timezone)


MAX_LIMIT = 100
DEFAULT_LIMIT_ALL = 20
DEFAULT_LIMIT_COMPACT = 50


def _resolve_limit(raw: str | None, mode: str) -> int:
    if raw is None:
        return (
            DEFAULT_LIMIT_ALL if mode == "all" else DEFAULT_LIMIT_COMPACT
        )
    try:
        n = int(raw)
    except ValueError:
        raise ValueError("limit 必须是整数")
    if n <= 0:
        raise ValueError("limit 必须大于 0")
    return min(n, MAX_LIMIT)


_RANGE_RE = re.compile(r"^(\d+)\s*[-~]\s*(\d+)$")
_COMPARE_RE = re.compile(r"^(>=|<=|>|<|=)?\s*(\d+)$")
_SHORTHAND_RE = re.compile(
    r"^(user_id|operator_id|level|age)\s*(>=|<=|>|<)\s*(\d+)$",
    re.IGNORECASE,
)
_FULLWIDTH_OPS = str.maketrans({"＞": ">", "＜": "<", "≥": ">=", "≤": "<="})


def _parse_number_condition(
    raw: str,
) -> tuple[str, int, int | None] | None:
    """解析数值条件：30 / >30 / >=30 / <30 / <=30 / 30-40 / 30~40。"""
    text = raw.strip().translate(_FULLWIDTH_OPS)
    range_match = _RANGE_RE.fullmatch(text)
    if range_match:
        lo, hi = int(range_match.group(1)), int(range_match.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return ("range", lo, hi)
    compare_match = _COMPARE_RE.fullmatch(text)
    if compare_match:
        return (
            compare_match.group(1) or "=",
            int(compare_match.group(2)),
            None,
        )
    return None


def _apply_number_condition(stmt, column, condition):
    op, lo, hi = condition
    if op == "range":
        return stmt.where(column.between(lo, hi))
    if op == ">":
        return stmt.where(column > lo)
    if op == ">=":
        return stmt.where(column >= lo)
    if op == "<":
        return stmt.where(column < lo)
    if op == "<=":
        return stmt.where(column <= lo)
    return stmt.where(column == lo)


def _number_condition_help(field: str) -> str:
    return (
        f"{field} 支持精确值、比较或区间，例如：\n"
        f"  {field}=30  {field}=>30  {field}=>=30  "
        f"{field}=<30  {field}=30-40\n"
        f"也支持简写：{field}>30"
    )


def _split_args(text: str) -> list[str]:
    """按空白切分参数，支持用引号包住含空格的值。

    例：card="技术 部" count -> ['card=技术 部', 'count']
    引号不配对时退回普通 split，避免因为名字里有引号而直接报错。
    """
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _member_compact_line(
    m: GroupMember, conditions: dict[str, str]
) -> str:
    """默认精简列表：查询了什么字段就展示什么字段。"""
    if conditions:
        details = []
        for key in conditions:
            if key == "user_id":
                continue
            value = getattr(m, key, "")
            if value is None:
                value = ""
            if key == "card" and not value:
                value = "无"
            details.append(f"{key}={value}")
        detail_text = " ".join(details)
    else:
        detail_text = f"sex={m.sex} role={m.role}"
    line = f"{m.user_id} {m.card or m.nickname}"
    return f"{line} ({detail_text})" if detail_text else line


async def _record_event(
    group_id: int,
    user_id: int,
    event_type: str,
    event_ts: int | None = None,
    sub_type: str = "",
    operator_id: int | None = None,
    card_old: str = "",
    card_new: str = "",
    raw_json: str = "",
):
    et = (
        datetime.fromtimestamp(event_ts, _TZ)
        if event_ts
        else datetime.now(_TZ)
    )
    await push_event(group_id, {
        "group_id": group_id,
        "user_id": user_id,
        "event_type": event_type,
        "sub_type": sub_type,
        "operator_id": operator_id,
        "card_old": card_old,
        "card_new": card_new,
        "raw_json": raw_json,
        "event_time": et,
    })


def _dump(event) -> str:
    try:
        return json.dumps(
            event.model_dump(), ensure_ascii=False, default=str
        )
    except Exception:
        try:
            return json.dumps(
                event.dict(), ensure_ascii=False, default=str
            )
        except Exception:
            return ""


# ==================== /同步成员 ====================
cmd = on_command("同步成员", permission=SUPERUSER)


@cmd.handle()
@cooldown(plugin_config.sync_cooldown, "同步成员")
async def _(bot: Bot, event: GroupMessageEvent):
    try:
        members = await bot.get_group_member_list(group_id=event.group_id)
    except Exception as e:
        logger.warning(f"群 {event.group_id} 获取成员列表失败: {e}")
        await cmd.finish("获取成员列表失败，请稍后重试或检查协议端状态")

    if not members:
        await cmd.finish(
            "获取到空成员列表，已跳过同步（防止清空数据库）"
        )

    try:
        result = await save_group_members(event.group_id, members)
    except RuntimeError as e:
        await cmd.finish(str(e))
    except Exception as e:
        logger.exception(f"群 {event.group_id} 同步写入失败")
        await cmd.finish(f"同步写入失败：{e}")

    await cmd.send(
        f"群 {event.group_id} 同步完成：当前 {result['total']} 人"
    )


# ==================== 实时事件监听 ====================
increase = on_notice()


@increase.handle()
async def _(event: GroupIncreaseNoticeEvent):
    await _record_event(
        group_id=event.group_id,
        user_id=event.user_id,
        event_type="increase",
        event_ts=getattr(event, "time", None),
        sub_type=getattr(event, "sub_type", ""),
        operator_id=getattr(event, "operator_id", None),
        raw_json=_dump(event),
    )


decrease = on_notice()


@decrease.handle()
async def _(event: GroupDecreaseNoticeEvent):
    if event.user_id == int(event.self_id):
        return
    await _record_event(
        group_id=event.group_id,
        user_id=event.user_id,
        event_type="decrease",
        event_ts=getattr(event, "time", None),
        sub_type=getattr(event, "sub_type", ""),
        operator_id=getattr(event, "operator_id", None),
        raw_json=_dump(event),
    )


card = on_notice()


@card.handle()
async def _(event: NoticeEvent):
    # OneBot v11 适配器未内置 GroupCardNoticeEvent，group_card 通知会被解析为
    # NoticeEvent（基类 extra=allow，群号/QQ/新旧名片保留在扩展字段中）。
    if getattr(event, "notice_type", None) != "group_card":
        return
    group_id = getattr(event, "group_id", None)
    user_id = getattr(event, "user_id", None)
    if not group_id or not user_id:
        logger.warning("收到 group_card 通知但缺少群号/QQ，已跳过")
        return
    card_new = getattr(event, "card_new", "") or ""
    await _record_event(
        group_id=group_id,
        user_id=user_id,
        event_type="card",
        event_ts=getattr(event, "time", None),
        card_old=getattr(event, "card_old", ""),
        card_new=card_new,
        raw_json=_dump(event),
    )
    # 主表是快照，名片变更后立即更新，避免下一次全量同步前按新名片查不到
    await update_member_card(group_id, user_id, card_new)


admin = on_notice()


@admin.handle()
async def _(event: GroupAdminNoticeEvent):
    await _record_event(
        group_id=event.group_id,
        user_id=event.user_id,
        event_type="admin",
        event_ts=getattr(event, "time", None),
        sub_type=getattr(event, "sub_type", ""),
        raw_json=_dump(event),
    )


# ==================== /事件查询 ====================
EVENT_COND_FIELDS = {"user_id", "event_type", "sub_type", "operator_id"}

EVENT_HELP_TEXT = (
    "用法：\n"
    "  /事件查询 user_id=10001              → 查某人最近事件\n"
    "  /事件查询 user_id=>10000             → QQ 号大于 10000\n"
    "  /事件查询 user_id=10000-20000        → QQ 号区间\n"
    "  /事件查询 event_type=decrease        → 所有退群事件\n"
    "  /事件查询 event_type=decrease count  → 退群数量\n"
    "  /事件查询 operator_id=>10000         → 操作人 QQ 大于 10000\n"
    "  /事件查询 source=archive             → 查归档表\n"
    "  /事件查询 source=all                 → 活跃 + 归档合并\n"
    "  /事件查询 event_type=decrease limit=50\n"
    "字段：user_id / event_type / sub_type / operator_id\n"
    "范围：user_id / operator_id 支持 >、>=、<、<= 和区间"
    "（如 user_id=>10000、user_id=10000-20000）\n"
    "模式：count / all / 默认 / help\n"
    "source：live（默认）/ archive / all\n"
    f"limit=N：默认 {DEFAULT_LIMIT_ALL}/{DEFAULT_LIMIT_COMPACT}，"
    f"上限 {MAX_LIMIT}"
)

event_query = on_command("事件查询", permission=SUPERUSER)


@event_query.handle()
@cooldown(plugin_config.query_cooldown, "事件查询")
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    text = args.extract_plain_text().strip()
    if not text:
        await event_query.finish(EVENT_HELP_TEXT)

    parts = _split_args(text)
    conditions: dict[str, str] = {}
    mode = "default"
    source = "live"
    limit_raw: str | None = None
    has_help = False

    for p in parts:
        shorthand = _SHORTHAND_RE.fullmatch(p)
        if shorthand and shorthand.group(1).lower() in EVENT_COND_FIELDS:
            conditions[shorthand.group(1).lower()] = (
                f"{shorthand.group(2)}{shorthand.group(3)}"
            )
            continue
        if "=" in p:
            k, v = p.split("=", 1)
            k = k.strip().lower()
            v = v.strip().strip("'\"").strip()
            if k == "limit":
                limit_raw = v
                continue
            if k == "source":
                if v not in ("live", "archive", "all"):
                    await event_query.finish(
                        "source 只支持 live / archive / all"
                    )
                source = v
                continue
            if k not in EVENT_COND_FIELDS:
                await event_query.finish(
                    f"不支持的字段：{k}\n"
                    f"支持：{', '.join(sorted(EVENT_COND_FIELDS))}"
                )
            conditions[k] = v
        elif p in ("count", "all"):
            mode = p
        elif p.lower() == "help":
            has_help = True
        else:
            await event_query.finish(f"无法识别的参数：{p}")

    if has_help:
        await event_query.finish(EVENT_HELP_TEXT)

    try:
        display_limit = _resolve_limit(limit_raw, mode)
    except ValueError as e:
        await event_query.finish(str(e))

    numeric_conditions: dict[str, tuple[str, int, int | None]] = {}
    for field in ("user_id", "operator_id"):
        if field in conditions:
            parsed = _parse_number_condition(conditions[field])
            if parsed is None:
                await event_query.finish(_number_condition_help(field))
            numeric_conditions[field] = parsed

    def _apply(stmt, model):
        if "user_id" in numeric_conditions:
            stmt = _apply_number_condition(
                stmt, model.user_id, numeric_conditions["user_id"]
            )
        if "event_type" in conditions:
            stmt = stmt.where(
                model.event_type == conditions["event_type"]
            )
        if "sub_type" in conditions:
            stmt = stmt.where(model.sub_type == conditions["sub_type"])
        if "operator_id" in numeric_conditions:
            stmt = _apply_number_condition(
                stmt, model.operator_id, numeric_conditions["operator_id"]
            )
        return stmt

    try:
        await init_group_db(event.group_id)
        Session = await get_session(event.group_id)
        async with Session() as session:
            live_stmt = _apply(
                select(MemberEvent).where(
                    MemberEvent.group_id == event.group_id
                ),
                MemberEvent,
            )
            arch_stmt = _apply(
                select(MemberEventArchive).where(
                    MemberEventArchive.group_id == event.group_id
                ),
                MemberEventArchive,
            )

            if mode == "count":
                total = 0
                if source in ("live", "all"):
                    total += await session.scalar(
                        select(func.count()).select_from(
                            live_stmt.subquery()
                        )
                    ) or 0
                if source in ("archive", "all"):
                    total += await session.scalar(
                        select(func.count()).select_from(
                            arch_stmt.subquery()
                        )
                    ) or 0
                cond_desc = (
                    " ".join(f"{k}={v}" for k, v in conditions.items())
                    or "全部"
                )
                await event_query.finish(
                    f"条件 [{cond_desc}] source={source} 匹配 {total} 条事件"
                )
                return

            rows: list = []
            if source in ("live", "all"):
                res = await session.execute(
                    live_stmt.order_by(
                        MemberEvent.event_time.desc()
                    ).limit(display_limit)
                )
                rows.extend(("live", r) for r in res.scalars().all())
            if source in ("archive", "all"):
                res = await session.execute(
                    arch_stmt.order_by(
                        MemberEventArchive.event_time.desc()
                    ).limit(display_limit)
                )
                rows.extend(("archive", r) for r in res.scalars().all())
    except FinishedException:
        # finish() 依靠抛异常结束处理，不能被下面的 except Exception 吞掉
        raise
    except Exception as e:
        logger.exception(f"事件查询失败 group={event.group_id}")
        await event_query.finish(f"查询失败：{e}")

    cond_desc = (
        " ".join(f"{k}={v}" for k, v in conditions.items()) or "全部"
    )

    if not rows:
        await event_query.finish(
            f"条件 [{cond_desc}] source={source} 无事件记录"
        )

    if source == "all":
        rows.sort(key=lambda x: x[1].event_time, reverse=True)
        rows = rows[:display_limit]

    if mode == "all":
        lines = []
        for src, r in rows:
            raw_preview = (r.raw_json or "")[:60].replace("\n", " ")
            lines.append(
                f"[{src}] id={r.id} user={r.user_id} "
                f"type={r.event_type}/{r.sub_type} op={r.operator_id} "
                f"card={r.card_old}→{r.card_new} "
                f"time={r.event_time:%Y-%m-%d %H:%M:%S} raw={raw_preview}"
            )
    else:
        lines = []
        for src, r in rows:
            d = f"{r.event_time:%Y-%m-%d %H:%M:%S} [{src}] {r.event_type}"
            if r.sub_type:
                d += f"/{r.sub_type}"
            d += f" user={r.user_id}"
            if r.card_old or r.card_new:
                d += f" {r.card_old}→{r.card_new}"
            if r.operator_id:
                d += f" operator={r.operator_id}"
            lines.append(d)

    header = (
        f"条件 [{cond_desc}] source={source} 共 {len(rows)} 条"
        f"（最多显示 {display_limit} 条）"
    )
    await event_query.send(header + "\n" + "\n".join(lines))


# ==================== /成员查询 ====================
SEX_ALIAS = {
    "m": "male", "male": "male",
    "f": "female", "female": "female",
    "u": "unknown", "unknown": "unknown",
}
MEMBER_COND_FIELDS = {
    "sex", "role", "level", "age", "card", "nickname", "user_id",
}

MEMBER_HELP_TEXT = (
    "用法：\n"
    "  /成员查询 sex=m count      → 男性数量\n"
    "  /成员查询 sex=f all        → 女性全字段\n"
    "  /成员查询 sex=u            → 性别未知\n"
    "  /成员查询 role=admin       → 管理员精简列表\n"
    "  /成员查询 level=>30        → 等级大于 30\n"
    "  /成员查询 level=30-40      → 等级 30~40\n"
    "  /成员查询 age=>20          → 年龄大于 20\n"
    "  /成员查询 user_id=>10000   → QQ 号大于 10000\n"
    "  /成员查询 card=技术        → 名片含「技术」（无名片时按昵称）\n"
    "  /成员查询 nickname=小      → 昵称含「小」（模糊）\n"
    "  /成员查询 card=\"技术 部\"   → 含空格的关键词加引号\n"
    "  /成员查询 user_id=10001    → 精确查某人\n"
    "字段：sex(m/f/u) / role / level / age / card / nickname / user_id\n"
    "范围：level / age / user_id 支持 >、>=、<、<= 和区间"
    "（如 level=>30、level=30-40）\n"
    "模式：count / all / 默认 / help\n"
    f"limit=N：默认 {DEFAULT_LIMIT_ALL}/{DEFAULT_LIMIT_COMPACT}，"
    f"上限 {MAX_LIMIT}"
)

member_query = on_command("成员查询", permission=SUPERUSER)


@member_query.handle()
@cooldown(plugin_config.query_cooldown, "成员查询")
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    text = args.extract_plain_text().strip()
    if not text:
        await member_query.finish(MEMBER_HELP_TEXT)

    parts = _split_args(text)
    conditions: dict[str, str] = {}
    mode = "default"
    limit_raw: str | None = None
    has_help = False

    for p in parts:
        shorthand = _SHORTHAND_RE.fullmatch(p)
        if shorthand and shorthand.group(1).lower() in MEMBER_COND_FIELDS:
            conditions[shorthand.group(1).lower()] = (
                f"{shorthand.group(2)}{shorthand.group(3)}"
            )
            continue
        if "=" in p:
            k, v = p.split("=", 1)
            k = k.strip().lower()
            v = v.strip().strip("'\"").strip()
            if k == "limit":
                limit_raw = v
                continue
            if k not in MEMBER_COND_FIELDS:
                await member_query.finish(
                    f"不支持的字段：{k}\n"
                    f"支持：{', '.join(sorted(MEMBER_COND_FIELDS))}"
                )
            conditions[k] = v
        elif p in ("count", "all"):
            mode = p
        elif p.lower() == "help":
            has_help = True
        else:
            await member_query.finish(f"无法识别的参数：{p}")

    if has_help:
        await member_query.finish(MEMBER_HELP_TEXT)

    if "sex" in conditions:
        sex_raw = conditions["sex"].lower()
        if sex_raw not in SEX_ALIAS:
            await member_query.finish("sex 只支持 m / f / u")
        conditions["sex"] = SEX_ALIAS[sex_raw]

    numeric_conditions: dict[str, tuple[str, int, int | None]] = {}
    for field in ("level", "age", "user_id"):
        if field in conditions:
            parsed = _parse_number_condition(conditions[field])
            if parsed is None:
                await member_query.finish(_number_condition_help(field))
            numeric_conditions[field] = parsed

    try:
        display_limit = _resolve_limit(limit_raw, mode)
    except ValueError as e:
        await member_query.finish(str(e))

    try:
        await init_group_db(event.group_id)
        Session = await get_session(event.group_id)
        async with Session() as session:
            stmt = select(GroupMember).where(
                GroupMember.group_id == event.group_id
            )
            if "sex" in conditions:
                stmt = stmt.where(
                    GroupMember.sex == conditions["sex"]
                )
            if "role" in conditions:
                stmt = stmt.where(
                    GroupMember.role == conditions["role"]
                )
            if "level" in numeric_conditions:
                stmt = stmt.where(GroupMember.level != "")
                stmt = _apply_number_condition(
                    stmt,
                    cast(GroupMember.level, Integer),
                    numeric_conditions["level"],
                )
            if "age" in numeric_conditions:
                stmt = _apply_number_condition(
                    stmt,
                    GroupMember.age,
                    numeric_conditions["age"],
                )
            if "card" in conditions:
                # 名片为空（或只有空白）时回退到昵称，避免没设名片的人搜不到；
                # trim 处理首尾空格，autoescape 让 %/_ 按普通字符匹配。
                display_name = func.coalesce(
                    func.nullif(func.trim(GroupMember.card), ""),
                    GroupMember.nickname,
                    "",
                )
                stmt = stmt.where(
                    display_name.contains(
                        conditions["card"], autoescape=True
                    )
                )
            if "nickname" in conditions:
                stmt = stmt.where(
                    GroupMember.nickname.contains(
                        conditions["nickname"], autoescape=True
                    )
                )
            if "user_id" in numeric_conditions:
                stmt = _apply_number_condition(
                    stmt,
                    GroupMember.user_id,
                    numeric_conditions["user_id"],
                )

            if mode == "count":
                total = await session.scalar(
                    select(func.count()).select_from(stmt.subquery())
                )
                cond_desc = (
                    " ".join(
                        f"{k}={v}" for k, v in conditions.items()
                    )
                    or "全部"
                )
                await member_query.finish(
                    f"条件 [{cond_desc}] 匹配 {total} 人"
                )
                return

            result = await session.execute(stmt.limit(display_limit))
            members = result.scalars().all()
    except FinishedException:
        # finish() 依靠抛异常结束处理，不能被下面的 except Exception 吞掉
        raise
    except Exception as e:
        logger.exception(f"成员查询失败 group={event.group_id}")
        await member_query.finish(f"查询失败：{e}")

    cond_desc = (
        " ".join(f"{k}={v}" for k, v in conditions.items()) or "全部"
    )
    if not members:
        await member_query.finish(f"条件 [{cond_desc}] 无匹配成员")

    if mode == "all":
        lines = [
            f"user_id={m.user_id} nickname={m.nickname} card={m.card} "
            f"sex={m.sex} age={m.age} area={m.area} role={m.role} "
            f"level={m.level} join_time={m.join_time} "
            f"last_sent_time={m.last_sent_time} title={m.title} "
            f"title_expire_time={m.title_expire_time} "
            f"card_changeable={m.card_changeable} "
            f"shut_up_timestamp={m.shut_up_timestamp} "
            f"is_robot={m.is_robot} unfriendly={m.unfriendly}"
            for m in members
        ]
    else:
        lines = [_member_compact_line(m, conditions) for m in members]

    header = f"条件 [{cond_desc}] 共 {len(members)} 人"
    if len(members) >= display_limit:
        header += f"（最多显示 {display_limit} 条，可用 limit=N 调整）"
    await member_query.send(header + "\n" + "\n".join(lines))


# ==================== /状态 ====================
status_cmd = on_command("状态", permission=SUPERUSER)


@status_cmd.handle()
@cooldown(plugin_config.query_cooldown, "状态")
async def _(bot: Bot, event: GroupMessageEvent):
    s = get_state()

    lines = ["📊 群成员插件状态\n"]

    pc = pending_count()
    pbg = pending_by_group()
    lines.append(f"【事件缓冲】当前 {pc} 条")
    if pbg:
        for gid, n in sorted(pbg.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"  - 群 {gid}: {n}")
        if len(pbg) > 10:
            lines.append(f"  ... 共 {len(pbg)} 个群有积压")
    lines.append(
        f"  累计：推送 {s.event_pushed} / "
        f"缓冲 {s.event_buffered} / "
        f"刷出 {s.event_flushed} / "
        f"丢弃 {s.event_dropped}"
    )
    if s.dropped_by_group:
        top = sorted(s.dropped_by_group.items(), key=lambda x: -x[1])[:5]
        lines.append(
            "  丢弃分布："
            + ", ".join(f"群{gid}={n}" for gid, n in top)
        )
    lines.append("")

    fails = failing_groups()
    if fails:
        lines.append(
            f"⚠️ 连续同步失败（≥{plugin_config.sync_fail_warn} 次）"
        )
        for gid, cnt, last in fails[:10]:
            lines.append(f"  群 {gid}: 连续 {cnt} 次，最近 {last}")
        lines.append("")

    if s.last_sync:
        lines.append("【上次同步】")
        recent = sorted(
            s.last_sync.items(), key=lambda x: x[1], reverse=True
        )[:10]
        for gid, ts in recent:
            status = s.sync_status.get(gid, "?")
            lines.append(f"  群 {gid}: {ts:%Y-%m-%d %H:%M} ({status})")
        if len(s.last_sync) > 10:
            lines.append(f"  ... 共 {len(s.last_sync)} 个群已同步")
    else:
        lines.append("【上次同步】暂无记录（尚未执行过同步）")
    lines.append("")

    lines.append(
        f"【引擎缓存】{engine_count()} 个群连接"
        f"（上限 {plugin_config.group_db_max_engines}）"
    )
    lines.append("")

    if s.last_archive:
        lines.append(
            f"【归档】上次 {s.last_archive:%Y-%m-%d %H:%M}，"
            f"移动 {s.last_archive_moved} 条"
        )
    else:
        lines.append("【归档】尚未执行")
    lines.append(
        f"  保留 {plugin_config.event_retention_days} 天，"
        f"每月 {plugin_config.archive_cron_day} 日 "
        f"{plugin_config.archive_cron_hour}:00 执行"
    )

    if s.last_cleanup:
        lines.append("")
        lines.append(
            f"【群库清理】上次 {s.last_cleanup:%Y-%m-%d %H:%M}，"
            f"删除 {len(s.last_cleanup_deleted)} 个"
        )
        if s.last_cleanup_deleted:
            lines.append(
                "  "
                + ", ".join(str(g) for g in s.last_cleanup_deleted[:10])
            )
    else:
        lines.append("")
        lines.append("【群库清理】尚未执行")

    orphan_groups = [
        gid
        for gid in get_known_groups()
        if get_orphan_marker(gid) is not None
    ]
    if orphan_groups:
        lines.append(
            f"  ⏳ 观察中：{len(orphan_groups)} 个群"
            f"（grace={plugin_config.auto_cleanup_grace_days} 天）"
        )

    await status_cmd.send("\n".join(lines))


# ==================== /清理群库 ====================
CLEANUP_HELP_TEXT = (
    "用法：\n"
    "  /清理群库                        → 显示本帮助\n"
    "  /清理群库 help                   → 显示本帮助\n"
    "  /清理群库 <群号>                 → 手动删除指定群库"
    "（要求 Bot 已不在该群）\n"
    "  /清理群库 <群号> force           → 跳过“Bot 仍在群”检查，强制删除\n"
    "  /清理群库 scan                   → 扫描孤儿群（预览，不删）\n"
    "  /清理群库 scan confirm           → 扫描并执行清理"
    "（删除 grace 到期的孤儿）\n"
    "\n"
    f"自动模式："
    f"{'已开启' if plugin_config.auto_cleanup_enabled else '已关闭'}"
    f"（每月 {plugin_config.auto_cleanup_cron_day} 日 "
    f"{plugin_config.auto_cleanup_cron_hour}:00 执行）\n"
    f"grace 期：{plugin_config.auto_cleanup_grace_days} 天"
    "（首次发现 Bot 不在群后，保留 N 天才真删）\n"
    "孤儿标记：data/groups/<群号>.db.orphan（Bot 回群后自动清除）"
)

cleanup_cmd = on_command("清理群库", permission=SUPERUSER)


@cleanup_cmd.handle()
@cooldown(plugin_config.query_cooldown, "清理群库")
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    text = args.extract_plain_text().strip()
    parts = text.split() if text else []

    if not text or (parts and parts[0].lower() == "help"):
        await cleanup_cmd.finish(CLEANUP_HELP_TEXT)

    if parts[0].lower() == "scan":
        confirm = len(parts) > 1 and parts[1].lower() == "confirm"

        bots = list(get_bots().values())
        if not bots:
            await cleanup_cmd.finish("当前没有已连接的 Bot")

        scan = await scan_orphan_groups(bots)
        if "error" in scan:
            await cleanup_cmd.finish(f"扫描失败：{scan['error']}")

        to_delete = scan["to_delete"]
        in_grace = scan["in_grace"]
        rejoined = scan["rejoined"]

        if not to_delete and not in_grace and not rejoined:
            await cleanup_cmd.finish(
                f"扫描完成：{scan['alive']} 个群在线，无孤儿群库"
            )

        lines = [f"🔍 扫描结果（{scan['alive']} 个群在线）\n"]

        if to_delete:
            lines.append(
                f"🗑️ 可清理（grace 已过，{len(to_delete)} 个）："
            )
            for gid, days in to_delete[:20]:
                lines.append(f"  - 群 {gid}（孤儿 {days} 天）")
            if len(to_delete) > 20:
                lines.append(f"  ... 共 {len(to_delete)} 个")
            lines.append("")

        if in_grace:
            lines.append(
                f"⏳ 观察期（{len(in_grace)} 个，"
                f"grace={plugin_config.auto_cleanup_grace_days} 天）："
            )
            for gid, days in in_grace[:10]:
                remain = plugin_config.auto_cleanup_grace_days - days
                lines.append(
                    f"  - 群 {gid}（已孤儿 {days} 天，还剩 {remain} 天）"
                )
            if len(in_grace) > 10:
                lines.append(f"  ... 共 {len(in_grace)} 个")
            lines.append("")

        if rejoined:
            lines.append(
                f"✅ Bot 已回到（已清标记，{len(rejoined)} 个）："
            )
            lines.append(
                "  " + ", ".join(str(g) for g in rejoined[:20])
            )
            lines.append("")

        if not confirm:
            lines.append("如需执行清理，发送：/清理群库 scan confirm")
            await cleanup_cmd.send("\n".join(lines))
            return

        if not to_delete:
            await cleanup_cmd.finish("无待清理群库（观察期未满）")

        deleted = await execute_cleanup(scan)
        _state.mark_cleanup(deleted)
        lines.append(f"\n✅ 已删除 {len(deleted)} 个群库：")
        lines.append("  " + ", ".join(str(g) for g in deleted))
        await cleanup_cmd.send("\n".join(lines))
        return

    try:
        gid = int(parts[0])
    except ValueError:
        await cleanup_cmd.finish(
            f"群号必须是数字\n\n{CLEANUP_HELP_TEXT}"
        )

    force = "force" in [p.lower() for p in parts[1:]]

    if not force:
        try:
            groups = await bot.get_group_list()
            in_group = any(g["group_id"] == gid for g in groups)
        except Exception as e:
            await cleanup_cmd.finish(f"获取群列表失败：{e}")
        if in_group:
            await cleanup_cmd.finish(
                f"Bot 仍在群 {gid} 中，请先让 Bot 退群，"
                f"或使用 /清理群库 {gid} force 强制删除"
            )

    removed = await delete_group_db(gid)
    if removed:
        await cleanup_cmd.send(f"已删除群 {gid} 的数据库文件")
    else:
        await cleanup_cmd.finish(f"群 {gid} 的数据库文件不存在")


# ==================== Bot 被移出群 ====================
bot_leave = on_notice()


@bot_leave.handle()
async def _(event: GroupDecreaseNoticeEvent):
    if event.user_id == int(event.self_id):
        await dispose_group(event.group_id)
