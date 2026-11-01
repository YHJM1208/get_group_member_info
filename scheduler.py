"""定时任务。

自动同步成员、事件缓冲 flush、事件归档、孤儿群库清理。
"""

import asyncio

from nonebot import get_bots, get_plugin_config
from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler

from .archive import archive_all_groups
from .cleanup import auto_cleanup
from .config import Config
from .event_buffer import flush_all
from .sync import save_group_members

plugin_config = get_plugin_config(Config)


# ==================== 定时任务 ====================
_synced_this_round: set[int] = set()


@scheduler.scheduled_job(
    "cron", hour=plugin_config.auto_sync_hours, id="auto_sync_members"
)
async def auto_sync_members():
    bots = get_bots()
    if not bots:
        return
    _synced_this_round.clear()
    for bot in bots.values():
        try:
            groups = await bot.get_group_list()
        except Exception as e:
            logger.warning(f"定时同步获取群列表失败: {e}")
            continue
        for g in groups:
            group_id = g["group_id"]
            if group_id in _synced_this_round:
                continue
            _synced_this_round.add(group_id)
            try:
                members = await bot.get_group_member_list(
                    group_id=group_id
                )
                if not members:
                    continue
                await save_group_members(group_id, members)
                logger.info(f"定时同步群 {group_id}: {len(members)} 人")
            except RuntimeError as e:
                logger.info(f"定时同步群 {group_id} 跳过: {e}")
            except Exception as e:
                logger.warning(f"定时同步群 {group_id} 失败: {e}")
            await asyncio.sleep(2)


@scheduler.scheduled_job(
    "interval",
    seconds=plugin_config.event_flush_interval,
    id="flush_event_buffer",
)
async def flush_event_buffer_job():
    try:
        n = await flush_all()
        if n:
            logger.info(f"事件缓冲 flush：{n} 条")
    except Exception as e:
        logger.exception(f"flush 失败: {e}")


@scheduler.scheduled_job(
    "cron",
    day=plugin_config.archive_cron_day,
    hour=plugin_config.archive_cron_hour,
    id="archive_events",
)
async def archive_events_job():
    try:
        stats = await archive_all_groups()
        logger.info(
            f"归档完成：{stats['groups']} 群，"
            f"移动 {stats['moved']} 条，失败 {len(stats['failed'])} 群"
        )
    except Exception as e:
        logger.exception(f"归档失败: {e}")


@scheduler.scheduled_job(
    "cron",
    day=plugin_config.auto_cleanup_cron_day,
    hour=plugin_config.auto_cleanup_cron_hour,
    id="auto_cleanup_orphan_groups",
)
async def auto_cleanup_job():
    if not plugin_config.auto_cleanup_enabled:
        return
    bots = list(get_bots().values())
    if not bots:
        return
    try:
        result = await auto_cleanup(bots)
        scan = result["scan"]
        deleted = result["deleted"]
        logger.info(
            f"自动清理完成：删除 {len(deleted)} 个，"
            f"{len(scan.get('in_grace', []))} 个仍在观察期"
        )
    except Exception as e:
        logger.exception(f"自动清理失败: {e}")
