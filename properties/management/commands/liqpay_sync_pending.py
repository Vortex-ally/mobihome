"""
Уточнення незавершених платежів запитом статусу до LiqPay (запускати періодично, напр. cron раз на 10 хв):

    python manage.py liqpay_sync_pending

Обробляє:
- платежі «створено/очікує» старші за --min-age хвилин: серверний запит action=status; після закінчення
  терміну на оплату — «не завершено» (EXPIRED), але ЛИШЕ після достовірної відповіді LiqPay
  (payment_not_found або try_again). Помилка мережі/API не закриває платіж;
- незараховані «скасовано/не завершено», створені не пізніше ніж payment_hold_window() тому: форму
  LiqPay ще могли оплатити в іншій вкладці, а callback — загубитись. Успіх зараховується за чинними
  правилами (як пізній callback), інші відповіді такий платіж не змінюють.

Захист від перевантаження API: пауза --delay між запитами; після --max-failures помилок мережі
поспіль обробка зупиняється з ненульовим кодом виходу (для моніторингу cron/systemd).
Реквізити, ключі й підписи не виводяться.
"""

import time
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from properties import liqpay
from properties.models import Payment, PaymentStatus
from properties.placement import payment_hold_window

# Відповіді, після серії яких поспіль LiqPay вважається недоступним або таким, що обмежує запити.
PROVIDER_FAILURES = {"network_error", "invalid_response", "api_error"}


class Command(BaseCommand):
    help = "Уточнює статус незавершених платежів LiqPay (Status API)."

    def add_arguments(self, parser):
        parser.add_argument("--min-age", type=int, default=5, help="Мінімальний вік платежу, хв.")
        parser.add_argument("--limit", type=int, default=200, help="Максимум платежів за запуск.")
        parser.add_argument("--delay", type=float, default=0.5,
                            help="Пауза між запитами до LiqPay, с (захист від rate limit).")
        parser.add_argument("--max-failures", type=int, default=5,
                            help="Зупинитися після стількох помилок мережі поспіль.")

    def candidates(self, now, min_age, limit):
        closed_recently = Q(status__in=[PaymentStatus.CANCELLED, PaymentStatus.EXPIRED], applied_at__isnull=True,
                            created_at__gte=now - payment_hold_window())
        open_payments = Q(status__in=[PaymentStatus.CREATED, PaymentStatus.PENDING])
        return list(Payment.objects.filter(open_payments | closed_recently,
                                           created_at__lte=now - timedelta(minutes=min_age))
                    .order_by("created_at", "pk")[:limit])

    def handle(self, *args, **options):
        config = liqpay.LiqPayConfig.from_settings()
        if config.problems():
            self.stdout.write(self.style.WARNING("LiqPay не налаштовано — пропускаю: " + "; ".join(config.problems())))
            return
        payments = self.candidates(timezone.now(), options["min_age"], max(options["limit"], 0))
        counts, failures_in_row, aborted = {}, 0, False
        for index, payment in enumerate(payments):
            if index and options["delay"] > 0:
                time.sleep(options["delay"])
            try:
                result = liqpay.fetch_status(payment, config)
                result_note = result.note
                payment.refresh_from_db()
                liqpay.expire_if_confirmed(payment, result)  # помилка мережі/відповіді не закриває платіж
            except Exception as exc:  # один проблемний платіж не зупиняє обробку решти
                result_note = f"error:{exc.__class__.__name__}"
            counts[result_note] = counts.get(result_note, 0) + 1
            failures_in_row = failures_in_row + 1 if result_note in PROVIDER_FAILURES else 0
            if options["max_failures"] > 0 and failures_in_row >= options["max_failures"]:
                aborted = True
                break
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        self.stdout.write(f"Оброблено: {sum(counts.values())} з {len(payments)}; {summary}")
        if aborted:
            raise CommandError(f"LiqPay недоступний: {failures_in_row} помилок поспіль — зупинено. "
                               "Платежі не змінено; повторіть пізніше.")
