"""Представлення оплати розміщення через LiqPay."""

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from . import liqpay
from .models import Payment, PaymentEvent, PaymentStatus, Property
from .placement import (
    PlacementError,
    can_pay_for,
    get_or_create_open_payment,
    mark_unsuccessful,
    open_payment_for,
    payment_hold_window,
)

logger = logging.getLogger(__name__)

MAX_PAYMENTS_PER_HOUR = 10
STATUS_CHECK_INTERVAL_SECONDS = 10


@login_required(login_url="/auth/")
@require_POST
def start_payment(request, pk):
    """
    Оплата розміщення (99 UAH за тарифом; суму визначає сервер) через LiqPay Checkout.
    Лише власник оголошення (перевірка на сервері за request.user). Якщо незавершена оплата
    вже існує — продовжується вона (той самий order_id), нова не створюється.
    """
    prop = get_object_or_404(Property, pk=pk)
    if not can_pay_for(request.user, prop):
        raise Http404
    if not liqpay.payments_active():
        messages.info(request, "Онлайн-оплата тимчасово недоступна.")
        return redirect("properties:payment", pk=pk)
    config = liqpay.LiqPayConfig.from_settings()

    existing = open_payment_for(prop)
    if existing is not None:
        if liqpay.is_stale(existing):
            # Перш ніж закрити прострочену оплату й дозволити нову, уточнюємо її стан у LiqPay:
            # оплачений, але не підтверджений callback платіж не можна «прострочити» (подвійне списання).
            status = liqpay.fetch_status(existing, config)
            existing.refresh_from_db()
            existing = liqpay.expire_if_confirmed(existing, status)
            if existing.status == PaymentStatus.SUCCESS:
                messages.success(request, "Попередню оплату підтверджено LiqPay. Нову оплату не створено.")
                return redirect("properties:payment_result", order_id=existing.order_id)
            if not existing.is_final():
                messages.error(request, "Не вдалося підтвердити стан попередньої оплати в LiqPay. Щоб уникнути "
                                        "подвійного списання, нову оплату не створено. Спробуйте пізніше.")
                return redirect("properties:payment", pk=pk)
        if existing.is_test != config.sandbox and not existing.is_final():
            # Режим LiqPay змінився (тест <-> бойовий): стару незавершену оплату закриваємо.
            existing = mark_unsuccessful(existing.pk, PaymentStatus.CANCELLED, "Змінено режим LiqPay.")
    if open_payment_for(prop) is None:
        recent = Payment.objects.filter(user=request.user, created_at__gte=timezone.now() - timedelta(hours=1))
        if recent.count() >= MAX_PAYMENTS_PER_HOUR:
            messages.error(request, "Забагато спроб оплати. Спробуйте пізніше.")
            return redirect("properties:payment", pk=pk)

    try:
        payment, _created = get_or_create_open_payment(request.user, prop, is_test=config.sandbox)
    except PlacementError as exc:
        messages.error(request, str(exc))
        return redirect("properties:payment", pk=pk)
    if payment.status == PaymentStatus.PENDING:
        messages.info(request, "Оплата вже обробляється LiqPay. Дочекайтеся підтвердження.")
        return redirect("properties:payment_result", order_id=payment.order_id)
    if timezone.now() >= payment.created_at + timedelta(minutes=settings.LIQPAY_PAYMENT_TTL_MINUTES):
        # Форма в LiqPay уже недійсна, але 3-D Secure ще може завершитись — чекаємо expire_stale.
        free_at = timezone.localtime(payment.created_at + payment_hold_window())
        messages.info(request, f"Термін цієї форми оплати минув. Нову оплату можна створити після {free_at:%H:%M}.")
        return redirect("properties:payment", pk=pk)
    # Поля форми беруться лише з серверного запису; браузер не може змінити суму/валюту
    # без порушення підпису. Повторний перехід використовує той самий order_id.
    checkout = liqpay.build_checkout(payment, config)
    return render(request, "properties/liqpay_redirect.html", {"checkout": checkout, "payment": payment})


# Захист callback від зловживань (А2)
MAX_CALLBACK_BYTES = 32 * 1024  # реальні повідомлення LiqPay — кілька КБ
INVALID_CALLBACK_WINDOW = 600  # с
INVALID_CALLBACK_PER_IP = 20  # неправильних запитів з однієї IP за вікно
INVALID_CALLBACK_DB_RECORDS = 50  # максимум записів про неправильні запити за вікно (глобально)
ORDER_ID_RE = re.compile(r"^mh-\d{1,12}-[0-9a-f]{20}$")


def _bump(key):
    if cache.add(key, 1, INVALID_CALLBACK_WINDOW):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, INVALID_CALLBACK_WINDOW)
        return 1


def _record_invalid_callback(request, reason, data):
    """Мінімальний запис без вмісту запиту; кількість записів обмежена."""
    ip = request.META.get("REMOTE_ADDR", "unknown")
    per_ip = _bump(f"liqpay-cb-invalid:ip:{ip}")
    total = _bump("liqpay-cb-invalid:total")
    if per_ip > INVALID_CALLBACK_PER_IP:
        return True  # перевищено ліміт для IP
    if total <= INVALID_CALLBACK_DB_RECORDS:
        fields = liqpay.decode_data(data) or {}
        order_id = str(fields.get("order_id", ""))
        PaymentEvent.objects.create(
            order_id=order_id if ORDER_ID_RE.match(order_id) else "",
            signature_valid=False,
            note=f"[callback] Відхилено: {reason}; розмір data={len(data)} байт."[:255],
        )
    logger.warning("Rejected LiqPay callback (%s) from %s", reason, ip)
    return False


@csrf_exempt  # server-to-server від LiqPay; автентичність — через підпис
@require_POST
def liqpay_callback(request):
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = MAX_CALLBACK_BYTES + 1
    if length > MAX_CALLBACK_BYTES:
        return HttpResponse("too large", status=413, content_type="text/plain")
    data = request.POST.get("data", "")
    result = liqpay.handle_callback(data, request.POST.get("signature", ""))
    if result.note == "invalid_signature":
        # Перевірка підпису йде ДО ліміту: справжні повідомлення LiqPay обробляються завжди,
        # навіть якщо хтось «засмічує» endpoint з тієї ж IP (напр. за проксі).
        limited = _record_invalid_callback(request, "недійсний підпис", data)
        return HttpResponse("too many", status=429) if limited else HttpResponseBadRequest("invalid")
    if not result.ok:
        return HttpResponseBadRequest("invalid")
    return HttpResponse("OK", content_type="text/plain")


def _own_payment_or_404(request, order_id):
    payment = get_object_or_404(Payment.objects.select_related("property"), order_id=order_id)
    if not (request.user.is_staff or payment.user_id == request.user.id):
        raise Http404
    return payment


@csrf_exempt  # LiqPay може повернути користувача POST-запитом; тіло запиту повністю ігнорується
@login_required(login_url="/auth/")
@require_http_methods(["GET", "POST"])
def payment_result(request, order_id):
    """
    Сторінка повернення з LiqPay (result_url). НЕ підтверджує оплату: показує стан із БД і,
    якщо платіж ще не завершено, уточнює його серверним запитом статусу до LiqPay.
    Будь-які параметри в адресі чи тілі запиту (зокрема data/signature) ігноруються.
    """
    payment = _own_payment_or_404(request, order_id)
    if not payment.is_final() and liqpay.payments_active():
        key = f"liqpay-status-check:{payment.order_id}"
        if cache.add(key, 1, STATUS_CHECK_INTERVAL_SECONDS):
            liqpay.fetch_status(payment)
            payment.refresh_from_db()
    try_again = liqpay.last_attempt_try_again(payment)
    # LiqPay підтвердив іншу суму/валюту (не зараховано) — чекати автоматично нічого: рішення адміністратора.
    amount_mismatch = payment.status == PaymentStatus.PENDING and bool(payment.error_message)
    return render(request, "properties/payment_result.html", {
        "payment": payment,
        "property": payment.property,
        "is_pending": not payment.is_final() and not try_again and not amount_mismatch,
        "amount_mismatch": amount_mismatch,
        "try_again": try_again,
        "can_retry": try_again and liqpay.payments_active() and timezone.now()
        < payment.created_at + timedelta(minutes=settings.LIQPAY_PAYMENT_TTL_MINUTES),
        "PaymentStatus": PaymentStatus,
    })


@login_required(login_url="/auth/")
@require_POST
def cancel_payment(request, order_id):
    """Користувач відмовився від незавершеної оплати. Пізній успішний callback усе одно буде зараховано."""
    payment = _own_payment_or_404(request, order_id)
    # Платіж із підтвердженою LiqPay, але невідповідною сумою (error_message) не скасовується користувачем:
    # кошти, можливо, списано, рішення — за адміністратором.
    if not payment.is_final() and not payment.error_message:
        mark_unsuccessful(payment.pk, PaymentStatus.CANCELLED, "Скасовано користувачем.")
        messages.info(request, "Оплату скасовано.")
    return redirect("properties:payment", pk=payment.property_id)
