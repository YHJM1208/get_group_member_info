"""Load the plugin's real handler/sync modules without a NoneBot runtime.

The unit tests only touch pure helpers, but ``handlers.py`` imports NoneBot,
the OneBot adapter, SQLAlchemy and the plugin's sibling modules at module
level.  This conftest builds a minimal stand-in for those imports, then loads
the real ``sync.py`` and ``handlers.py`` files from disk.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_CANDIDATES = (
    ROOT / "src" / "plugins" / "get_group_member_info",
    ROOT,
)
PLUGIN_DIR = next(
    (path for path in _CANDIDATES if (path / "handlers.py").is_file()),
    _CANDIDATES[0],
)

PACKAGE = "_gmi_under_test"


class FinishedException(Exception):
    """Stand-in for ``nonebot.exception.FinishedException``."""


class _Matcher:
    """Minimal matcher used by the module-level decorators."""

    def handle(self):
        def decorator(func):
            return func

        return decorator

    async def finish(self, *args, **kwargs):
        raise FinishedException()

    async def send(self, *args, **kwargs):
        return None


class _Logger:
    def warning(self, *args, **kwargs):
        pass

    info = warning
    exception = warning
    debug = warning


def _new_module(name: str, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []  # let Python treat it as a package
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _make_config():
    return SimpleNamespace(
        group_db_dir=str(PLUGIN_DIR / "data"),
        group_db_timeout=10,
        group_db_wal=True,
        group_db_max_engines=50,
        group_sync_timezone="Asia/Shanghai",
        sync_cooldown=30,
        query_cooldown=10,
        auto_sync_hours="*/6",
        sync_lock_timeout=30,
        event_lock_timeout=10,
        archive_lock_timeout=60,
        event_retention_days=180,
        archive_cron_day=1,
        archive_cron_hour=3,
        event_buffer_max=100,
        event_flush_interval=30,
        sync_fail_warn=3,
        auto_cleanup_enabled=True,
        auto_cleanup_cron_day=15,
        auto_cleanup_cron_hour=4,
        auto_cleanup_grace_days=7,
    )


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_stubs():
    for name in [
        name
        for name in sys.modules
        if name == PACKAGE or name.startswith(f"{PACKAGE}.")
    ]:
        del sys.modules[name]

    config = _make_config()

    nonebot = _new_module(
        "nonebot",
        get_bots=lambda: {},
        get_plugin_config=lambda config_cls: config,
        on_command=lambda *args, **kwargs: _Matcher(),
        on_notice=lambda *args, **kwargs: _Matcher(),
    )
    adapters = _new_module("nonebot.adapters")
    onebot = _new_module("nonebot.adapters.onebot")
    v11 = _new_module(
        "nonebot.adapters.onebot.v11",
        Bot=type("Bot", (), {}),
        GroupAdminNoticeEvent=type("GroupAdminNoticeEvent", (), {}),
        GroupDecreaseNoticeEvent=type("GroupDecreaseNoticeEvent", (), {}),
        GroupIncreaseNoticeEvent=type("GroupIncreaseNoticeEvent", (), {}),
        GroupMessageEvent=type("GroupMessageEvent", (), {}),
        Message=type("Message", (), {}),
        NoticeEvent=type("NoticeEvent", (), {}),
    )
    nonebot.adapters = adapters
    adapters.onebot = onebot
    onebot.v11 = v11

    _new_module("nonebot.exception", FinishedException=FinishedException)
    _new_module("nonebot.log", logger=_Logger())
    _new_module("nonebot.params", CommandArg=type("CommandArg", (), {}))
    _new_module("nonebot.permission", SUPERUSER=object())

    _new_module(
        "sqlalchemy",
        Integer=object(),
        cast=lambda *args, **kwargs: None,
        func=SimpleNamespace(count=lambda *args, **kwargs: None),
        select=lambda *args, **kwargs: None,
        delete=lambda *args, **kwargs: None,
        update=lambda *args, **kwargs: None,
    )

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PACKAGE] = package

    _new_module(f"{PACKAGE}.config", Config=type("Config", (), {}))
    _new_module(
        f"{PACKAGE}.cooldown",
        cooldown=lambda seconds, command_name: (lambda func: func),
    )
    _new_module(
        f"{PACKAGE}.cleanup",
        execute_cleanup=lambda *args, **kwargs: None,
        scan_orphan_groups=lambda *args, **kwargs: None,
    )
    _new_module(
        f"{PACKAGE}.db",
        delete_group_db=lambda *args, **kwargs: None,
        dispose_group=lambda *args, **kwargs: None,
        engine_count=lambda: 0,
        get_known_groups=lambda: [],
        get_orphan_marker=lambda *args, **kwargs: None,
        get_session=lambda *args, **kwargs: None,
        group_lock=None,
        init_group_db=lambda *args, **kwargs: None,
    )
    _new_module(
        f"{PACKAGE}.event_buffer",
        pending_by_group=lambda: {},
        pending_count=lambda: 0,
        push_event=lambda *args, **kwargs: None,
    )
    _new_module(
        f"{PACKAGE}.models",
        GroupMember=type("GroupMember", (), {}),
        MemberEvent=type("MemberEvent", (), {}),
        MemberEventArchive=type("MemberEventArchive", (), {}),
    )
    _new_module(
        f"{PACKAGE}.state",
        failing_groups=lambda: [],
        get_state=lambda: SimpleNamespace(
            last_sync={},
            sync_status={},
            sync_fail_count={},
            event_pushed=0,
            event_buffered=0,
            event_flushed=0,
            event_dropped=0,
            dropped_by_group={},
            last_archive=None,
            last_archive_moved=0,
            last_cleanup=None,
            last_cleanup_deleted=[],
            mark_cleanup=lambda deleted: None,
        ),
    )

    # Windows test environments often have no tzdata package.  ZoneInfo is
    # only used for a module-level _TZ constant, so UTC is enough here.
    real_zoneinfo = sys.modules.get("zoneinfo")
    zoneinfo_stub = types.ModuleType("zoneinfo")
    import datetime as _datetime

    zoneinfo_stub.ZoneInfo = lambda key: _datetime.timezone.utc
    sys.modules["zoneinfo"] = zoneinfo_stub
    try:
        sync = _load_module(f"{PACKAGE}.sync", PLUGIN_DIR / "sync.py")
        handlers = _load_module(
            f"{PACKAGE}.handlers", PLUGIN_DIR / "handlers.py"
        )
    finally:
        if real_zoneinfo is None:
            sys.modules.pop("zoneinfo", None)
        else:
            sys.modules["zoneinfo"] = real_zoneinfo

    return handlers, sync


@pytest.fixture(scope="session")
def plugin_modules():
    return _install_stubs()


@pytest.fixture(scope="session")
def handlers(plugin_modules):
    return plugin_modules[0]


@pytest.fixture(scope="session")
def sync(plugin_modules):
    return plugin_modules[1]
