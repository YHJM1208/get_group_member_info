"""群成员同步插件入口。

只负责插件元数据、依赖声明、模块装配和生命周期钩子；命令与事件响应器在
handlers.py，定时任务在 scheduler.py，入口文件不承载业务逻辑。
"""

import sys

from nonebot import get_driver, require
from nonebot.log import logger
from nonebot.plugin import PluginMetadata

from .config import Config
from .db import close_all
from .event_buffer import flush_all, pending_count

# 其它插件可能已经直接 import 了 apscheduler；此时再 require 会触发
# “Module ... is not loaded as a plugin”，因此仅在尚未导入时声明依赖。
if "nonebot_plugin_apscheduler" not in sys.modules:
    require("nonebot_plugin_apscheduler")

from . import handlers as _handlers  # noqa: E402,F401
from . import scheduler as _scheduler  # noqa: E402,F401

__plugin_meta__ = PluginMetadata(
    name="群成员同步",
    description="主表快照 + 实时事件监听 + 归档 + 状态监控",
    usage="/同步成员 | /事件查询 | /成员查询 | /状态 | /清理群库",
    type="application",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

_driver = get_driver()


@_driver.on_shutdown
async def _shutdown() -> None:
    """关机前先刷出事件缓冲，再释放全部 SQLite 引擎。"""
    try:
        n = await flush_all()
        if n:
            logger.info(f"关机前 flush 事件：{n} 条")
        if pending_count():
            logger.warning(f"关机时仍有 {pending_count()} 条事件未写入")
    except Exception as e:
        logger.warning(f"关机 flush 失败: {e}")
    await close_all()
