"""
Telegram-бот модерації (long polling):

    python manage.py telegram_moderation_bot            # працює постійно (systemd: deploy/telegram-bot.service)
    python manage.py telegram_moderation_bot --once     # один цикл (перевірка налаштувань)

Кожен цикл: повторне надсилання невідправлених заявок + обробка натискань кнопок і причин відхилення.
Потрібні TELEGRAM_BOT_ENABLED=True, TELEGRAM_BOT_TOKEN, TELEGRAM_MODERATION_CHAT_ID (див. TELEGRAM_SETUP.md).
"""

import time

from django.core.management.base import BaseCommand, CommandError

from properties import telegram_bot


class Command(BaseCommand):
    help = "Telegram-бот модерації оголошень (long polling)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Один цикл і вихід.")
        parser.add_argument("--timeout", type=int, default=25, help="Long polling, с.")

    def handle(self, *args, **options):
        if not telegram_bot.is_enabled():
            raise CommandError("Бот вимкнено: задайте TELEGRAM_BOT_ENABLED=True, TELEGRAM_BOT_TOKEN і "
                               "TELEGRAM_MODERATION_CHAT_ID (значення не виводяться).")
        offset = None
        while True:
            retried = telegram_bot.retry_failed()
            try:
                payload = {"timeout": 0 if options["once"] else options["timeout"],
                           "allowed_updates": ["callback_query", "message"]}
                if offset is not None:
                    payload["offset"] = offset
                updates = telegram_bot._api("getUpdates", payload, timeout=options["timeout"] + 10)
            except telegram_bot.TelegramError as exc:
                self.stderr.write(f"Telegram недоступний: {exc}")
                if options["once"]:
                    raise CommandError("Не вдалося отримати оновлення від Telegram.")
                time.sleep(10)
                continue
            for update in updates or []:
                offset = update["update_id"] + 1
                try:
                    result = telegram_bot.handle_update(update)
                except Exception as exc:  # один збійний update не зупиняє бота
                    result = f"error:{exc.__class__.__name__}"
                self.stdout.write(f"update {update['update_id']}: {result}")
            if options["once"]:
                if offset is not None:  # підтвердити оброблені оновлення
                    telegram_bot._api("getUpdates", {"offset": offset, "timeout": 0})
                self.stdout.write(f"Готово: оновлень {len(updates or [])}, повторних надсилань {retried}.")
                return
