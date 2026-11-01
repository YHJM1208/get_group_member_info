from datetime import datetime
from zoneinfo import ZoneInfo

from nonebot import get_plugin_config
from nonebot.log import logger

from .config import Config
from .db import (
    clear_orphan_marker,
    delete_group_db,
    get_known_groups,
    get_orphan_marker,
    set_orphan_marker,
)
from .state import get_state

plugin_config = get_plugin_config(Config)
_state = get_state()
_TZ = ZoneInfo(plugin_config.group_sync_timezone)


async def _get_alive_groups(bots) -> set[int] | None:
    alive: set[int] = set()
    for bot in bots:
        try:
            gl = await bot.get_group_list()
            alive.update(g["group_id"] for g in gl)
        except Exception as e:
            logger.warning(f"获取群列表失败（保守跳过本次清理）: {e}")
            return None
    return alive


async def scan_orphan_groups(bots) -> dict:
    groups = get_known_groups()
    if not groups:
        return {
            "to_delete": [],
            "in_grace": [],
            "rejoined": [],
            "alive": 0,
        }

    alive = await _get_alive_groups(bots)
    if alive is None:
        return {
            "to_delete": [],
            "in_grace": [],
            "rejoined": [],
            "alive": 0,
            "error": "群列表获取失败",
        }

    now = datetime.now(_TZ)
    grace = plugin_config.auto_cleanup_grace_days

    to_delete: list[tuple[int, int]] = []
    in_grace: list[tuple[int, int]] = []
    rejoined: list[int] = []

    for gid in groups:
        if gid in alive:
            if get_orphan_marker(gid) is not None:
                clear_orphan_marker(gid)
                rejoined.append(gid)
            continue

        first_seen = get_orphan_marker(gid)
        if first_seen is None:
            set_orphan_marker(gid, now)
            in_grace.append((gid, 0))
        else:
            days = (now - first_seen).days
            if days >= grace:
                to_delete.append((gid, days))
            else:
                in_grace.append((gid, days))

    return {
        "to_delete": to_delete,
        "in_grace": in_grace,
        "rejoined": rejoined,
        "alive": len(alive),
    }


async def execute_cleanup(scan_result: dict) -> list[int]:
    deleted: list[int] = []
    for gid, _days in scan_result.get("to_delete", []):
        try:
            if await delete_group_db(gid):
                deleted.append(gid)
                logger.info(f"自动清理：已删除群 {gid} 的数据库")
        except Exception as e:
            logger.warning(f"清理群 {gid} 失败: {e}")
    return deleted


async def auto_cleanup(bots) -> dict:
    scan = await scan_orphan_groups(bots)
    deleted = await execute_cleanup(scan)
    _state.mark_cleanup(deleted)
    return {"scan": scan, "deleted": deleted}
