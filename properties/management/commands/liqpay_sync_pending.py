"""
Уточнення незавершених платежів запитом статусу до LiqPay (запускати періодично, напр. cron раз на 10 хв):

    python manage.py liqpay_sync_pending

Для кожного платежу «створено/очікує» старшого за --min-age хвилин: серверний запит action=status;
після закінчення терміну на оплату — позначка «не завершено». Реквізити/ключі не виводяться.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from properties import liqpay
from properties.models import Payment, PaymentStatus


class Command(BaseCommand):
    help = "Уточнює статус незавершених платежів LiqPay."

    def add_arguments(self, parser):
        parser.add_argument("--min-age", type=int, default=5, help="Мінімальний вік платежу, хв.")
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **options):
        config = liqpay.LiqPayConfig.from_settings()
        if config.problems():
            self.stdout.write(self.style.WARNING("LiqPay не налаштовано — пропускаю: " + "; ".join(config.problems())))
            return
        cutoff = timezone.now() - timedelta(minutes=options["min_age"])
        qs = Payment.objects.filter(status__in=[PaymentStatus.CREATED, PaymentStatus.PENDING],
                                    created_at__lte=cutoff).order_by("created_at")[: options["limit"]]
        counts = {}
        for payment in qs:
            result = liqpay.fetch_status(payment, config)
            payment.refresh_from_db()
            liqpay.expire_stale(payment)
            counts[result.note] = counts.get(result.note, 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        self.stdout.write(f"Оброблено: {sum(counts.values())}; {summary}")
