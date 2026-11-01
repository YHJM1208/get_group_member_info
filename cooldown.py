import time
from functools import wraps

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.log import logger

_cooldown: dict[tuple[str, int, int], float] = {}
_CLEANUP_THRESHOLD = 1000


def cooldown(seconds: int, command_name: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            event = kwargs.get("event")
            if not isinstance(event, GroupMessageEvent):
                event = None
                for a in args:
                    if isinstance(a, GroupMessageEvent):
                        event = a
                        break
            bot = kwargs.get("bot")
            if event is None or bot is None:
                return await func(*args, **kwargs)

            key = (command_name, event.group_id, event.user_id)
            now = time.time()

            if len(_cooldown) > _CLEANUP_THRESHOLD:
                for k in [k for k, v in _cooldown.items() if v < now]:
                    _cooldown.pop(k, None)

            expire = _cooldown.get(key, 0)
            if now < expire:
                remaining = int(expire - now)
                try:
                    await bot.send(event, f"冷却中，还剩 {remaining} 秒")
                except Exception as e:
                    logger.warning(f"冷却提示发送失败: {e}")
                return

            _cooldown[key] = now + seconds
            return await func(*args, **kwargs)

        return wrapper

    return decorator
