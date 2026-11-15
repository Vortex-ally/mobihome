"""Допоміжні засоби безпеки: обмеження спроб входу."""

import hashlib

from django.conf import settings
from django.core.cache import cache


def _client_ip(request):
    # REMOTE_ADDR: за проксі його має коректно виставляти вебсервер (не довіряємо X-Forwarded-For).
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _keys(request, username):
    uname = hashlib.sha256((username or "").strip().lower().encode()).hexdigest()[:32]
    ip = _client_ip(request)
    return (f"login-fail:ip:{ip}", f"login-fail:user:{uname}")


def login_is_locked(request, username):
    limit = settings.LOGIN_THROTTLE_MAX_ATTEMPTS
    ip_key, user_key = _keys(request, username)
    # Ліміт на IP вищий, щоб кілька людей за одним NAT не блокували одне одного.
    return cache.get(user_key, 0) >= limit or cache.get(ip_key, 0) >= limit * 4


def register_login_failure(request, username):
    window = settings.LOGIN_THROTTLE_WINDOW_SECONDS
    for key in _keys(request, username):
        if cache.add(key, 1, window):
            continue
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, window)


def reset_login_failures(request, username):
    cache.delete_many(list(_keys(request, username)))
