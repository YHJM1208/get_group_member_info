from pydantic import BaseModel


class Config(BaseModel):
    # ===== 数据库 =====
    group_db_dir: str = "/home/admin/nbbot/data/groups"
    group_db_timeout: int = 10
    group_db_wal: bool = True
    group_db_max_engines: int = 50
    group_sync_timezone: str = "Asia/Shanghai"

    # ===== 命令冷却 =====
    sync_cooldown: int = 30
    query_cooldown: int = 10

    # ===== 定时任务 =====
    auto_sync_hours: str = "*/6"

    # ===== 锁超时 =====
    sync_lock_timeout: int = 30
    event_lock_timeout: int = 10
    archive_lock_timeout: int = 60

    # ===== 归档 =====
    event_retention_days: int = 180
    archive_cron_day: int = 1
    archive_cron_hour: int = 3

    # ===== 事件缓冲 =====
    event_buffer_max: int = 100
    event_flush_interval: int = 30

    # ===== 告警 =====
    sync_fail_warn: int = 3

    # ===== 自动清理 =====
    auto_cleanup_enabled: bool = True
    auto_cleanup_cron_day: int = 15
    auto_cleanup_cron_hour: int = 4
    auto_cleanup_grace_days: int = 7
