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

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    ModerationStatus,
    Payment,
    PaymentEvent,
    PaymentStatus,
    PlacementTransition,
    Property,
    Tariff,
)

TRANSITION_DAYS = 30


class PlacementError(Exception):
    pass


class DuplicateChargeError(PlacementError):
    """Успіх із ІНШИМ payment_id для вже зарахованого платежу: не зараховано, позначено для адміністратора."""


DUPLICATE_CHARGE_NOTE = "[ALERT duplicate_charge]"
DUPLICATE_REFUND_NOTE = "[ALERT duplicate_refund]"
DUPLICATE_PROPERTY_NOTE = "[ALERT duplicate_property_charge]"
AMOUNT_MISMATCH_NOTE = "[ALERT amount_mismatch]"
PAYMENT_REVERSED_NOTE = "[ALERT payment_reversed]"


def _flag_duplicate(payment, new_id, note_prefix, text, provider_status, **payload):
    """Викликається під select_for_update платежу. Одна подія кожного типу на кожен новий payment_id."""
    Payment.objects.filter(pk=payment.pk).update(needs_review=True, updated_at=timezone.now())
    already = PaymentEvent.objects.filter(
        payment=payment, note__startswith=note_prefix, payload__payment_id=new_id
    ).exists()
    if not already:
        PaymentEvent.objects.create(
            payment=payment, order_id=payment.order_id, signature_valid=True, provider_status=provider_status,
            # Лише ідентифікатори й сума — без карткових і персональних даних.
            payload={"order_id": payment.order_id, "payment_id": new_id, **payload},
            note=f"{note_prefix} {text}"[:255],
        )


def _flag_duplicate_charge(payment, new_id, paid_amount, paid_currency):
    _flag_duplicate(
        payment, new_id, DUPLICATE_CHARGE_NOTE,
        f"Друге успішне списання: payment_id {new_id}, зараховано раніше {payment.provider_transaction_id or '—'}. "
        "Не зараховано; перевірте і поверніть кошти.",
        "success", amount=str(paid_amount), currency=str(paid_currency)[:8],
    )


def _cross_order_duplicates(payment):
    """
    Інші платежі того самого оголошення, з якими зарахування `payment` (зараз) утворює ймовірне
    подвійне списання: старіший платіж отримав успіх уже ПІСЛЯ створення новішого. Звичайне
    продовження (A зараховано -> потім створено й оплачено B) сюди не потрапляє.
    Порядок платежів — (created_at, pk): однаковий created_at не ховає пару.
    Граничний випадок: applied_at старішого ТОЧНО дорівнює created_at новішого. Обидва значення —
    timezone.now() з точністю до мікросекунди (SQLite/PostgreSQL), а при нормальному продовженні
    B створюється окремим запитом уже після commit зарахування A, тож рівність означає, що порядок
    не встановлюється. Безпечна поведінка — позначити для перевірки (>=): хибна позначка коштує
    адміністратору перегляду, пропуск — непомічене подвійне списання.
    Викликається під select_for_update оголошення, тож паралельні зарахування бачать одне одного.
    """
    # Лише той самий тариф: оплата розміщення і ТОП (або два різні ТОП) — різні послуги, не подвійне списання.
    others = Payment.objects.filter(property_id=payment.property_id, tariff_id=payment.tariff_id).exclude(pk=payment.pk)
    newer = Q(created_at__gt=payment.created_at) | Q(created_at=payment.created_at, pk__gt=payment.pk)
    older = Q(created_at__lt=payment.created_at) | Q(created_at=payment.created_at, pk__lt=payment.pk)
    open_or_credited = (Q(status__in=OPEN_STATUSES, applied_at__isnull=True)
                        | Q(status=PaymentStatus.SUCCESS, applied_at__isnull=False))
    return list(others.filter(
        # Новіший платіж уже існує (відкритий або зарахований), а цей, старіший, зараховується лише зараз.
        (newer & open_or_credited)
        # Старіший платіж зарахований уже після створення цього.
        | (older & Q(status=PaymentStatus.SUCCESS, applied_at__gte=payment.created_at))
    ).order_by("created_at", "pk"))


def _property_duplicate_alert(this, that):
    """payload і note ALERT для пари: ID записів Payment і ID транзакцій LiqPay — окремі поля."""
    payload = {
        "type": "duplicate_property_charge",
        "property_id": this.property_id,
        "payment_record_id": this.pk,
        "order_id": this.order_id,
        "liqpay_payment_id": this.provider_transaction_id or "",
        "other_payment_record_id": that.pk,
        "other_order_id": that.order_id,
        "other_liqpay_payment_id": that.provider_transaction_id or "",
    }
    note = (f"{DUPLICATE_PROPERTY_NOTE} Оголошення {this.property_id}: {this.order_id} "
            f"(LiqPay {payload['liqpay_payment_id'] or '—'}) і {that.order_id} "
            f"(LiqPay {payload['other_liqpay_payment_id'] or '—'}). "
            "Можливе подвійне списання; перевірте й поверніть зайве вручну.")[:255]
    return payload, note


def _flag_property_duplicates(payment, others):
    """
    needs_review на обох платежах. Рівно одна ALERT-подія на кожен платіж пари: якщо ID транзакції
    другого платежу став відомим пізніше — наявна подія оновлюється (payload і note), а не дублюється.
    Лише ідентифікатори — без карткових і персональних даних.
    """
    payment.needs_review = True  # збережеться разом із зарахуванням
    Payment.objects.filter(pk__in=[o.pk for o in others]).update(needs_review=True, updated_at=timezone.now())
    for other in others:
        for this, that in ((payment, other), (other, payment)):
            payload, note = _property_duplicate_alert(this, that)
            existing = [e for e in PaymentEvent.objects.filter(payment_id=this.pk,
                                                               note__startswith=DUPLICATE_PROPERTY_NOTE)
                        if e.payload.get("other_payment_record_id") == that.pk]
            if existing:
                event = existing[0]
                if (event.payload, event.note) != (payload, note):
                    event.payload, event.note = payload, note
                    event.save(update_fields=["payload", "note"])
                continue
            # Внутрішня подія застосунку, а не повідомлення LiqPay: signature_valid=False і без
            # provider_status (інакше вона сприймалась би як останній перевірений статус провайдера,
            # напр. у last_attempt_try_again). Розпізнається за префіксом note «[ALERT».
            PaymentEvent.objects.create(payment_id=this.pk, order_id=this.order_id, signature_valid=False,
                                        provider_status="", payload=payload, note=note)


def _flag_amount_mismatch(payment, transaction_id, paid_amount, paid_currency):
    """
    Провайдер підтвердив оплату з іншою сумою/валютою: не зараховано, але кошти, можливо, списано —
    потрібне рішення адміністратора (needs_review). Одна ALERT-подія на кожну комбінацію
    payment_id + сума + валюта (повтори callback і sync не дублюють її). Внутрішня подія
    застосунку: signature_valid=False і без provider_status (як [ALERT duplicate_property_charge]).
    """
    payload = {"type": "amount_mismatch", "order_id": payment.order_id,
               "payment_id": str(transaction_id or "")[:64], "amount": str(paid_amount)[:32],
               "currency": str(paid_currency or "")[:8], "expected_amount": str(payment.amount),
               "expected_currency": payment.currency}
    if any(e.payload == payload for e in PaymentEvent.objects.filter(payment_id=payment.pk,
                                                                     note__startswith=AMOUNT_MISMATCH_NOTE)):
        return
    PaymentEvent.objects.create(
        payment_id=payment.pk, order_id=payment.order_id, signature_valid=False, provider_status="",
        payload=payload,
        note=(f"{AMOUNT_MISMATCH_NOTE} Очікувалось {payment.amount} {payment.currency}, LiqPay підтвердив "
              f"{payload['amount']} {payload['currency']}. Не зараховано; перевірте в кабінеті LiqPay і "
              "поверніть кошти.")[:255],
    )


def _flag_reversed(payment, reversed_id):
    """
    Викликається під select_for_update платежу. Створює подію [ALERT payment_reversed], якщо її ще немає;
    повертає True, лише якщо подію створено (тоді викликач ставить needs_review). Одна подія на платіж:
    зарахований payment_id у платежу лише один. Внутрішня подія застосунку: signature_valid=False і без
    provider_status (як інші ALERT, що не є повідомленнями LiqPay). Лише ідентифікатори й сума.
    """
    if PaymentEvent.objects.filter(payment_id=payment.pk, note__startswith=PAYMENT_REVERSED_NOTE).exists():
        return False
    payload = {"type": "payment_reversed", "property_id": payment.property_id, "payment_record_id": payment.pk,
               "order_id": payment.order_id, "liqpay_payment_id": payment.provider_transaction_id or "",
               "reversed_payment_id": reversed_id, "amount": str(payment.paid_amount),
               "currency": payment.paid_currency}
    PaymentEvent.objects.create(
        payment_id=payment.pk, order_id=payment.order_id, signature_valid=False, provider_status="",
        payload=payload,
        note=(f"{PAYMENT_REVERSED_NOTE} LiqPay повернув {payload['amount']} {payload['currency']} за зарахованим "
              f"платежем (LiqPay {payload['liqpay_payment_id'] or '—'}). Розміщення не змінено; вирішіть, "
              "чи залишати оплачений період.")[:255],
    )
    return True


def can_pay_for(user, prop):
    """Оплачувати може ЛИШЕ власник (перевірка на сервері; адміністратор — ні)."""
    if not user.is_authenticated or prop.archived_at is not None:
        return False
    return prop.owner_id is not None and prop.owner_id == user.id


OPEN_STATUSES = (PaymentStatus.CREATED, PaymentStatus.PENDING)


def open_payment_for(prop):
    """Незавершена оплата оголошення (та, що підпадає під one_open_payment_per_property)."""
    return (
        Payment.objects.filter(property=prop, status__in=OPEN_STATUSES, applied_at__isnull=True, error_message="")
        .order_by("-created_at")
        .first()
    )


def payment_hold_window():
    """Скільки після створення форму LiqPay ще можна оплатити: термін форми + запас на 3-D Secure."""
    return timedelta(minutes=settings.LIQPAY_PAYMENT_TTL_MINUTES + 30)


def new_payment_blocked_until(prop, is_test):
    """
    Поки форму попередньої НЕзарахованої оплати (скасованої, відхиленої, простроченої) ще можна
    оплатити в іншій вкладці, нова оплата заборонена — інакше можливе подвійне списання.
    Повертає час, після якого можна створити нову оплату, або None. Не довше payment_hold_window().
    """
    # Відкриті (created/pending) сюди не входять: їх продовжують, а гонку закриває індекс БД.
    closed = [PaymentStatus.CANCELLED, PaymentStatus.FAILURE, PaymentStatus.EXPIRED]
    window = payment_hold_window()
    last = (
        Payment.objects.filter(property=prop, is_test=is_test, status__in=closed, applied_at__isnull=True,
                               created_at__gt=timezone.now() - window)
        .order_by("-created_at")
        .first()
    )
    return last.created_at + window if last else None


def get_or_create_open_payment(user, prop, is_test=False, tariff=None):
    """
    Повертає (payment, created). Якщо незавершена оплата вже є — повертає її (без нової).
    Паралельні виклики: унікальний частковий індекс БД пропускає лише одну вставку;
    інші ловлять IntegrityError і повертають наявну оплату.
    """
    if not can_pay_for(user, prop):
        raise PlacementError("Оплатити можна лише власне активне оголошення.")
    existing = open_payment_for(prop)
    if existing is not None:
        return existing, False
    blocked_until = new_payment_blocked_until(prop, is_test)
    if blocked_until is not None:
        raise PlacementError(
            "Попередню форму оплати LiqPay ще можна оплатити в іншій вкладці. Щоб уникнути подвійного "
            f"списання, нову оплату можна створити після {timezone.localtime(blocked_until):%H:%M}."
        )
    try:
        with transaction.atomic():
            return create_payment(user, prop, tariff=tariff, is_test=is_test), True
    except IntegrityError:
        existing = open_payment_for(prop)
        if existing is None:
            raise PlacementError("Не вдалося створити оплату. Спробуйте ще раз.")
        return existing, False


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
        duration_hours=tariff.duration_hours,
        promotion_rank=tariff.promotion_rank,
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


def _log_resubmission(prop):
    """Історія модерації й сповіщення модераторів, коли оплата простроченого оголошення повертає його в чергу."""
    from .models import ModerationAction, ModerationEvent, ModerationSource

    ModerationEvent.objects.create(property=prop, action=ModerationAction.SUBMITTED, source=ModerationSource.SYSTEM,
                                   from_status=ModerationStatus.APPROVED, to_status=prop.moderation_status,
                                   reason="Оплата розміщення простроченого оголошення — повторна модерація.",
                                   version=prop.moderation_version)
    from .moderation import _notify_after_commit

    _notify_after_commit(prop.pk)


def is_catalog_visible(prop, now=None):
    """Оголошення показується в каталозі (ті самі умови, що Property.objects.public())."""
    now = now or timezone.now()
    if not is_publishable(prop):
        return False
    if getattr(settings, "PLACEMENT_PAYMENT_REQUIRED", False):
        return prop.placement_until is not None and prop.placement_until > now
    return True


def _activate_promotion(payment, now):
    """
    Запускає оплачене просування. Оголошення не змінюється: ранг у каталозі обчислюється з активних
    періодів платежів (active_promotions), тож після period_end просування зникає саме.
    Кілька оплат того самого рангу йдуть послідовно (дні не губляться); різні ранги — незалежно.
    """
    latest_end = (Payment.objects.filter(property_id=payment.property_id, status=PaymentStatus.SUCCESS,
                                         promotion_rank=payment.promotion_rank, period_end__gt=now)
                  .exclude(pk=payment.pk).order_by("-period_end").values_list("period_end", flat=True).first())
    start = latest_end or now
    payment.period_start, payment.period_end, payment.activated_at = start, start + payment.duration(), now


def _activate(payment, prop, now):
    start = prop.placement_until if prop.placement_until and prop.placement_until > now else now
    end = start + timedelta(days=payment.duration_days)
    payment.period_start, payment.period_end, payment.activated_at = start, end, now
    prop.placement_until = end


def _apply_period_rules(payment, prop, now):
    """Визначає, що робити з оголошенням після зарахованої оплати. Повертає змінені поля."""
    if payment.promotion_rank is not None:
        # Просування: лише для оголошення, яке вже показується в каталозі; інакше — при публікації
        # після модерації (activate_on_publication). Модерацію й розміщення не змінює.
        if is_catalog_visible(prop, now):
            _activate_promotion(payment, now)
        return []
    if is_live(prop, now):
        # Продовження активного оголошення: +30 днів до поточної дати завершення.
        _activate(payment, prop, now)
        return ["placement_until"]
    if prop.placement_until is not None and prop.placement_until <= now:
        # Прострочене: повертається на модерацію; дні стартують з повторної публікації.
        if prop.moderation_status == ModerationStatus.APPROVED:
            # Повторна модерація простроченого оголошення (оплата НЕ схвалює — лише повертає в чергу).
            prop.moderation_status = ModerationStatus.PENDING
            prop.moderation_version += 1
            prop.moderation_submitted_at = now
            return ["moderation_status", "moderation_version", "moderation_submitted_at"]
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

    Ідемпотентність і захист від паралельних callback: рядок платежу блокується
    (select_for_update), і вся перевірка й зарахування виконуються під цим блокуванням.
    Для вже зарахованого платежу: той самий payment_id — тихий повтор; ІНШИЙ payment_id —
    друге списання: нічого не зараховується, платіж позначається needs_review, створюється
    подія [ALERT duplicate_charge], після фіксації транзакції — DuplicateChargeError.
    """
    new_id = str(transaction_id)[:64] if transaction_id else ""
    with transaction.atomic():
        # Спершу оголошення, потім платіж — той самий порядок, що в activate_on_publication. Зарахування
        # різних order_id одного оголошення серіалізуються й бачать одне одного (_cross_order_duplicates).
        property_id = Payment.objects.values_list("property_id", flat=True).get(pk=payment_id)
        list(Property.objects.select_for_update().filter(pk=property_id).values_list("pk", flat=True))
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.applied_at is None and payment.status != PaymentStatus.REVERSED:
            return _credit_locked(payment, paid_amount, paid_currency, transaction_id, paid_at)
        if not new_id or new_id == (payment.provider_transaction_id or ""):
            return payment, False  # той самий payment_id — ідемпотентний повтор
        _flag_duplicate_charge(payment, new_id, paid_amount, paid_currency)
    # Прапорець і подію вже збережено (транзакцію зафіксовано) — повідомляємо викликача.
    raise DuplicateChargeError(f"Друге успішне списання за order_id {payment.order_id}: payment_id {new_id}.")


def _credit_locked(payment, paid_amount, paid_currency, transaction_id, paid_at):
    """Зарахування незарахованого платежу; викликається під select_for_update у транзакції."""
    if Decimal(str(paid_amount)) != payment.amount or (paid_currency or "").upper() != payment.currency:
        Payment.objects.filter(pk=payment.pk, applied_at__isnull=True).update(
            status=PaymentStatus.PENDING,
            error_message=(
                f"Невідповідність суми/валюти: очікувалось {payment.amount} {payment.currency}, "
                f"отримано {paid_amount} {paid_currency}. Потрібна ручна перевірка."
            )[:255],
            needs_review=True,
            updated_at=timezone.now(),
        )
        _flag_amount_mismatch(payment, transaction_id, paid_amount, paid_currency)
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
    duplicates = _cross_order_duplicates(payment)
    try:
        with transaction.atomic():
            payment.save()
    except IntegrityError:
        raise PlacementError("Ця транзакція провайдера вже прив'язана до іншого платежу.")
    if prop_fields:
        prop.save(update_fields=prop_fields + ["updated_at"])
        if "moderation_status" in prop_fields:
            _log_resubmission(prop)
    if duplicates:
        _flag_property_duplicates(payment, duplicates)
        payment.save(update_fields=["needs_review", "updated_at"])
    return payment, True


def mark_reversed(payment_id, message="", transaction_id=None):
    """
    Повернення коштів провайдером. Період не скасовується автоматично — потрібне рішення адміна.
    Повернення ЗАРАХОВАНОГО платежу: needs_review і рівно одна подія [ALERT payment_reversed]
    (повтори callback і Status API її не дублюють і не відкривають уже закриту перевірку знову).
    Дані зарахування (payment_id, сума, дати, період) і оголошення (публікація, модерація) не змінюються.
    Повернення ІНШОГО payment_id, ніж зарахований (друге списання за тим самим order_id), не
    чіпає зарахований платіж: needs_review, подія [ALERT duplicate_refund], DuplicateChargeError.
    """
    new_id = str(transaction_id)[:64] if transaction_id else ""
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        credited_id = payment.provider_transaction_id or ""
        if not (new_id and credited_id and new_id != credited_id):
            payment.status = PaymentStatus.REVERSED
            payment.error_message = (message or "Кошти повернено. Перевірте розміщення вручну.")[:255]
            fields = ["status", "error_message", "updated_at"]
            if payment.applied_at is not None and _flag_reversed(payment, new_id):
                payment.needs_review = True
                fields.append("needs_review")
            payment.save(update_fields=fields)
            return payment
        _flag_duplicate(
            payment, new_id, DUPLICATE_REFUND_NOTE,
            f"Повернення payment_id {new_id} (друге списання). Зарахований платіж {credited_id} не змінено.",
            "reversed",
        )
    raise DuplicateChargeError(f"Повернення другого списання за order_id {payment.order_id}: payment_id {new_id}.")


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
        activated = 0
        placements = [p for p in pending if p.promotion_rank is None]
        for payment in placements:
            _activate(payment, prop, now)
            payment.save(update_fields=["period_start", "period_end", "activated_at", "updated_at"])
            activated += 1
        if placements:
            prop.save(update_fields=["placement_until", "updated_at"])
        if is_catalog_visible(prop, now):
            for payment in (p for p in pending if p.promotion_rank is not None):
                _activate_promotion(payment, now)
                payment.save(update_fields=["period_start", "period_end", "activated_at", "updated_at"])
                activated += 1
        return activated


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


def active_promotions(qs, now=None):
    """
    Анотує оголошення активним просуванням: promo_rank (1 — ТОП №1, 3 — ТОП №3, 5 — активна публікація,
    None — немає), promo_since, promo_until. Ураховуються лише зараховані (success) платежі з діючим
    періодом: прострочене чи повернене (reversed) просування не впливає на каталог.
    """
    from django.db.models import OuterRef, Subquery

    now = now or timezone.now()
    current = (Payment.objects.filter(property=OuterRef("pk"), status=PaymentStatus.SUCCESS,
                                      promotion_rank__isnull=False, period_start__lte=now, period_end__gt=now)
               .order_by("promotion_rank", "-period_start", "-pk"))
    return qs.annotate(promo_rank=Subquery(current.values("promotion_rank")[:1]),
                       promo_since=Subquery(current.values("period_start")[:1]),
                       promo_until=Subquery(current.values("period_end")[:1]))


def promotion_ordering(*tail):
    """Порядок каталогу: ТОП №1 → ТОП №3 → активна публікація → решта; у межах рангу — свіжіша активація."""
    from django.db.models import F

    return (F("promo_rank").asc(nulls_last=True), F("promo_since").desc(nulls_last=True), *tail, "-pk")
