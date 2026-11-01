import asyncio
import contextvars
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from collections import OrderedDict

from nonebot import get_plugin_config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Config
from .models import Base

plugin_config = get_plugin_config(Config)

DB_DIR = Path(plugin_config.group_db_dir)
DB_DIR.mkdir(parents=True, exist_ok=True)

_engines: OrderedDict[int, AsyncEngine] = OrderedDict()
_sessions: dict[int, async_sessionmaker[AsyncSession]] = {}
_inited: set[int] = set()
_lock = asyncio.Lock()
_group_locks: dict[int, asyncio.Lock] = {}

_held: contextvars.ContextVar[set[int] | None] = contextvars.ContextVar(
    "held_group_locks", default=None
)


def _db_path(group_id: int) -> Path:
    return (DB_DIR / f"{group_id}.db").resolve()


def get_known_groups() -> list[int]:
    return sorted(
        int(p.stem) for p in DB_DIR.glob("*.db") if p.stem.isdigit()
    )


def _get_group_lock(group_id: int) -> asyncio.Lock:
    if group_id not in _group_locks:
        _group_locks[group_id] = asyncio.Lock()
    return _group_locks[group_id]


@asynccontextmanager
async def group_lock(group_id: int, timeout: float):
    held = _held.get()
    if held is None:
        held = set()
        _held.set(held)

    if group_id in held:
        yield
        return

    lock = _get_group_lock(group_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"群 {group_id} 正在同步/归档中，请稍后")

    held.add(group_id)
    try:
        yield
    finally:
        held.discard(group_id)
        lock.release()


async def _dispose_engine(group_id: int):
    engine = _engines.pop(group_id, None)
    _sessions.pop(group_id, None)
    _inited.discard(group_id)
    if engine is not None:
        await engine.dispose()


async def _get_engine(group_id: int) -> AsyncEngine:
    async with _lock:
        if group_id in _engines:
            _engines.move_to_end(group_id)
            return _engines[group_id]

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{_db_path(group_id)}",
            connect_args={"timeout": plugin_config.group_db_timeout},
        )
        _engines[group_id] = engine

        max_n = plugin_config.group_db_max_engines
        if max_n > 0 and len(_engines) > max_n:
            old_id = next(iter(_engines))
            await _dispose_engine(old_id)

        return engine


async def get_session(group_id: int) -> async_sessionmaker[AsyncSession]:
    if group_id not in _sessions:
        engine = await _get_engine(group_id)
        _sessions[group_id] = async_sessionmaker(
            engine, expire_on_commit=False
        )
    return _sessions[group_id]


async def init_group_db(group_id: int):
    if group_id in _inited:
        return
    engine = await _get_engine(group_id)
    async with engine.begin() as conn:
        if plugin_config.group_db_wal:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(Base.metadata.create_all)
    _inited.add(group_id)


async def dispose_group(group_id: int):
    async with _lock:
        await _dispose_engine(group_id)
        _group_locks.pop(group_id, None)


def _orphan_marker_path(group_id: int) -> Path:
    return DB_DIR / f"{group_id}.db.orphan"


def get_orphan_marker(group_id: int) -> datetime | None:
    p = _orphan_marker_path(group_id)
    if not p.exists():
        return None
    try:
        return datetime.fromisoformat(p.read_text().strip())
    except Exception:
        return None


def set_orphan_marker(group_id: int, ts: datetime):
    _orphan_marker_path(group_id).write_text(ts.isoformat())


def clear_orphan_marker(group_id: int):
    p = _orphan_marker_path(group_id)
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass


async def delete_group_db(group_id: int) -> bool:
    await dispose_group(group_id)
    removed = False
    for suffix in ("", "-wal", "-shm", ".orphan"):
        p = DB_DIR / f"{group_id}.db{suffix}"
        if p.exists():
            try:
                p.unlink()
                removed = True
            except Exception:
                pass
    return removed


def engine_count() -> int:
    return len(_engines)


async def close_all():
    async with _lock:
        for group_id in list(_engines.keys()):
            await _dispose_engine(group_id)
    _group_locks.clear()
