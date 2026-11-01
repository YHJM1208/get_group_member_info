from nonebot import get_plugin_config
from nonebot.log import logger
from sqlalchemy import delete, update

from .config import Config
from .db import get_session, group_lock, init_group_db
from .models import GroupMember
from .state import get_state

plugin_config = get_plugin_config(Config)
_state = get_state()


def _row(group_id: int, m: dict) -> dict:
    return {
        "group_id": group_id,
        "user_id": m["user_id"],
        "nickname": m.get("nickname") or "",
        "card": m.get("card") or "",
        "sex": m.get("sex") or "unknown",
        "age": m.get("age", 0) or 0,
        "area": m.get("area") or "",
        "join_time": m.get("join_time", 0) or 0,
        "last_sent_time": m.get("last_sent_time", 0) or 0,
        "level": m.get("level") or "",
        "role": m.get("role") or "member",
        "unfriendly": bool(m.get("unfriendly", False)),
        "title": m.get("title") or "",
        "title_expire_time": m.get("title_expire_time", 0) or 0,
        "card_changeable": bool(m.get("card_changeable", True)),
        "shut_up_timestamp": m.get("shut_up_timestamp", 0) or 0,
        "is_robot": bool(m.get("is_robot", False)),
    }


async def save_group_members(group_id: int, members: list[dict]) -> dict:
    await init_group_db(group_id)
    try:
        async with group_lock(group_id, plugin_config.sync_lock_timeout):
            Session = await get_session(group_id)
            async with Session() as session:
                await session.execute(
                    delete(GroupMember).where(
                        GroupMember.group_id == group_id
                    )
                )
                if members:
                    session.add_all(
                        [GroupMember(**_row(group_id, m)) for m in members]
                    )
                await session.commit()
        _state.mark_sync_success(group_id)
    except RuntimeError as e:
        _state.mark_sync_failed(group_id, str(e))
        raise
    except Exception as e:
        _state.mark_sync_failed(group_id, str(e))
        raise

    return {"total": len(members)}


async def update_member_card(
    group_id: int, user_id: int, card: str
) -> bool:
    """名片变更时同步更新主表，避免要等下一次全量同步才能查到。"""
    try:
        await init_group_db(group_id)
        async with group_lock(group_id, plugin_config.event_lock_timeout):
            Session = await get_session(group_id)
            async with Session() as session:
                result = await session.execute(
                    update(GroupMember)
                    .where(
                        GroupMember.group_id == group_id,
                        GroupMember.user_id == user_id,
                    )
                    .values(card=card or "")
                )
                await session.commit()
                return bool(result.rowcount)
    except Exception as e:
        logger.warning(
            f"更新群 {group_id} 成员 {user_id} 名片失败: {e}"
        )
        return False
