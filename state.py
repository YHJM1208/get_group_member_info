from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from nonebot import get_plugin_config

from .config import Config

plugin_config = get_plugin_config(Config)
_TZ = ZoneInfo(plugin_config.group_sync_timezone)


class _State:
    def __init__(self):
        self.last_sync: dict[int, datetime] = {}
        self.sync_status: dict[int, str] = {}
        self.sync_fail_count: dict[int, int] = defaultdict(int)

        self.event_pushed = 0
        self.event_buffered = 0
        self.event_flushed = 0
        self.event_dropped = 0
        self.dropped_by_group: dict[int, int] = defaultdict(int)

        self.last_archive: datetime | None = None
        self.last_archive_moved = 0

        self.last_cleanup: datetime | None = None
        self.last_cleanup_deleted: list[int] = []

    def mark_sync_success(self, group_id: int):
        self.last_sync[group_id] = datetime.now(_TZ)
        self.sync_status[group_id] = "success"
        self.sync_fail_count[group_id] = 0

    def mark_sync_failed(self, group_id: int, reason: str):
        self.last_sync[group_id] = datetime.now(_TZ)
        self.sync_status[group_id] = f"failed: {reason[:60]}"
        self.sync_fail_count[group_id] += 1

    def mark_event_pushed(self):
        self.event_pushed += 1

    def mark_event_buffered(self):
        self.event_buffered += 1

    def mark_event_flushed(self, n: int):
        self.event_flushed += n

    def mark_event_dropped(self, group_id: int, n: int = 1):
        self.event_dropped += n
        self.dropped_by_group[group_id] += n

    def mark_archive(self, moved: int):
        self.last_archive = datetime.now(_TZ)
        self.last_archive_moved = moved

    def mark_cleanup(self, deleted: list[int]):
        self.last_cleanup = datetime.now(_TZ)
        self.last_cleanup_deleted = deleted


_state = _State()


def get_state() -> _State:
    return _state


def failing_groups() -> list[tuple[int, int, str]]:
    threshold = plugin_config.sync_fail_warn
    return [
        (gid, cnt, _state.sync_status.get(gid, ""))
        for gid, cnt in _state.sync_fail_count.items()
        if cnt >= threshold
    ]
