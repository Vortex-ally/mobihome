"""
Спільний кеш production (Redis) для лічильників: ліміти спроб входу, форм, неправильних callback LiqPay.

ResilientRedisCache = вбудований django RedisCache + дві зміни:
- incr атомарний і НЕ створює ключ: стандартний incr робить EXISTS, потім INCR — якщо ключ саме зник
  (закінчилось вікно), INCR створив би лічильник без терміну дії (вічне блокування). Тут — один Lua-скрипт.
- Redis недоступний → помилка в лог, сайт відповідає далі (fail-open: ліміти тимчасово не діють),
  а не 500 на кожному вході чи формі. Таймаути з'єднання короткі (OPTIONS у settings).
"""

import logging

from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.redis import RedisCache

try:
    from redis.exceptions import ConnectionError as _RedisConnectionError
    from redis.exceptions import TimeoutError as _RedisTimeoutError

    REDIS_ERRORS = (_RedisConnectionError, _RedisTimeoutError, OSError)
except ImportError:  # пакет redis не встановлено: цей бекенд тоді не використовується
    REDIS_ERRORS = (OSError,)

logger = logging.getLogger(__name__)

# Збільшити лише наявний ключ (термін дії зберігається); nil — ключа немає.
_INCR_EXISTING = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('INCRBY', KEYS[1], ARGV[1])
end
return nil
"""


class ResilientRedisCache(RedisCache):
    def _unavailable(self, operation, exc):
        logger.error("Кеш Redis недоступний (%s: %s) — ліміти тимчасово не діють", operation, exc.__class__.__name__)

    def get(self, key, default=None, version=None):
        try:
            return super().get(key, default, version)
        except REDIS_ERRORS as exc:
            self._unavailable("get", exc)
            return default

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        try:
            return super().add(key, value, timeout, version)
        except REDIS_ERRORS as exc:
            self._unavailable("add", exc)
            return True  # «перший запит у вікні» — без блокування

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        try:
            return super().set(key, value, timeout, version)
        except REDIS_ERRORS as exc:
            self._unavailable("set", exc)

    def incr(self, key, delta=1, version=None):
        key = self.make_and_validate_key(key, version=version)
        try:
            value = self._cache.get_client(key, write=True).eval(_INCR_EXISTING, 1, key, delta)
        except REDIS_ERRORS as exc:
            self._unavailable("incr", exc)
            return delta
        if value is None:
            raise ValueError("Key '%s' not found." % key)
        return value

    def delete(self, key, version=None):
        try:
            return super().delete(key, version)
        except REDIS_ERRORS as exc:
            self._unavailable("delete", exc)
            return False

    def delete_many(self, keys, version=None):
        try:
            return super().delete_many(keys, version)
        except REDIS_ERRORS as exc:
            self._unavailable("delete_many", exc)
