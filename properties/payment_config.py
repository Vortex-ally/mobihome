"""
Перевірка конфігурації оплати без залежності від Django (використовується і в settings.py,
і в system checks, і в liqpay.py). Повертає список проблем без значень ключів.
"""

# Дозволені пари «версія API — алгоритм підпису». Автоматичного перемикання немає.
ALLOWED_VERSION_ALGORITHMS = {(7, "sha3_256"), (3, "sha1")}
SUPPORTED_ALGORITHMS = ("sha3_256", "sha1")


def liqpay_problems(*, public_key, private_key, sandbox, version, algorithm, site_url, debug):
    problems = []
    if not public_key or not private_key:
        problems.append("Не задано LIQPAY_PUBLIC_KEY / LIQPAY_PRIVATE_KEY.")
    if algorithm not in SUPPORTED_ALGORITHMS:
        problems.append("LIQPAY_SIGNATURE_ALGORITHM має бути sha3_256 або sha1.")
    elif (version, algorithm) not in ALLOWED_VERSION_ALGORITHMS:
        problems.append(
            "Недозволена пара LIQPAY_API_VERSION / LIQPAY_SIGNATURE_ALGORITHM: "
            f"{version} + {algorithm}. Дозволено лише 7 + sha3_256 або 3 + sha1."
        )
    if public_key:
        is_sandbox_key = public_key.startswith("sandbox_")
        if sandbox and not is_sandbox_key:
            problems.append("LIQPAY_SANDBOX=True, але ключ не тестовий (немає префікса sandbox_).")
        if not sandbox and is_sandbox_key:
            problems.append("LIQPAY_SANDBOX=False (бойовий режим), але задано тестовий ключ sandbox_.")
    if not site_url:
        problems.append("Не задано SITE_URL (потрібен для server_url і result_url).")
    elif not site_url.startswith("https://") and not debug:
        problems.append("SITE_URL має бути https:// у бойовому середовищі.")
    return problems


def startup_problems(*, payments_enabled, placement_required, liqpay_kwargs):
    """Проблеми, через які production-сервер не повинен стартувати."""
    problems = []
    if placement_required and not payments_enabled:
        problems.append(
            "PLACEMENT_PAYMENT_REQUIRED=True без PAYMENTS_ENABLED=True: каталог вимагатиме оплату, "
            "яку неможливо здійснити."
        )
    if payments_enabled:
        problems.extend(liqpay_problems(**liqpay_kwargs))
    return problems
