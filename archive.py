from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nonebot import get_plugin_config
from nonebot.log import logger
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import Config
from .db import get_known_groups, get_session, group_lock, init_group_db
from .models import MemberEvent, MemberEventArchive
from .state import get_state

plugin_config = get_plugin_config(Config)
_state = get_state()
_TZ = ZoneInfo(plugin_config.group_sync_timezone)


async def archive_old_events(group_id: int) -> int:
    await init_group_db(group_id)
    cutoff = datetime.now(_TZ) - timedelta(
        days=plugin_config.event_retention_days
    )

    async with group_lock(group_id, plugin_config.archive_lock_timeout):
        Session = await get_session(group_id)
        async with Session() as session:
            result = await session.execute(
                select(MemberEvent).where(MemberEvent.event_time < cutoff)
            )
            old_events = result.scalars().all()
            if not old_events:
                return 0

            rows = [
                {
                    "group_id": e.group_id,
                    "user_id": e.user_id,
                    "event_type": e.event_type,
                    "sub_type": e.sub_type,
                    "operator_id": e.operator_id,
                    "card_old": e.card_old,
                    "card_new": e.card_new,
                    "raw_json": e.raw_json,
                    "event_time": e.event_time,
                }
                for e in old_events
            ]
            stmt = (
                sqlite_insert(MemberEventArchive)
                .values(rows)
                .on_conflict_do_nothing()
            )
            await session.execute(stmt)

            await session.execute(
                delete(MemberEvent).where(MemberEvent.event_time < cutoff)
            )
            await session.commit()
            return len(old_events)


async def archive_all_groups() -> dict:
    groups = get_known_groups()
    total_moved = 0
    failed: list[int] = []
    for gid in groups:
        try:
            n = await archive_old_events(gid)
            if n:
                logger.info(f"群 {gid} 归档 {n} 条事件")
            total_moved += n
        except Exception as e:
            logger.warning(f"群 {gid} 归档失败: {e}")
            failed.append(gid)
    _state.mark_archive(total_moved)
    return {"groups": len(groups), "moved": total_moved, "failed": failed}
