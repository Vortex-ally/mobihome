"""System checks: узгодженість налаштувань оплати (manage.py check / запуск сервера)."""

from django.conf import settings
from django.core.checks import Error, Warning, register


@register()
def payment_settings_check(app_configs, **kwargs):
    from .liqpay import LiqPayConfig

    issues = []
    if settings.PLACEMENT_PAYMENT_REQUIRED and not settings.PAYMENTS_ENABLED:
        issues.append(Error(
            "PLACEMENT_PAYMENT_REQUIRED=True без PAYMENTS_ENABLED=True: каталог вимагатиме оплату, "
            "яку неможливо здійснити.", id="properties.E001"))
    if settings.PAYMENTS_ENABLED:
        for problem in LiqPayConfig.from_settings().problems():
            issues.append(Error(f"LiqPay: {problem}", id="properties.E002"))
    elif settings.LIQPAY_PUBLIC_KEY and settings.LIQPAY_SANDBOX is False and not settings.DEBUG:
        issues.append(Warning("Задано бойові ключі LiqPay, але PAYMENTS_ENABLED=False.", id="properties.W001"))
    return issues
