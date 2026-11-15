"""Представлення оплати розміщення через LiqPay."""

import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from . import liqpay
from .models import Payment, PaymentStatus, Property
from .placement import PlacementError, can_pay_for, create_payment, mark_unsuccessful

logger = logging.getLogger(__name__)

MAX_PAYMENTS_PER_HOUR = 10
STATUS_CHECK_INTERVAL_SECONDS = 10


@login_required(login_url="/auth/")
@require_POST
def start_payment(request, pk):
    """Створює платіж (99 UAH за поточним тарифом, визначає сервер) і веде на LiqPay Checkout."""
    prop = get_object_or_404(Property, pk=pk)
    if not can_pay_for(request.user, prop):
        raise Http404
    if not liqpay.payments_active():
        messages.info(request, "Онлайн-оплата тимчасово недоступна.")
        return redirect("properties:payment", pk=pk)
    recent = Payment.objects.filter(user=request.user, created_at__gte=timezone.now() - timedelta(hours=1))
    if recent.count() >= MAX_PAYMENTS_PER_HOUR:
        messages.error(request, "Забагато спроб оплати. Спробуйте пізніше.")
        return redirect("properties:payment", pk=pk)
    config = liqpay.LiqPayConfig.from_settings()
    try:
        payment = create_payment(request.user, prop, is_test=config.sandbox)
    except PlacementError as exc:
        messages.error(request, str(exc))
        return redirect("properties:payment", pk=pk)
    # Поля форми беруться лише з серверного запису; браузер не може змінити суму/валюту
    # без порушення підпису.
    checkout = liqpay.build_checkout(payment, config)
    return render(request, "properties/liqpay_redirect.html", {"checkout": checkout, "payment": payment})


@csrf_exempt  # server-to-server від LiqPay; автентичність — через підпис
@require_POST
def liqpay_callback(request):
    result = liqpay.handle_callback(request.POST.get("data", ""), request.POST.get("signature", ""))
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
    return render(request, "properties/payment_result.html", {
        "payment": payment,
        "property": payment.property,
        "is_pending": not payment.is_final(),
        "PaymentStatus": PaymentStatus,
    })


@login_required(login_url="/auth/")
@require_POST
def cancel_payment(request, order_id):
    """Користувач відмовився від незавершеної оплати. Пізній успішний callback усе одно буде зараховано."""
    payment = _own_payment_or_404(request, order_id)
    if not payment.is_final():
        mark_unsuccessful(payment.pk, PaymentStatus.CANCELLED, "Скасовано користувачем.")
        messages.info(request, "Оплату скасовано.")
    return redirect("properties:payment", pk=payment.property_id)
