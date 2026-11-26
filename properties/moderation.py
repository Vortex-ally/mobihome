"""
Спільний сервіс модерації оголошень — єдине місце, де змінюється статус модерації
(кабінет персоналу, Django Admin, Telegram-бот, подання власником).

Правила:
- подання/повторне подання -> «На перевірці» (pending), оголошення не показується в каталозі;
- схвалення -> «Схвалено» + опубліковано + фото перевірено; оплачені періоди стартують (сигнал post_save);
- відхилення — лише з причиною; власник бачить причину в кабінеті;
- кожна дія — під select_for_update рядка оголошення, з перевіркою поточного статусу (і версії, якщо
  передано): одночасні «схвалити» й «відхилити» від різних модераторів — спрацює лише перша;
- кожна дія записується в ModerationEvent; версія заявки збільшується (застарілі кнопки не спрацюють);
- оплата ніколи не схвалює оголошення (цей модуль не викликається з платіжного коду).
"""

import logging

from django.db import transaction
from django.utils import timezone

from .models import ModerationAction, ModerationEvent, ModerationSource, ModerationStatus, Property

logger = logging.getLogger(__name__)

MODERATE_PERMISSION = "properties.moderate_property"
MODERATORS_GROUP = "Модератори"


class ModerationError(Exception):
    """Дію не виконано (застаріла заявка, вже вирішено, немає прав, немає причини). Текст — для людини."""


def can_moderate(user):
    """Активний персонал із правом модерації (суперкористувач — завжди). Перевіряється щоразу заново."""
    return bool(user and user.is_active and user.is_staff
                and (user.is_superuser or user.has_perm(MODERATE_PERMISSION)))


def _actor_label(actor):
    return actor.get_username()[:150] if actor else ""


def _log(prop, actor, action, source, from_status, reason=""):
    return ModerationEvent.objects.create(
        property=prop, actor=actor, actor_snapshot=_actor_label(actor), action=action, source=source,
        from_status=from_status or "", to_status=prop.moderation_status, reason=reason[:2000],
        version=prop.moderation_version)


def _notify_after_commit(prop_id):
    def send():
        try:
            from .telegram_bot import notify_new_submission

            notify_new_submission(prop_id)
        except Exception:  # noqa: BLE001 — збій Telegram не ламає модерацію/створення оголошення
            logger.exception("Telegram notification failed for property %s", prop_id)

    transaction.on_commit(send)


def submit(prop_id, *, actor=None, source=ModerationSource.OWNER):
    """
    Подати (або повторно подати) оголошення на модерацію: pending, прихований із каталогу до схвалення.
    Повертає оновлений Property. Архівне оголошення подати не можна.
    """
    with transaction.atomic():
        prop = Property.objects.select_for_update().get(pk=prop_id)
        if prop.archived_at:
            raise ModerationError("Архівне оголошення не можна подати на модерацію.")
        previous = prop.moderation_status
        prop.moderation_status = ModerationStatus.PENDING
        prop.is_published = True
        prop.is_verified_by_admin = False
        prop.moderation_version += 1
        prop.moderation_submitted_at = timezone.now()
        prop.save(update_fields=["moderation_status", "is_published", "is_verified_by_admin",
                                 "moderation_version", "moderation_submitted_at", "updated_at"])
        _log(prop, actor, ModerationAction.SUBMITTED, source, previous)
        _notify_after_commit(prop.pk)
    return prop


def _decide(prop_id, *, actor, source, expected_version, allowed_from):
    if not can_moderate(actor):
        raise ModerationError("Немає прав на модерацію.")
    prop = Property.objects.select_for_update().get(pk=prop_id)
    if prop.archived_at:
        raise ModerationError("Оголошення в архіві.")
    if expected_version is not None and int(expected_version) != prop.moderation_version:
        raise ModerationError("Заявка застаріла: оголошення вже змінили або розглянули. Оновіть сторінку.")
    if prop.moderation_status not in allowed_from:
        raise ModerationError(f"Заявку вже розглянуто (статус: {prop.get_moderation_status_display()}).")
    return prop


def approve(prop_id, *, actor, source, expected_version=None, allowed_from=(ModerationStatus.PENDING,)):
    """Схвалити й опублікувати. Повертає Property."""
    with transaction.atomic():
        prop = _decide(prop_id, actor=actor, source=source, expected_version=expected_version,
                       allowed_from=allowed_from)
        previous = prop.moderation_status
        prop.moderation_status = ModerationStatus.APPROVED
        prop.is_published = True
        prop.is_verified_by_admin = True
        prop.verified_at = timezone.now()
        prop.moderation_note = ""
        prop.moderation_version += 1
        # save() без update_fields «перевірки» сигналу: activate_paid_placement_on_publication стартує оплачені періоди.
        prop.save(update_fields=["moderation_status", "is_published", "is_verified_by_admin", "verified_at",
                                 "moderation_note", "moderation_version", "updated_at"])
        _log(prop, actor, ModerationAction.APPROVED, source, previous)
    return prop


def reject(prop_id, *, actor, reason, source, expected_version=None,
           allowed_from=(ModerationStatus.PENDING,)):
    """Відхилити з обов'язковою причиною (її бачить власник). Повертає Property."""
    reason = (reason or "").strip()
    if not reason:
        raise ModerationError("Вкажіть причину відхилення — її побачить власник оголошення.")
    with transaction.atomic():
        prop = _decide(prop_id, actor=actor, source=source, expected_version=expected_version,
                       allowed_from=allowed_from)
        previous = prop.moderation_status
        prop.moderation_status = ModerationStatus.REJECTED
        prop.moderation_note = reason[:2000]
        prop.is_verified_by_admin = False
        prop.moderation_version += 1
        prop.save(update_fields=["moderation_status", "moderation_note", "is_verified_by_admin",
                                 "moderation_version", "updated_at"])
        _log(prop, actor, ModerationAction.REJECTED, source, previous, reason)
    return prop
