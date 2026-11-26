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


# Ліміти для форм, які анонім може надсилати масово (спам, розсилка листів, засмічення БД).
# Лічильник рахує лише успішно прийняті запити; кеш — той самий, що для входу.
FORM_LIMITS = {
    "register": (10, 3600),        # нових акаунтів з однієї IP за годину
    "password_reset": (5, 3600),   # листів відновлення з однієї IP за годину
    "consultation": (5, 600),      # заявок на консультацію за 10 хв
    "audit_request": (5, 600),     # заявок на аудит за 10 хв
}


def form_rate_limited(request, scope):
    """True — ліміт для IP вичерпано (запит не обробляти). Сам не рахує: див. count_form_submission."""
    limit, _window = FORM_LIMITS[scope]
    return cache.get(f"form-rate:{scope}:{_client_ip(request)}", 0) >= limit


def count_form_submission(request, scope):
    _limit, window = FORM_LIMITS[scope]
    key = f"form-rate:{scope}:{_client_ip(request)}"
    if not cache.add(key, 1, window):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, window)
