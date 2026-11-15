"""
Бізнес-логіка платного розміщення квартири (затверджені правила).

- Тариф «Розміщення квартири»: одна оплата — одне оголошення — один період (30 днів).
- Відлік оплаченого періоду починається з ФАКТИЧНОЇ ПУБЛІКАЦІЇ після модерації,
  а не з моменту оплати:
    * нове оголошення: оплачено -> модерація -> схвалення -> старт 30 днів;
    * прострочене оголошення: після оплати повертається на модерацію,
      30 днів починаються з повторної публікації;
    * активне (опубліковане, з діючим періодом) оголошення: 30 днів одразу
      додаються до поточної дати завершення, без повторної модерації.
- Оплата сама по собі ніколи не публікує й не схвалює оголошення.
- Успішний платіж зараховується рівно один раз (ідемпотентно); сума та валюта,
  підтверджені провайдером, мають збігатися з очікуваними.
- Перехідний період для оголошень, опублікованих до запуску обов'язкової оплати:
  одноразово, 30 днів від дати активації платної системи (apply_transition).
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    ModerationStatus,
    Payment,
    PaymentStatus,
    PlacementTransition,
    Property,
    Tariff,
)

TRANSITION_DAYS = 30


class PlacementError(Exception):
    pass


def can_pay_for(user, prop):
    if not user.is_authenticated:
        return False
    if prop.archived_at is not None:
        return False
    return user.is_staff or prop.owner_id == user.id


def create_payment(user, prop, tariff=None, is_test=False):
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
        is_test=is_test,
    )


def is_live(prop, now=None):
    """Оголошення опубліковане, схвалене, фото перевірено, не в архіві і період діє."""
    now = now or timezone.now()
    placement_running = prop.placement_until is not None and prop.placement_until > now
    return is_publishable(prop) and placement_running


def is_publishable(prop):
    """Пройшло модерацію і може показуватись (без огляду на оплату)."""
    approved = prop.moderation_status == ModerationStatus.APPROVED
    return all((prop.is_published, approved, prop.is_verified_by_admin, prop.archived_at is None))


def _activate(payment, prop, now):
    start = prop.placement_until if prop.placement_until and prop.placement_until > now else now
    end = start + timedelta(days=payment.duration_days)
    payment.period_start, payment.period_end, payment.activated_at = start, end, now
    prop.placement_until = end


def _apply_period_rules(payment, prop, now):
    """Визначає, що робити з оголошенням після зарахованої оплати. Повертає змінені поля."""
    if is_live(prop, now):
        # Продовження активного оголошення: +30 днів до поточної дати завершення.
        _activate(payment, prop, now)
        return ["placement_until"]
    if prop.placement_until is not None and prop.placement_until <= now:
        # Прострочене: повертається на модерацію; дні стартують з повторної публікації.
        if prop.moderation_status == ModerationStatus.APPROVED:
            prop.moderation_status = ModerationStatus.PENDING
            return ["moderation_status"]
        return []
    if prop.placement_until is None and is_publishable(prop):
        # Уже схвалене модератором, але ще не оплачувалось — публікація починається зараз.
        _activate(payment, prop, now)
        return ["placement_until"]
    # Нове / на модерації — дні активуються при схваленні (activate_on_publication).
    return []


def apply_successful_payment(payment_id, *, paid_amount, paid_currency, transaction_id=None, paid_at=None):
    """
    Зараховує підтверджений провайдером успішний платіж. Повертає (payment, credited: bool).

    Ідемпотентність і захист від паралельних callback: платіж «захоплюється» атомарним
    умовним UPDATE (applied_at IS NULL -> now). Лише один виклик отримує rowcount=1;
    решта нічого не змінюють. Усе виконується в одній транзакції (відкат при помилці).
    """
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.applied_at is not None or payment.status == PaymentStatus.REVERSED:
            return payment, False

        if Decimal(str(paid_amount)) != payment.amount or (paid_currency or "").upper() != payment.currency:
            Payment.objects.filter(pk=payment.pk, applied_at__isnull=True).update(
                status=PaymentStatus.PENDING,
                error_message=(
                    f"Невідповідність суми/валюти: очікувалось {payment.amount} {payment.currency}, "
                    f"отримано {paid_amount} {paid_currency}. Потрібна ручна перевірка."
                )[:255],
                updated_at=timezone.now(),
            )
            payment.refresh_from_db()
            return payment, False

        now = timezone.now()
        claimed = Payment.objects.filter(pk=payment.pk, applied_at__isnull=True).update(applied_at=now)
        if claimed != 1:
            payment.refresh_from_db()
            return payment, False

        prop = Property.objects.select_for_update().get(pk=payment.property_id)
        payment.applied_at = now
        payment.status = PaymentStatus.SUCCESS
        payment.paid_amount = Decimal(str(paid_amount))
        payment.paid_currency = paid_currency.upper()
        payment.paid_at = paid_at or now
        payment.error_message = ""
        if transaction_id:
            payment.provider_transaction_id = str(transaction_id)[:64]

        prop_fields = _apply_period_rules(payment, prop, now)
        try:
            with transaction.atomic():
                payment.save()
        except IntegrityError:
            raise PlacementError("Ця транзакція провайдера вже прив'язана до іншого платежу.")
        if prop_fields:
            prop.save(update_fields=prop_fields + ["updated_at"])
        return payment, True


def mark_reversed(payment_id, message=""):
    """Повернення коштів провайдером. Період не скасовується автоматично — потрібне рішення адміна."""
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        payment.status = PaymentStatus.REVERSED
        payment.error_message = (message or "Кошти повернено. Перевірте розміщення вручну.")[:255]
        payment.save(update_fields=["status", "error_message", "updated_at"])
        return payment


def activate_on_publication(prop_id):
    """
    Викликається після схвалення/публікації оголошення. Якщо воно публікується і має
    зараховані, але ще не активовані оплати — запускає їхні періоди від поточного моменту
    (послідовно, якщо оплат кілька). Повертає кількість активованих оплат.
    """
    with transaction.atomic():
        prop = Property.objects.select_for_update().get(pk=prop_id)
        if not is_publishable(prop):
            return 0
        pending = list(
            Payment.objects.select_for_update()
            .filter(property=prop, status=PaymentStatus.SUCCESS,
                    applied_at__isnull=False, activated_at__isnull=True)
            .order_by("paid_at", "pk")
        )
        if not pending:
            return 0
        now = timezone.now()
        for payment in pending:
            _activate(payment, prop, now)
            payment.save(update_fields=["period_start", "period_end", "activated_at", "updated_at"])
        prop.save(update_fields=["placement_until", "updated_at"])
        return len(pending)


def transition_candidates():
    """Оголошення, опубліковані до запуску оплати: пройшли модерацію і ще без періоду."""
    return Property.objects.filter(
        is_published=True,
        moderation_status=ModerationStatus.APPROVED,
        is_verified_by_admin=True,
        archived_at__isnull=True,
        placement_until__isnull=True,
    )


def apply_transition(starts_at, applied_by=""):
    """
    Одноразово дає наявним опублікованим оголошенням період до starts_at + 30 днів.
    Змінює ЛИШЕ placement_until (без зміни статусів і дат оновлення). Повторно не виконується.
    """
    with transaction.atomic():
        if PlacementTransition.objects.select_for_update().exists():
            raise PlacementError("Перехідний період уже застосовано — повторне застосування заборонено.")
        ends_at = starts_at + timedelta(days=TRANSITION_DAYS)
        ids = list(transition_candidates().values_list("pk", flat=True))
        # update() не змінює updated_at та інші поля — лише дату завершення розміщення.
        Property.objects.filter(pk__in=ids).update(placement_until=ends_at)
        return PlacementTransition.objects.create(
            starts_at=starts_at, ends_at=ends_at, listings_count=len(ids),
            listing_ids=ids, applied_by=applied_by[:150],
        )


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
