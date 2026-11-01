import asyncio
from collections import defaultdict

from nonebot import get_plugin_config
from nonebot.log import logger
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import Config
from .db import get_session, group_lock, init_group_db
from .models import MemberEvent
from .state import get_state

plugin_config = get_plugin_config(Config)
_state = get_state()

_buffer: dict[int, list[dict]] = defaultdict(list)
_buffer_lock = asyncio.Lock()


def _truncate(group_id: int):
    buf = _buffer[group_id]
    max_n = plugin_config.event_buffer_max
    if len(buf) > max_n:
        drop_n = len(buf) - max_n
        buf[:] = buf[drop_n:]
        _state.mark_event_dropped(group_id, drop_n)
        logger.warning(
            f"事件缓冲已满，丢弃 {drop_n} 条最旧事件 group={group_id}"
        )


async def _write_events(group_id: int, events: list[dict]):
    Session = await get_session(group_id)
    async with Session() as session:
        stmt = (
            sqlite_insert(MemberEvent)
            .values(events)
            .on_conflict_do_nothing()
        )
        await session.execute(stmt)
        await session.commit()


async def push_event(group_id: int, data: dict) -> bool:
    try:
        await init_group_db(group_id)
    except Exception as e:
        logger.warning(f"事件写入前建表失败 group={group_id}: {e}")
        _state.mark_event_dropped(group_id)
        return False

    async with _buffer_lock:
        has_backlog = bool(_buffer.get(group_id))

    if not has_backlog:
        try:
            async with group_lock(group_id, plugin_config.event_lock_timeout):
                await _write_events(group_id, [data])
            _state.mark_event_pushed()
            return True
        except RuntimeError:
            pass
        except Exception as e:
            logger.warning(f"事件直接写入失败 group={group_id}: {e}")
            _state.mark_event_dropped(group_id)
            return False

    async with _buffer_lock:
        _buffer[group_id].append(data)
        _truncate(group_id)
    _state.mark_event_buffered()
    return False


async def flush_group(group_id: int) -> int:
    async with _buffer_lock:
        pending = _buffer.get(group_id, [])
        if not pending:
            return 0
        batch = pending[:]
        _buffer[group_id] = []

    try:
        async with group_lock(group_id, plugin_config.event_lock_timeout):
            await _write_events(group_id, batch)
        _state.mark_event_flushed(len(batch))
        return len(batch)
    except RuntimeError:
        async with _buffer_lock:
            _buffer[group_id] = batch + _buffer.get(group_id, [])
            _truncate(group_id)
        return 0
    except Exception as e:
        logger.warning(
            f"flush 事件失败 group={group_id} 条数={len(batch)}: {e}"
        )
        async with _buffer_lock:
            _buffer[group_id] = batch + _buffer.get(group_id, [])
            _truncate(group_id)
        return 0


async def flush_all() -> int:
    async with _buffer_lock:
        group_ids = [gid for gid, evs in _buffer.items() if evs]
    if not group_ids:
        return 0
    total = 0
    for gid in group_ids:
        total += await flush_group(gid)
    return total


def pending_count() -> int:
    return sum(len(v) for v in _buffer.values())


def pending_by_group() -> dict[int, int]:
    return {gid: len(evs) for gid, evs in _buffer.items() if evs}


async def clear_all():
    async with _buffer_lock:
        _buffer.clear()
