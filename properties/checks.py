"""System checks: узгодженість налаштувань оплати (manage.py check / запуск сервера) і спільного кешу (--deploy)."""

from django.conf import settings
from django.core.checks import Error, Warning, register


@register()
def payment_settings_check(app_configs, **kwargs):
    from .liqpay import LiqPayConfig
    from .payment_config import ALLOWED_VERSION_ALGORITHMS

    issues = []
    config = LiqPayConfig.from_settings()
    if settings.LIQPAY_PUBLIC_KEY and (config.version, config.algorithm) not in ALLOWED_VERSION_ALGORITHMS:
        issues.append(Error("LiqPay: недозволена пара версії API та алгоритму підпису.", id="properties.E003"))
    if settings.PLACEMENT_PAYMENT_REQUIRED and not settings.PAYMENTS_ENABLED:
        issues.append(Error(
            "PLACEMENT_PAYMENT_REQUIRED=True без PAYMENTS_ENABLED=True: каталог вимагатиме оплату, "
            "яку неможливо здійснити.", id="properties.E001"))
    if settings.PAYMENTS_ENABLED:
        for problem in config.problems():
            issues.append(Error(f"LiqPay: {problem}", id="properties.E002"))
    elif settings.LIQPAY_PUBLIC_KEY and settings.LIQPAY_SANDBOX is False and not settings.DEBUG:
        issues.append(Warning("Задано бойові ключі LiqPay, але PAYMENTS_ENABLED=False.", id="properties.W001"))
    return issues


@register(deploy=True)
def shared_cache_check(app_configs, **kwargs):
    """Production за proxy (кілька воркерів Gunicorn): лічильники лімітів мають бути спільні — лише Redis."""
    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if settings.BEHIND_PROXY and backend != "properties.cache.ResilientRedisCache" and not settings.ALLOW_LOCAL_CACHE:
        return [Error(
            "DJANGO_BEHIND_PROXY=True без спільного кешу: ліміти входу й форм рахувались би окремо в кожному "
            "воркері Gunicorn. Задайте DJANGO_REDIS_URL (redis://127.0.0.1:6379/1) або свідомо "
            "DJANGO_ALLOW_LOCAL_CACHE=True для одного процесу.", id="properties.E010")]
    return []
