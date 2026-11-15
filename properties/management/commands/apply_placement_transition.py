"""
Одноразовий перехідний період для оголошень, опублікованих до запуску обов'язкової оплати.

    # 1) Переглянути, кого торкнеться (нічого не змінює):
    python manage.py apply_placement_transition
    # 2) Застосувати в день активації платної системи (разом з PLACEMENT_PAYMENT_REQUIRED=True):
    python manage.py apply_placement_transition --apply
    #    або з явною датою активації:
    python manage.py apply_placement_transition --apply --start 2026-12-01

Змінює лише поле placement_until (= дата активації + 30 днів) для опублікованих, схвалених,
не архівних оголошень без оплаченого періоду. Статуси, модерація та інші дати не змінюються.
Повторне застосування заблоковано (журнал «Перехідний період» в адмінці).
"""

from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from properties.models import PlacementTransition
from properties.placement import TRANSITION_DAYS, PlacementError, apply_transition, transition_candidates


class Command(BaseCommand):
    help = "Одноразовий перехідний період (30 днів) для наявних опублікованих оголошень."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Застосувати (без нього — лише перегляд).")
        parser.add_argument("--start", help="Дата активації платної системи YYYY-MM-DD (типово — зараз).")

    def handle(self, *args, **options):
        existing = PlacementTransition.objects.first()
        if existing:
            self.stdout.write(self.style.WARNING(f"Уже застосовано: {existing}. Повторно не виконується."))
            return

        if options["start"]:
            try:
                day = datetime.strptime(options["start"], "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("Невірний формат --start, очікується YYYY-MM-DD.")
            starts_at = timezone.make_aware(datetime.combine(day, time.min))
        else:
            starts_at = timezone.now()

        ids = list(transition_candidates().values_list("pk", flat=True))
        ends = starts_at + timedelta(days=TRANSITION_DAYS)
        self.stdout.write(
            f"Оголошень для перехідного періоду: {len(ids)}; період до {timezone.localtime(ends):%d.%m.%Y %H:%M}."
        )
        if ids:
            self.stdout.write("ID: " + ", ".join(map(str, ids[:100])) + (" …" if len(ids) > 100 else ""))
        if not options["apply"]:
            self.stdout.write(self.style.NOTICE("Режим перегляду: нічого не змінено. Додайте --apply."))
            return
        try:
            record = apply_transition(starts_at, applied_by="manage.py apply_placement_transition")
        except PlacementError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f"Застосовано: {record}."))
