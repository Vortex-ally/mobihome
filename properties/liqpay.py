"""
Інтеграція з LiqPay (Hosted Checkout + server-to-server callback + запит статусу).

Документація (перевірено 2026-11): https://www.liqpay.ua/uk/doc/api/internet_acquiring/checkout,
https://www.liqpay.ua/uk/doc/api/callback, https://www.liqpay.ua/uk/doc/api/information/status_payment,
https://www.liqpay.ua/uk/doc/api/testing

- data      = base64(json)
- signature = base64(HASH(private_key + data + private_key)), HASH — sha3-256 (поточна специфікація,
  version=7). Застарілі приклади/SDK використовують sha1 + version=3; алгоритм і версія задаються
  налаштуваннями LIQPAY_SIGNATURE_ALGORITHM / LIQPAY_API_VERSION, але одночасно активний лише один.
- Наш сайт не збирає й не зберігає реквізити картки: оплата відбувається на сторінці LiqPay.
- Оплата зараховується ЛИШЕ за повідомленням із дійсним підписом (callback) або за серверним
  запитом статусу; повернення користувача на result_url нічого не підтверджує.

Секретний ключ ніколи не логується і не потрапляє у відповіді/події.
"""

import base64
import hashlib
import hmac
import http.client
import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .models import Payment, PaymentEvent, PaymentStatus
from .payment_config import ALLOWED_VERSION_ALGORITHMS, liqpay_problems
from .placement import (
    DuplicateChargeError,
    PlacementError,
    apply_successful_payment,
    mark_reversed,
    mark_unsuccessful,
    payment_hold_window,
)

logger = logging.getLogger(__name__)

HASHERS = {"sha3_256": hashlib.sha3_256, "sha1": hashlib.sha1}

FAILURE_STATUSES = {"failure", "error"}
PENDING_STATUSES = {
    "processing", "prepared", "wait_secure", "wait_accept", "wait_card", "wait_compensation",
    "wait_lc", "wait_reserve", "wait_qr", "wait_sender", "hold_wait", "invoice_wait", "cash_wait",
    "3ds_verify", "captcha_verify", "cvv_verify", "ivr_verify", "otp_verify", "password_verify",
    "phone_verify", "pin_verify", "senderapp_verify",
}
# Поля, які зберігаються в журналі подій. Ім'я/телефон/маска картки платника НЕ зберігаються.
SAFE_EVENT_FIELDS = (
    "action", "status", "order_id", "payment_id", "liqpay_order_id", "transaction_id", "amount",
    "currency", "public_key", "version", "err_code", "err_description", "paytype", "create_date",
    "end_date", "result", "type", "is_3ds",
)


class LiqPayConfigError(Exception):
    pass


@dataclass(frozen=True)
class LiqPayConfig:
    public_key: str
    private_key: str
    sandbox: bool
    version: int
    algorithm: str

    @classmethod
    def from_settings(cls):
        return cls(
            public_key=settings.LIQPAY_PUBLIC_KEY,
            private_key=settings.LIQPAY_PRIVATE_KEY,
            sandbox=settings.LIQPAY_SANDBOX,
            version=settings.LIQPAY_API_VERSION,
            algorithm=settings.LIQPAY_SIGNATURE_ALGORITHM,
        )

    def __repr__(self):  # ніколи не показуємо приватний ключ
        return (f"LiqPayConfig(public_key=***, private_key=***, sandbox={self.sandbox}, "
                f"version={self.version}, algorithm={self.algorithm})")

    def problems(self):
        """Список проблем конфігурації (без значень ключів)."""
        return liqpay_problems(
            public_key=self.public_key, private_key=self.private_key, sandbox=self.sandbox,
            version=self.version, algorithm=self.algorithm,
            site_url=settings.SITE_URL, debug=settings.DEBUG,
        )


def payments_active():
    """Оплата доступна користувачам: увімкнено прапорець і конфігурація без помилок."""
    return bool(settings.PAYMENTS_ENABLED) and not LiqPayConfig.from_settings().problems()


# --- Кодування та підпис ---


def encode_data(params):
    raw = json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_data(data):
    try:
        decoded = json.loads(base64.b64decode(data, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def make_signature(data, config=None):
    config = config or LiqPayConfig.from_settings()
    hasher = HASHERS.get(config.algorithm)
    if (config.version, config.algorithm) not in ALLOWED_VERSION_ALGORITHMS:
        raise LiqPayConfigError("Недозволена пара версії API та алгоритму підпису.")
    if hasher is None or not config.private_key:
        raise LiqPayConfigError("LiqPay не налаштовано.")
    digest = hasher((config.private_key + data + config.private_key).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_signature(data, signature, config=None):
    """Криптографічно безпечне порівняння (hmac.compare_digest). Невалідні входи — False."""
    if not isinstance(data, str) or not isinstance(signature, str) or not data or not signature:
        return False
    if not signature.isascii():
        return False  # base64-підпис завжди ASCII; жодних «тихих» перетворень
    try:
        expected = make_signature(data, config)
    except LiqPayConfigError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii"))


# --- Checkout ---


def _absolute(path):
    return f"{settings.SITE_URL}{path}"


def build_checkout(payment, config=None):
    """Параметри Hosted Checkout. Сума/валюта/опис — лише з серверного запису платежу."""
    config = config or LiqPayConfig.from_settings()
    # Від створення платежу, а не від «зараз»: повторне «Продовжити» не подовжує життя форми.
    expires = payment.created_at + timedelta(minutes=settings.LIQPAY_PAYMENT_TTL_MINUTES)
    params = {
        "version": config.version,
        "public_key": config.public_key,
        "action": "pay",
        "amount": str(payment.amount),
        "currency": payment.currency,
        "description": f"{payment.tariff_name}: оголошення #{payment.property_id}"[:255],
        "order_id": payment.order_id,
        "language": "uk",
        "result_url": _absolute(reverse("properties:payment_result", args=[payment.order_id])),
        "server_url": _absolute(reverse("properties:liqpay_callback")),
        "expired_date": expires.astimezone(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    data = encode_data(params)
    return {"url": settings.LIQPAY_CHECKOUT_URL, "data": data, "signature": make_signature(data, config)}


# --- Обробка повідомлень (callback і відповідь на запит статусу) ---


MAX_EVENT_VALUE_LENGTH = 255


def _safe_payload(fields):
    """Лише дозволені поля; рядки обрізаються, вкладені структури не зберігаються."""
    out = {}
    for key in SAFE_EVENT_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = str(value)[:MAX_EVENT_VALUE_LENGTH]
    return out


def _parse_paid_at(fields):
    for key in ("end_date", "create_date"):
        try:
            ms = int(fields.get(key))
            return datetime.fromtimestamp(ms / 1000, tz=dt_timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return None


@dataclass
class ProcessResult:
    ok: bool  # повідомлення прийняте (відповідь 200)
    credited: bool = False
    note: str = ""
    payment: Payment = None


def _rejection_reason(fields, payment, config):
    """Загальні перевірки повідомлення (крім підпису). None — усе гаразд."""
    if payment is None:
        return "unknown_order", "Відхилено: невідомий order_id."
    if not hmac.compare_digest(str(fields.get("public_key", "")), config.public_key):
        return "public_key_mismatch", "Відхилено: public_key не збігається з нашим."
    if fields.get("action") not in (None, "pay"):
        return "unexpected_action", f"Відхилено: неочікувана дія {fields.get('action')!r}."
    return None


def _handle_success(fields, payment, status, config, log):
    if status == "sandbox" and not (config.sandbox and payment.is_test):
        log("Відхилено: статус sandbox у бойовому режимі або для бойового платежу.")
        return ProcessResult(ok=True, note="sandbox_in_production", payment=payment)
    if payment.is_test and not config.sandbox:
        log("Відхилено: тестовий платіж не зараховується в бойовому режимі.")
        return ProcessResult(ok=True, note="test_payment_in_production", payment=payment)
    try:
        amount = Decimal(str(fields.get("amount")))
    except (InvalidOperation, TypeError):
        log("Відхилено: некоректна сума.")
        return ProcessResult(ok=True, note="bad_amount", payment=payment)
    try:
        payment, credited = apply_successful_payment(
            payment.pk, paid_amount=amount, paid_currency=str(fields.get("currency", "")),
            transaction_id=fields.get("payment_id") or fields.get("transaction_id"),
            paid_at=_parse_paid_at(fields),
        )
    except DuplicateChargeError:
        # Подію [ALERT duplicate_charge] і позначку needs_review уже збережено в apply_successful_payment.
        logger.error("LiqPay duplicate charge: order_id=%s payment_id=%s", payment.order_id,
                     str(fields.get("payment_id") or fields.get("transaction_id"))[:64])
        return ProcessResult(ok=True, note="duplicate_charge", payment=payment)
    except PlacementError as exc:
        log(f"Не зараховано: {exc}")
        return ProcessResult(ok=True, note="placement_error", payment=payment)
    if credited:
        log("Оплату зараховано.")
    elif payment.status == PaymentStatus.PENDING and payment.error_message:
        log("Не зараховано: невідповідність суми або валюти.")
    else:
        log("Повторне повідомлення: оплату вже зараховано раніше, змін немає.")
    return ProcessResult(ok=True, credited=credited, payment=payment,
                         note="credited" if credited else "not_credited")


def _handle_other_status(fields, payment, status, log):
    if status in FAILURE_STATUSES:
        err = " ".join(str(fields.get(k, "")) for k in ("err_code", "err_description")).strip()
        payment = mark_unsuccessful(payment.pk, PaymentStatus.FAILURE, err or status)
        log("Оплату відхилено.")
        return ProcessResult(ok=True, payment=payment, note="failure")
    if status == "reversed":
        provider_id = fields.get("payment_id") or fields.get("transaction_id")
        try:
            payment = mark_reversed(payment.pk, transaction_id=provider_id)
        except DuplicateChargeError:
            # Повернення ДРУГОГО списання: подію [ALERT duplicate_refund] уже збережено, перший платіж цілий.
            logger.error("LiqPay duplicate refund: order_id=%s payment_id=%s", payment.order_id,
                         str(provider_id)[:64])
            return ProcessResult(ok=True, payment=payment, note="duplicate_reversed")
        log("Кошти повернено.")
        return ProcessResult(ok=True, payment=payment, note="reversed")
    if status in PENDING_STATUSES:
        if not payment.is_final():
            payment = mark_unsuccessful(payment.pk, PaymentStatus.PENDING, "")
        log("Платіж ще не завершено.")
        return ProcessResult(ok=True, payment=payment, note="pending")
    log("Невідомий статус — без змін.")
    return ProcessResult(ok=True, payment=payment, note="unknown_status")


def process_payment_fields(fields, *, signature_valid, source, config=None):
    """
    Застосовує дані LiqPay до нашого платежу після всіх перевірок.
    signature_valid: для callback — результат verify_signature; для запиту статусу — True
    (HTTPS-відповідь від api/request на наш підписаний запит; поля додатково звіряються).
    """
    config = config or LiqPayConfig.from_settings()
    order_id = str(fields.get("order_id", ""))[:64]
    payment = Payment.objects.filter(order_id=order_id).first() if order_id else None
    status = str(fields.get("status", ""))[:32]

    def log(note):
        PaymentEvent.objects.create(
            payment=payment, order_id=order_id, signature_valid=signature_valid,
            provider_status=status, payload=_safe_payload(fields), note=f"[{source}] {note}"[:255],
        )

    if not signature_valid:
        # Вміст неперевіреного повідомлення не зберігається (див. payment_views.liqpay_callback).
        return ProcessResult(ok=False, note="invalid_signature")
    rejection = _rejection_reason(fields, payment, config)
    if rejection:
        log(rejection[1])
        return ProcessResult(ok=True, note=rejection[0], payment=payment)
    if status in ("success", "sandbox"):
        return _handle_success(fields, payment, status, config, log)
    return _handle_other_status(fields, payment, status, log)


def handle_callback(data, signature, config=None):
    config = config or LiqPayConfig.from_settings()
    if not verify_signature(data, signature, config):
        return ProcessResult(ok=False, note="invalid_signature")
    fields = decode_data(data)
    if fields is None:
        PaymentEvent.objects.create(signature_valid=True, note="[callback] Некоректний формат data.")
        return ProcessResult(ok=False, note="bad_data")
    return process_payment_fields(fields, signature_valid=True, source="callback", config=config)


# --- Серверний запит статусу ---


def _post_api(params, config, timeout=15):
    data = encode_data(params)
    body = urllib.parse.urlencode({"data": data, "signature": make_signature(data, config)}).encode()
    req = urllib.request.Request(
        settings.LIQPAY_API_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # urllib перевіряє TLS-сертифікат за замовчуванням.
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - фіксований https URL
        return json.loads(resp.read().decode("utf-8"))


def fetch_status(payment, config=None):
    """
    Уточнює стан платежу запитом до LiqPay (action=status) і застосовує результат.
    Відповідь api/request за документацією — JSON без підпису; якщо ж вона містить data+signature,
    підпис перевіряється. В обох випадках public_key/order_id/сума/валюта звіряються з нашим записом.
    """
    config = config or LiqPayConfig.from_settings()
    if config.problems():
        return ProcessResult(ok=False, note="not_configured", payment=payment)
    params = {"action": "status", "version": config.version, "public_key": config.public_key,
              "order_id": payment.order_id}
    try:
        response = _post_api(params, config)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # OSError охоплює URLError/HTTPError, тайм-аути, ConnectionReset/RemoteDisconnected;
        # HTTPException — IncompleteRead тощо; ValueError — некоректний JSON.
        logger.warning("LiqPay status request failed for %s: %s", payment.order_id, exc.__class__.__name__)
        return ProcessResult(ok=False, note="network_error", payment=payment)

    if isinstance(response, dict) and "data" in response and "signature" in response:
        valid = verify_signature(response["data"], response["signature"], config)
        fields = decode_data(response["data"]) or {}
    else:
        valid = isinstance(response, dict)
        fields = response if valid else {}

    if not valid:
        PaymentEvent.objects.create(payment=payment, order_id=payment.order_id, signature_valid=False,
                                    note="[status] Некоректна або непідписана відповідь — проігноровано.")
        return ProcessResult(ok=False, note="invalid_response", payment=payment)
    if fields.get("result") == "error" and fields.get("status") in (None, "", "error"):
        # LiqPay не знає такого замовлення (користувач не почав оплату) — лише фіксуємо.
        PaymentEvent.objects.create(payment=payment, order_id=payment.order_id, signature_valid=valid,
                                    provider_status=str(fields.get("status", "")),
                                    payload=_safe_payload(fields), note="[status] Платіж у LiqPay не знайдено.")
        return ProcessResult(ok=True, note="not_found", payment=payment)
    if str(fields.get("order_id", "")) != payment.order_id:
        PaymentEvent.objects.create(payment=payment, order_id=payment.order_id, signature_valid=False,
                                    payload=_safe_payload(fields), note="[status] order_id у відповіді не збігається.")
        return ProcessResult(ok=False, note="order_mismatch", payment=payment)
    return process_payment_fields(fields, signature_valid=True, source="status", config=config)


def expire_stale(payment, now=None):
    """Позначає незавершений платіж простроченим після закінчення терміну на оплату."""
    now = now or timezone.now()
    limit = payment.created_at + payment_hold_window()
    if not payment.is_final() and now > limit:
        return mark_unsuccessful(payment.pk, PaymentStatus.EXPIRED, "Оплату не завершено вчасно.")
    return payment
