"""
Бізнес-логіка платного розміщення квартири.

Правила (затверджені):
- Тариф «Розміщення квартири»: одна оплата — одне оголошення — один період.
- Якщо розміщення ще діє, новий період додається до поточної дати завершення;
  якщо завершилося (або ще не було) — рахується від моменту підтвердження оплати.
- Оплата НЕ змінює статус модерації та не публікує оголошення: публічність
  визначається модерацією + оплаченим періодом.
- Успішний платіж застосовується до оголошення рівно один раз (ідемпотентно).
- Сума та валюта, підтверджені провайдером, мають збігатися з очікуваними.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Payment, PaymentStatus, Property, Tariff


class PlacementError(Exception):
    pass


def can_pay_for(user, prop):
    if not user.is_authenticated:
        return False
    if prop.archived_at is not None:
        return False
    return user.is_staff or prop.owner_id == user.id


def create_payment(user, prop, tariff=None):
    """Створює платіж зі знімком умов тарифу. Нічого не змінює в оголошенні."""
    tariff = tariff or Tariff.placement()
    if tariff is None or not tariff.is_active:
        raise PlacementError("Тариф розміщення недоступний.")
    if not can_pay_for(user, prop):
        raise PlacementError("Оплатити можна лише власне активне оголошення.")
    return Payment.objects.create(
        order_id=f"mh-{prop.pk}-{uuid.uuid4().hex[:20]}",
        user=user,
        user_snapshot=f"{user.username} <{user.email}>"[:255],
        property=prop,
        tariff=tariff,
        tariff_name=tariff.name,
        amount=tariff.price,
        currency=tariff.currency,
        duration_days=tariff.duration_days,
    )


def compute_period(prop, duration_days, now=None):
    now = now or timezone.now()
    start = prop.placement_until if prop.placement_until and prop.placement_until > now else now
    return start, start + timedelta(days=duration_days)


def apply_successful_payment(payment_id, *, paid_amount, paid_currency, transaction_id=None, paid_at=None):
    """
    Застосовує підтверджений провайдером успішний платіж. Повертає (payment, applied: bool).
    Повторний виклик для того самого платежу нічого не змінює.
    """
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.applied_at is not None:
            return payment, False
        if payment.status == PaymentStatus.REVERSED:
            return payment, False

        if Decimal(str(paid_amount)) != payment.amount or (paid_currency or "").upper() != payment.currency:
            payment.status = PaymentStatus.PENDING
            payment.error_message = (
                f"Невідповідність суми/валюти: очікувалось {payment.amount} {payment.currency}, "
                f"отримано {paid_amount} {paid_currency}. Потрібна ручна перевірка."
            )[:255]
            payment.save(update_fields=["status", "error_message", "updated_at"])
            return payment, False

        prop = Property.objects.select_for_update().get(pk=payment.property_id)
        now = timezone.now()
        start, end = compute_period(prop, payment.duration_days, now)
        prop.placement_until = end
        prop.save(update_fields=["placement_until", "updated_at"])

        payment.status = PaymentStatus.SUCCESS
        payment.paid_amount = Decimal(str(paid_amount))
        payment.paid_currency = paid_currency.upper()
        payment.paid_at = paid_at or now
        payment.period_start = start
        payment.period_end = end
        payment.applied_at = now
        payment.error_message = ""
        if transaction_id:
            payment.provider_transaction_id = str(transaction_id)[:64]
        try:
            with transaction.atomic():
                payment.save()
        except IntegrityError:
            raise PlacementError("Ця транзакція провайдера вже прив'язана до іншого платежу.")
        return payment, True


def mark_unsuccessful(payment_id, status, message=""):
    """Фіксує відхилену/скасовану/незавершену оплату. Успішний застосований платіж не чіпає."""
    if status not in (PaymentStatus.FAILURE, PaymentStatus.CANCELLED, PaymentStatus.EXPIRED, PaymentStatus.PENDING):
        raise ValueError(status)
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.applied_at is not None:
            return payment
        payment.status = status
        payment.error_message = (message or "")[:255]
        payment.save(update_fields=["status", "error_message", "updated_at"])
        return payment
