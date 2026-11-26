"""
Telegram-бот модерації (закритий робочий чат модераторів). Без сторонніх бібліотек — Bot API через urllib.

Надсилання: після подання/повторного подання оголошення (moderation.submit -> on_commit) — одне повідомлення
на (оголошення, версію заявки). Фото власника — локальні файли через Storage API (multipart): 1 фото —
sendPhoto з підписом і кнопками; кілька — альбоми sendMediaGroup по 10 (головне першим) + окреме повідомлення
з кнопками у відповідь на альбом. Фото надсилаються один раз (media_done); збій Telegram не ламає подання,
повідомлення повторюється ботом (retry_failed).

Кнопки «Підтвердити» / «Відхилити» (callback_data = mod:a|r:<id>:<версія>) — НЕ довіряємо даним кнопки:
- чат має бути саме TELEGRAM_MODERATION_CHAT_ID;
- Telegram user ID (не username) має бути прив'язаний (UserProfile.telegram_id) до активного Django-акаунта
  з правом модерації — перевіряється при кожному натисканні;
- версія заявки й статус перевіряються у спільному сервісі moderation (повторні/застарілі натискання — відмова);
- «Відхилити» просить причину відповіддю на повідомлення бота; приймається лише від того самого модератора.

Користувацький текст екранується (parse_mode=HTML). Токен не логується й не потрапляє в помилки.
"""

import html
import io
import json
import logging
import mimetypes
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError
from django.db.models import F
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from . import moderation
from .mobility import MOBILITY_LEVELS
from .private_files import PrivateReportStorage
from .models import ModerationStatus, Property, TelegramModerationMessage, TelegramRejectPrompt, UserProfile

logger = logging.getLogger(__name__)

CALLBACK_RE = re.compile(r"^mod:(a|r):(\d{1,12}):(\d{1,9})$")
MAX_ATTEMPTS = 5
MOBILITY_SHORT = {level["id"]: level["short"] for level in MOBILITY_LEVELS}


class TelegramError(Exception):
    def __init__(self, message, network=False):
        super().__init__(message)
        self.network = network  # мережа/таймаут (варто повторити), а не відмова Telegram


def is_enabled():
    return bool(settings.TELEGRAM_BOT_ENABLED and settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_MODERATION_CHAT_ID)


def _decode(raw):
    try:
        data = json.loads(raw.decode())
    except ValueError:
        raise TelegramError("Некоректна відповідь Telegram") from None
    if not data.get("ok"):
        raise TelegramError(str(data.get("description", "Telegram API error"))[:200])
    return data.get("result")


def _open(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 — фіксований https
            return _decode(resp.read())
    except urllib.error.HTTPError as exc:  # 400/403/413/429/5xx: у тілі — опис помилки Telegram
        retry_later = exc.code == 429 or exc.code >= 500  # ліміт частоти / збій Telegram — повторити пізніше
        try:
            return _decode(exc.read())
        except TelegramError as err:
            raise TelegramError(str(err), network=retry_later) from None
        except OSError:
            raise TelegramError(f"HTTP {exc.code}", network=retry_later) from None
    except OSError as exc:
        raise TelegramError(exc.__class__.__name__, network=True) from None  # без URL (у ньому токен)


def _api(method, payload, timeout=15):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}", data=json.dumps(payload).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    return _open(req, timeout)


def _api_upload(method, fields, files, timeout=60):
    """multipart/form-data: fields — значення (dict/list → JSON), files — [(attach_name, filename, bytes)]."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, content in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                     f"Content-Type: {mimetypes.guess_type(filename)[0] or 'image/jpeg'}\r\n\r\n".encode()
                     + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}", data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return _open(req, timeout)


# --- Повідомлення про заявку ---

TEXT_LIMIT, CAPTION_LIMIT = 4096, 1024  # ліміти Telegram (видимий текст)
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # sendPhoto: до 10 МБ
ALBUM_SIZE = 10  # sendMediaGroup: 2–10 елементів
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}


def public_site_url():
    """SITE_URL, лише якщо це справжня зовнішня https-адреса (не localhost) — інакше ''."""
    url = (settings.SITE_URL or "").rstrip("/")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host in LOCAL_HOSTS or host.endswith((".local", ".localhost")) \
            or host.startswith("127."):
        return ""
    return url


def _visible_len(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def _text(prop, desc_limit=1500):
    e = html.escape
    price = f"{prop.price:,.0f} грн".replace(",", " ") if prop.price is not None else "за домовленістю"
    level = MOBILITY_SHORT.get(prop.mobility_level)
    access = f"рівень {prop.mobility_level} — {level}" if level else "рівень не вказано"
    features = ", ".join(f.name for f in prop.features.all()[:10]) or "—"
    description = (prop.description or "").strip()
    if len(description) > desc_limit:
        description = description[:desc_limit].rsplit(" ", 1)[0] + "…"
    floor = f"\nПоверх: {prop.floor}/{prop.total_floors or '?'}" if prop.floor else ""
    location = ", ".join(x for x in (prop.city, prop.address) if x) or "—"
    title = "Повторна заявка" if prop.moderation_version > 1 else "Нова заявка"
    return (f"<b>{title} на модерацію · ID {prop.pk}</b>\n"
            f"<b>{e(prop.name)}</b>\n"
            f"Ціна: {e(price)}\n"
            f"Місто й адреса: {e(location)}\n"
            f"Площа: {e(f'{prop.area_sqm:g} м²' if prop.area_sqm else '—')}\n"
            f"Кімнат: {prop.rooms or '—'}{floor}\n"
            f"Доступність: {e(access)}\nЗручності: {e(features)}\n"
            f"Статус: {e(prop.get_moderation_status_display())}\n\n"
            f"{e(description) or '—'}")


def _fit(prop, limit):
    """Текст у межах ліміту Telegram — скорочуємо лише опис."""
    for desc_limit in (1500, 800, 400, 150, 0):
        text = _text(prop, desc_limit)
        if _visible_len(text) <= limit:
            return text
    return None


def _keyboard(prop):
    data = f"{prop.pk}:{prop.moderation_version}"
    rows = [[{"text": "✅ Підтвердити", "callback_data": f"mod:a:{data}"},
             {"text": "❌ Відхилити", "callback_data": f"mod:r:{data}"}]]
    site = public_site_url()
    if site:  # localhost Telegram не відкриє — кнопку не додаємо
        rows.append([{"text": "Відкрити оголошення в адмінці",
                      "url": site + reverse("admin:properties_property_change", args=[prop.pk])}])
    return {"inline_keyboard": rows}


def _photo_fields(prop):
    """Публічні фото власника: головне першим, далі галерея. Приватні звіти/документи сюди не потрапляють."""
    fields, seen = [], set()
    for field in [prop.photo] + [p.image for p in prop.extra_photos.all()]:
        if field and field.name and field.name not in seen and not isinstance(field.storage, PrivateReportStorage):
            seen.add(field.name)
            fields.append(field)
    return fields


def _read_photo(field):
    """Байти фото або None (немає файлу, пошкоджений, завеликий, не підходить Telegram)."""
    try:
        if field.storage.size(field.name) > PHOTO_MAX_BYTES:
            return None
        with field.storage.open(field.name, "rb") as fh:
            content = fh.read(PHOTO_MAX_BYTES + 1)
        if len(content) > PHOTO_MAX_BYTES:
            return None
        with Image.open(io.BytesIO(content)) as img:
            img.verify()
            width, height = img.size
        if not width or not height or width + height > 10000 or max(width, height) / min(width, height) > 20:
            return None
        return content
    except Exception:  # файл відсутній / пошкоджений — без фото, але заявка піде
        return None


def _load_photos(prop):
    photos = []
    for field in _photo_fields(prop):
        content = _read_photo(field)
        if content is None:
            logger.warning("Telegram: фото оголошення %s пропущено (немає/пошкоджене/завелике)", prop.pk)
        else:
            photos.append((re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(field.name))[-80:] or "photo.jpg", content))
    return photos


def _send_one_photo(chat_id, photo, **extra):
    return _api_upload("sendPhoto", {"chat_id": chat_id, **extra}, [("photo", photo[0], photo[1])])


def _send_album(chat_id, photos, caption):
    """Альбоми по 10 (залишок з 1 фото — sendPhoto). Повертає id першого повідомлення або None."""
    first_id = None
    for start in range(0, len(photos), ALBUM_SIZE):
        chunk = photos[start:start + ALBUM_SIZE]
        cap = caption if start == 0 else ""
        try:
            if len(chunk) == 1:
                result = [_send_one_photo(chat_id, chunk[0], **({"caption": cap} if cap else {}))]
            else:
                media = [{"type": "photo", "media": f"attach://p{i}", **({"caption": cap} if i == 0 and cap else {})}
                         for i in range(len(chunk))]
                result = _api_upload("sendMediaGroup", {"chat_id": chat_id, "media": media},
                                     [(f"p{i}", name, content) for i, (name, content) in enumerate(chunk)])
        except TelegramError as exc:
            if exc.network:  # мережа: нічого ще не надіслано — повторимо все пізніше; інакше — без решти фото
                if first_id is None:
                    raise
                break
            logger.warning("Telegram: альбом не прийнято (%s), надсилаю фото поодинці", exc)
            result = []
            for photo in chunk:
                try:
                    result.append(_send_one_photo(chat_id, photo))
                except TelegramError:
                    continue
        if result and first_id is None:
            first_id = result[0]["message_id"]
    return first_id


def _send(prop, chat_id, record):
    """Надіслати заявку: фото (один раз) + повідомлення з кнопками (обов'язково). Повертає message_id з кнопками."""
    keyboard = _keyboard(prop)
    photos = [] if record.media_done else _load_photos(prop)
    if len(photos) == 1 and not record.media_done:
        caption = _fit(prop, CAPTION_LIMIT)
        if caption:
            try:  # одне фото: підпис і кнопки в одному повідомленні
                return _send_one_photo(chat_id, photos[0], caption=caption, parse_mode="HTML",
                                       reply_markup=keyboard)["message_id"]
            except TelegramError as exc:
                if exc.network:
                    raise  # повторимо разом із фото
                logger.warning("Telegram: фото оголошення %s не прийнято (%s)", prop.pk, exc)
                photos = []  # далі — текстом
    if photos:
        record.media_message_id = _send_album(chat_id, photos, f"Фото оголошення ID {prop.pk} ({len(photos)})")
    if not record.media_done:  # фото — лише один раз, навіть якщо далі збій повідомлення з кнопками
        record.media_done = True
        record.save(update_fields=["media_done", "media_message_id", "updated_at"])
    payload = {"chat_id": chat_id, "text": _fit(prop, TEXT_LIMIT), "parse_mode": "HTML",
               "disable_web_page_preview": True, "reply_markup": keyboard}
    if record.media_message_id:  # прив'язка до альбому цього оголошення
        payload["reply_parameters"] = {"message_id": record.media_message_id, "allow_sending_without_reply": True}
    return _api("sendMessage", payload)["message_id"]


SENDING_TIMEOUT = timedelta(minutes=10)


def notify_new_submission(prop_id):
    """Надіслати заявку в чат (якщо бот увімкнено). Повторний виклик для тієї ж версії — без дубля."""
    if not is_enabled():
        return None
    prop = Property.objects.filter(pk=prop_id).prefetch_related("features", "extra_photos").first()
    if prop is None or prop.moderation_status != ModerationStatus.PENDING or prop.archived_at:
        return None
    chat_id = settings.TELEGRAM_MODERATION_CHAT_ID
    try:
        record, _ = TelegramModerationMessage.objects.get_or_create(
            property=prop, version=prop.moderation_version, defaults={"chat_id": chat_id})
    except IntegrityError:
        record = TelegramModerationMessage.objects.get(property=prop, version=prop.moderation_version)
    if record.status in ("sent", "closed", "sending") or record.attempts >= MAX_ATTEMPTS:
        return record
    # Захоплення запису: сайт і бот не надсилають ту саму заявку одночасно.
    if not TelegramModerationMessage.objects.filter(pk=record.pk, status=record.status).update(
            status="sending", attempts=F("attempts") + 1, updated_at=timezone.now()):
        return record
    record.refresh_from_db()
    try:
        record.message_id, record.status, record.last_error = _send(prop, chat_id, record), "sent", ""
    except TelegramError as exc:
        record.status, record.last_error = "failed", str(exc)[:255]
        logger.warning("Telegram moderation message failed for property %s: %s", prop.pk, record.last_error)
    record.save(update_fields=["message_id", "status", "last_error", "updated_at"])
    return record


def retry_failed():
    """Повторити невідправлені повідомлення актуальних заявок. Повертає кількість спроб."""
    TelegramModerationMessage.objects.filter(  # процес упав під час надсилання
        status="sending", updated_at__lt=timezone.now() - SENDING_TIMEOUT).update(status="failed")
    count = 0
    for record in TelegramModerationMessage.objects.filter(status__in=("pending", "failed"), attempts__lt=MAX_ATTEMPTS):
        prop = record.property
        if prop.moderation_status == ModerationStatus.PENDING and prop.moderation_version == record.version:
            notify_new_submission(prop.pk)
            count += 1
        else:
            TelegramModerationMessage.objects.filter(pk=record.pk).update(status="closed")
    return count


# --- Обробка натискань і відповідей ---


def _moderator_for(telegram_user_id):
    """Django-модератор за числовим Telegram ID; None — немає прив'язки або прав (перевіряється щоразу)."""
    profile = UserProfile.objects.select_related("user").filter(telegram_id=str(telegram_user_id)).first()
    user = profile.user if profile else None
    return user if moderation.can_moderate(user) else None


def _close_message(chat_id, message_id, prop_id, note):
    try:
        _api("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}})
    except TelegramError:
        pass
    _api("sendMessage", {"chat_id": chat_id, "reply_to_message_id": message_id, "text": note, "parse_mode": "HTML"})
    TelegramModerationMessage.objects.filter(property_id=prop_id, message_id=message_id).update(status="closed")


def _answer(callback_id, text):
    try:
        _api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190], "show_alert": True})
    except TelegramError:
        pass


def handle_update(update):
    """Обробити один update Bot API. Повертає короткий підсумок (для логів і тестів)."""
    if "callback_query" in update:
        return _handle_callback(update["callback_query"])
    message = update.get("message") or {}
    if message.get("reply_to_message") and message.get("text"):
        return _handle_reject_reason(message)
    return "ignored"


def _handle_callback(cq):
    message = cq.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if chat_id != str(settings.TELEGRAM_MODERATION_CHAT_ID):
        _answer(cq.get("id"), "Цей чат не є робочим чатом модерації.")
        return "wrong_chat"
    match = CALLBACK_RE.match(str(cq.get("data", "")))
    if not match:
        _answer(cq.get("id"), "Некоректна кнопка.")
        return "bad_data"
    action, prop_id, version = match.group(1), int(match.group(2)), int(match.group(3))
    user = _moderator_for((cq.get("from") or {}).get("id"))
    if user is None:
        _answer(cq.get("id"), "Немає прав модератора або Telegram ID не прив'язано до акаунта.")
        return "forbidden"
    if action == "a":
        try:
            prop = moderation.approve(prop_id, actor=user, source=moderation.ModerationSource.TELEGRAM,
                                      expected_version=version)
        except (moderation.ModerationError, Property.DoesNotExist) as exc:
            _answer(cq.get("id"), str(exc) or "Заявку не знайдено.")
            return "rejected_action"
        _answer(cq.get("id"), "Оголошення схвалено.")
        _close_message(chat_id, message.get("message_id"), prop.pk,
                       f"✅ Схвалено: #{prop.pk} — {html.escape(user.get_username())}")
        return "approved"
    # Відхилення: перевіряємо актуальність, просимо причину відповіддю (рішення — після причини).
    prop = Property.objects.filter(pk=prop_id).first()
    if prop is None or prop.moderation_status != ModerationStatus.PENDING or prop.moderation_version != version:
        _answer(cq.get("id"), "Заявку вже розглянуто або змінено.")
        return "stale"
    prompt = _api("sendMessage", {
        "chat_id": chat_id, "reply_to_message_id": message.get("message_id"),
        "text": f"Причина відхилення оголошення #{prop.pk}? Відповідайте на це повідомлення "
                f"(її побачить власник).", "reply_markup": {"force_reply": True, "selective": True}})
    TelegramRejectPrompt.objects.create(property=prop, version=version, telegram_user_id=int(cq["from"]["id"]),
                                        chat_id=chat_id, prompt_message_id=prompt["message_id"])
    _answer(cq.get("id"), "Напишіть причину відповіддю на повідомлення бота.")
    return "reason_requested"


def _handle_reject_reason(message):
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if chat_id != str(settings.TELEGRAM_MODERATION_CHAT_ID):
        return "wrong_chat"
    prompt = TelegramRejectPrompt.objects.filter(
        chat_id=chat_id, prompt_message_id=(message.get("reply_to_message") or {}).get("message_id"), used=False).first()
    if prompt is None:
        return "ignored"
    tg_user = (message.get("from") or {}).get("id")
    if tg_user != prompt.telegram_user_id:
        return "other_user"  # причину приймаємо лише від того, хто натиснув «Відхилити»
    user = _moderator_for(tg_user)
    if user is None:
        return "forbidden"
    reason = message["text"].strip()
    try:
        prop = moderation.reject(prompt.property_id, actor=user, reason=reason,
                                 source=moderation.ModerationSource.TELEGRAM, expected_version=prompt.version)
    except moderation.ModerationError as exc:
        _api("sendMessage", {"chat_id": chat_id, "reply_to_message_id": message.get("message_id"),
                             "text": f"Не відхилено: {exc}"})
        TelegramRejectPrompt.objects.filter(pk=prompt.pk).update(used=True)
        return "rejected_action"
    TelegramRejectPrompt.objects.filter(pk=prompt.pk).update(used=True)
    original = TelegramModerationMessage.objects.filter(property_id=prop.pk, version=prompt.version).first()
    note = (f"❌ Відхилено: #{prop.pk} — {html.escape(user.get_username())}\n"
            f"Причина: {html.escape(reason[:500])}")
    if original and original.message_id:
        _close_message(chat_id, original.message_id, prop.pk, note)
    else:
        _api("sendMessage", {"chat_id": chat_id, "text": note, "parse_mode": "HTML"})
    return "rejected"
