from datetime import datetime
from zoneinfo import ZoneInfo

from nonebot import get_plugin_config
from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import Config

plugin_config = get_plugin_config(Config)
_TZ = ZoneInfo(plugin_config.group_sync_timezone)


def _now() -> datetime:
    return datetime.now(_TZ)


class Base(DeclarativeBase):
    pass


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)

    nickname: Mapped[str] = mapped_column(String(128), default="")
    card: Mapped[str] = mapped_column(String(128), default="")
    sex: Mapped[str] = mapped_column(String(16), default="unknown")
    age: Mapped[int] = mapped_column(Integer, default=0)
    area: Mapped[str] = mapped_column(String(64), default="")
    join_time: Mapped[int] = mapped_column(Integer, default=0)
    last_sent_time: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[str] = mapped_column(String(16), default="")
    role: Mapped[str] = mapped_column(String(16), default="member")
    unfriendly: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(128), default="")
    title_expire_time: Mapped[int] = mapped_column(Integer, default=0)
    card_changeable: Mapped[bool] = mapped_column(Boolean, default=True)
    shut_up_timestamp: Mapped[int] = mapped_column(Integer, default=0)
    is_robot: Mapped[bool] = mapped_column(Boolean, default=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now
    )


class MemberEvent(Base):
    __tablename__ = "member_events"
    __table_args__ = (
        Index("idx_events_group_user", "group_id", "user_id"),
        Index("idx_events_group_type", "group_id", "event_type"),
        Index(
            "idx_events_dedup",
            "group_id", "user_id", "event_type", "event_time",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    sub_type: Mapped[str] = mapped_column(String(16), default="")
    operator_id: Mapped[int | None] = mapped_column(Integer, default=None)
    card_old: Mapped[str] = mapped_column(String(128), default="")
    card_new: Mapped[str] = mapped_column(String(128), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")
    event_time: Mapped[datetime] = mapped_column(DateTime, default=_now)


class MemberEventArchive(Base):
    __tablename__ = "member_events_archive"
    __table_args__ = (
        Index("idx_archive_group_user", "group_id", "user_id"),
        Index(
            "idx_archive_dedup",
            "group_id", "user_id", "event_type", "event_time",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    sub_type: Mapped[str] = mapped_column(String(16), default="")
    operator_id: Mapped[int | None] = mapped_column(Integer, default=None)
    card_old: Mapped[str] = mapped_column(String(128), default="")
    card_new: Mapped[str] = mapped_column(String(128), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")
    event_time: Mapped[datetime] = mapped_column(DateTime)
